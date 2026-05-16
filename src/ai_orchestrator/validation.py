from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
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
