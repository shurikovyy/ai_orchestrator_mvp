from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from ai_orchestrator.schemas import ExecutionResult, StructuredExecutionReport


@dataclass(frozen=True)
class StructuredReportLoadResult:
    report: StructuredExecutionReport | None
    source: str | None
    source_path: Path | None = None
    error: str | None = None


@dataclass(frozen=True)
class ValidationCommandResult:
    command: str
    status: str
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    reason: str | None = None


@dataclass(frozen=True)
class WorkspaceManifestValidationResult:
    status: str
    missing_reported_files: list[str]
    unreported_workspace_files: list[str]
    ignored_reported_files: list[str]
    unchanged_reported_files: list[str] = field(default_factory=list)
    workspace_dir: Path | None = None
    reason: str | None = None


_MANIFEST_REPORTABLE_EXTENSIONS = {
    ".txt",
    ".md",
    ".py",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".sql",
    ".csv",
}

_MANIFEST_IGNORED_DIR_NAMES = {
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".git",
}

_MANIFEST_IGNORED_FILE_SUFFIXES = {
    ".pyc",
    ".pyo",
}


def _normalize_manifest_path(value: str) -> str:
    normalized = value.replace("\\", "/").strip().lstrip("./")
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    return normalized


def _manifest_key(value: str) -> str:
    return _normalize_manifest_path(value).lower()


def is_manifest_ignored_path(path: Path) -> bool:
    parts = set(path.parts)
    if parts & _MANIFEST_IGNORED_DIR_NAMES:
        return True
    if path.suffix.lower() in _MANIFEST_IGNORED_FILE_SUFFIXES:
        return True
    return False


def is_manifest_reportable_file(path: Path) -> bool:
    return path.suffix.lower() in _MANIFEST_REPORTABLE_EXTENSIONS and not is_manifest_ignored_path(path)


def collect_workspace_manifest_files(workspace_dir: Path) -> list[str]:
    paths: list[str] = []
    for path in sorted(workspace_dir.rglob("*")):
        if not path.is_file():
            continue
        rel_path = path.relative_to(workspace_dir)
        if is_manifest_reportable_file(rel_path):
            paths.append(rel_path.as_posix())
    return paths


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def collect_workspace_manifest_fingerprints(workspace_dir: Path) -> dict[str, str]:
    """Return reportable workspace files as normalized relative path -> sha256."""

    fingerprints: dict[str, str] = {}
    for path in sorted(workspace_dir.rglob("*")):
        if not path.is_file():
            continue
        rel_path = path.relative_to(workspace_dir)
        if is_manifest_reportable_file(rel_path):
            fingerprints[rel_path.as_posix()] = _file_sha256(path)
    return fingerprints


def write_workspace_manifest_snapshot(snapshot_path: Path, workspace_dir: Path) -> None:
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "1.0",
        "workspace_dir": str(workspace_dir),
        "files": collect_workspace_manifest_fingerprints(workspace_dir),
    }
    snapshot_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def read_workspace_manifest_snapshot(snapshot_path: Path) -> dict[str, str]:
    payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    files = payload.get("files", {})
    if not isinstance(files, dict):
        raise ValueError("workspace manifest snapshot `files` must be an object")
    return {str(path): str(digest) for path, digest in files.items()}


def find_workspace_baseline_manifest_path(artifact_paths: list[str]) -> Path | None:
    for artifact in artifact_paths:
        path = Path(artifact)
        if path.name == "workspace_baseline_manifest.json" and path.exists():
            return path
    return None


def _diff_manifest_files(*, baseline_files: dict[str, str], actual_files: dict[str, str]) -> list[str]:
    baseline_by_key = {_manifest_key(path): path for path in baseline_files}
    actual_by_key = {_manifest_key(path): path for path in actual_files}

    added = [actual_by_key[key] for key in sorted(actual_by_key.keys() - baseline_by_key.keys())]
    deleted = [baseline_by_key[key] for key in sorted(baseline_by_key.keys() - actual_by_key.keys())]
    modified = [
        actual_by_key[key]
        for key in sorted(actual_by_key.keys() & baseline_by_key.keys())
        if actual_files[actual_by_key[key]] != baseline_files[baseline_by_key[key]]
    ]
    return sorted(added + modified + deleted, key=_manifest_key)


def validate_workspace_manifest(
    loaded_report: StructuredReportLoadResult,
    *,
    baseline_manifest_path: Path | None = None,
) -> WorkspaceManifestValidationResult:
    report = loaded_report.report
    if report is None:
        return WorkspaceManifestValidationResult(
            status="skipped",
            missing_reported_files=[],
            unreported_workspace_files=[],
            ignored_reported_files=[],
            reason="structured report is unavailable",
        )
    if loaded_report.source_path is None:
        return WorkspaceManifestValidationResult(
            status="failed",
            missing_reported_files=[],
            unreported_workspace_files=[],
            ignored_reported_files=[],
            reason="structured report has no filesystem path, so validator cannot infer workspace cwd",
        )

    workspace_dir = loaded_report.source_path.parent
    actual_fingerprints = collect_workspace_manifest_fingerprints(workspace_dir)
    actual_by_key = {_manifest_key(path): path for path in actual_fingerprints}

    baseline_fingerprints: dict[str, str] | None = None
    baseline_by_key: dict[str, str] = {}
    if baseline_manifest_path is not None:
        try:
            baseline_fingerprints = read_workspace_manifest_snapshot(baseline_manifest_path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return WorkspaceManifestValidationResult(
                status="failed",
                missing_reported_files=[],
                unreported_workspace_files=[],
                ignored_reported_files=[],
                workspace_dir=workspace_dir,
                reason=f"could not read workspace baseline manifest: {exc}",
            )
        baseline_by_key = {_manifest_key(path): path for path in baseline_fingerprints}
        expected_files = _diff_manifest_files(baseline_files=baseline_fingerprints, actual_files=actual_fingerprints)
    else:
        # Backward-compatible 0.1.5 behavior for empty workspaces: every
        # reportable file present after execution must be listed in changed_files.
        expected_files = sorted(actual_fingerprints, key=_manifest_key)

    expected_by_key = {_manifest_key(path): path for path in expected_files}
    reported_files = [_normalize_manifest_path(path) for path in report.changed_files]
    reported_by_key = {_manifest_key(path): path for path in reported_files}

    ignored_reported_files = [path for path in reported_files if is_manifest_ignored_path(Path(path))]
    ignored_reported_keys = {_manifest_key(path) for path in ignored_reported_files}

    missing_reported_files: list[str] = []
    unchanged_reported_files: list[str] = []
    for key, path in reported_by_key.items():
        if key in ignored_reported_keys or key in expected_by_key:
            continue
        if baseline_fingerprints is not None and (key in actual_by_key or key in baseline_by_key):
            unchanged_reported_files.append(path)
        else:
            missing_reported_files.append(path)

    unreported_workspace_files = [
        path
        for key, path in expected_by_key.items()
        if key not in reported_by_key
    ]

    status = "passed"
    if missing_reported_files or unreported_workspace_files or ignored_reported_files or unchanged_reported_files:
        status = "failed"

    return WorkspaceManifestValidationResult(
        status=status,
        missing_reported_files=sorted(missing_reported_files),
        unreported_workspace_files=sorted(unreported_workspace_files),
        ignored_reported_files=sorted(ignored_reported_files),
        unchanged_reported_files=sorted(unchanged_reported_files),
        workspace_dir=workspace_dir,
    )


def _read_report_from_artifacts(result: ExecutionResult) -> tuple[str, str, Path | None] | None:
    for artifact in result.artifact_paths:
        path = Path(artifact)
        if path.name.lower() != "execution_report.json":
            continue
        if not path.exists():
            return str(path), "artifact path exists in ExecutionResult, but file is missing", path
        return str(path), path.read_text(encoding="utf-8", errors="replace"), path
    return None


def _extract_report_from_content(content: str) -> tuple[str, str, Path | None] | None:
    # CodexCliBackend appends workspace files as markdown sections:
    #
    #   ### EXECUTION_REPORT.json
    #   { ... }
    #
    # This fallback keeps tests and non-file backends easy to support.
    pattern = re.compile(
        r"^###\s+EXECUTION_REPORT\.json\s*\n(?P<body>.*?)(?=\n###\s+|\Z)",
        flags=re.IGNORECASE | re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(content)
    if match:
        return "content section: EXECUTION_REPORT.json", match.group("body").strip(), None

    # Optional fenced fallback. Useful when a backend only returns a final message.
    fenced = re.compile(
        r"```json\s*(?P<body>\{.*?\})\s*```",
        flags=re.IGNORECASE | re.DOTALL,
    )
    match = fenced.search(content)
    if match and "schema_version" in match.group("body") and "changed_files" in match.group("body"):
        return "content fenced json", match.group("body").strip(), None
    return None


def load_structured_report(result: ExecutionResult) -> StructuredReportLoadResult:
    raw = _read_report_from_artifacts(result) or _extract_report_from_content(result.content)
    if raw is None:
        return StructuredReportLoadResult(report=None, source=None)

    source, body, source_path = raw
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        return StructuredReportLoadResult(
            report=None,
            source=source,
            source_path=source_path,
            error=f"EXECUTION_REPORT.json is not valid JSON: {exc}",
        )

    try:
        report = StructuredExecutionReport.model_validate(parsed)
    except ValidationError as exc:
        return StructuredReportLoadResult(
            report=None,
            source=source,
            source_path=source_path,
            error=f"EXECUTION_REPORT.json does not match schema: {exc}",
        )
    return StructuredReportLoadResult(report=report, source=source, source_path=source_path, error=None)


def _norm(value: str) -> str:
    return value.replace("\\", "/").strip().lower()


def evaluate_structured_criterion(criterion: str, report: StructuredExecutionReport) -> tuple[bool, str | None]:
    """Evaluate a small explicit criterion DSL against EXECUTION_REPORT.json.

    Supported examples:
      - report.status=completed
      - changed_files includes src/toy_calc.py
      - changed_files:src/toy_calc.py
      - commands_run includes python -m unittest discover -s tests
      - commands_run:python -m unittest discover -s tests
      - tests.status=passed
      - tests passed

    Unknown criteria are not handled here and should be checked with the legacy
    text matcher by the caller.
    """

    raw = criterion.strip()
    lower = raw.lower()

    if lower.startswith("report.status="):
        expected = raw.split("=", 1)[1].strip().lower()
        ok = report.status == expected
        return ok, None if ok else f"Expected report.status={expected}, got {report.status}."

    if lower.startswith("changed_files includes "):
        expected = raw[len("changed_files includes ") :].strip()
        files = [_norm(item) for item in report.changed_files]
        ok = any(_norm(expected) in item or item in _norm(expected) for item in files)
        return ok, None if ok else f"Expected changed_files to include {expected}."

    if lower.startswith("changed_files:"):
        expected = raw.split(":", 1)[1].strip()
        files = [_norm(item) for item in report.changed_files]
        ok = any(_norm(expected) in item or item in _norm(expected) for item in files)
        return ok, None if ok else f"Expected changed_files to include {expected}."

    if lower.startswith("commands_run includes "):
        expected = raw[len("commands_run includes ") :].strip().lower()
        ok = any(expected in command.command.lower() for command in report.commands_run)
        return ok, None if ok else f"Expected commands_run to include {expected}."

    if lower.startswith("commands_run:"):
        expected = raw.split(":", 1)[1].strip().lower()
        ok = any(expected in command.command.lower() for command in report.commands_run)
        return ok, None if ok else f"Expected commands_run to include {expected}."

    if lower in {"tests.status=passed", "tests passed"}:
        ok = bool(report.tests) and all(test.status == "passed" for test in report.tests)
        return ok, None if ok else "Expected at least one test report and all test statuses to be passed."

    return False, "UNHANDLED"


_DANGEROUS_SHELL_TOKENS = ("&&", "||", ";", "|", ">", "<", "`", "$(", "\n", "\r")


def split_validation_command(command: str) -> list[str]:
    """Split a report command for shell=False execution.

    The validator intentionally supports simple commands only. Anything that
    looks like a shell pipeline/redirection is rejected before it can execute.
    """

    if any(token in command for token in _DANGEROUS_SHELL_TOKENS):
        raise ValueError("command contains shell control characters or redirection")
    parts = shlex.split(command, posix=os.name != "nt")
    if not parts:
        raise ValueError("command is empty")
    return parts


def normalize_validation_command(command: str) -> list[str]:
    """Return argv for a safe validation command.

    Supported MVP allowlist:
      - python -m unittest ...
      - python -m pytest ...
      - pytest ...

    Python invocations are rebound to sys.executable so the rerun uses the same
    interpreter/venv as the orchestrator process instead of relying on PATH.
    """

    parts = split_validation_command(command)
    executable = Path(parts[0]).name.lower()

    if executable in {"python", "python.exe", "py", "py.exe"}:
        if len(parts) >= 3 and parts[1] == "-m" and parts[2] in {"unittest", "pytest"}:
            return [sys.executable, *parts[1:]]
        raise ValueError("only `python -m unittest ...` and `python -m pytest ...` are allowed")

    if executable in {"pytest", "pytest.exe"}:
        return [*parts]

    raise ValueError("command is not in validator allowlist")


def rerun_report_test_commands(
    *,
    loaded_report: StructuredReportLoadResult,
    timeout_seconds: int,
) -> list[ValidationCommandResult]:
    """Rerun test commands from EXECUTION_REPORT.json in the report workspace.

    This is deliberately limited to `report.tests[*].command`, not every
    `commands_run` item, because setup/edit commands are often write-oriented.
    """

    report = loaded_report.report
    if report is None:
        return []
    if loaded_report.source_path is None:
        return [
            ValidationCommandResult(
                command="",
                status="skipped",
                reason="structured report has no filesystem path, so validator cannot infer workspace cwd",
            )
        ]

    workspace_dir = loaded_report.source_path.parent
    results: list[ValidationCommandResult] = []
    for test in report.tests:
        command = test.command.strip()
        if test.status in {"skipped", "not_run"}:
            results.append(
                ValidationCommandResult(
                    command=command,
                    status="skipped",
                    reason=f"reported test status is {test.status}",
                )
            )
            continue
        try:
            argv = normalize_validation_command(command)
        except ValueError as exc:
            results.append(
                ValidationCommandResult(
                    command=command,
                    status="blocked",
                    reason=str(exc),
                )
            )
            continue
        try:
            completed = subprocess.run(
                argv,
                cwd=workspace_dir,
                text=True,
                capture_output=True,
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            results.append(
                ValidationCommandResult(
                    command=command,
                    status="failed",
                    exit_code=None,
                    stdout=exc.stdout or "",
                    stderr=exc.stderr or "",
                    reason=f"timed out after {timeout_seconds} seconds",
                )
            )
            continue
        results.append(
            ValidationCommandResult(
                command=command,
                status="passed" if completed.returncode == 0 else "failed",
                exit_code=completed.returncode,
                stdout=completed.stdout.strip(),
                stderr=completed.stderr.strip(),
            )
        )
    return results
