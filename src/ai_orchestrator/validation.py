from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from ai_orchestrator.schemas import ExecutionResult, StructuredExecutionReport


@dataclass(frozen=True)
class StructuredReportLoadResult:
    report: StructuredExecutionReport | None
    source: str | None
    error: str | None = None


def _read_report_from_artifacts(result: ExecutionResult) -> tuple[str, str] | None:
    for artifact in result.artifact_paths:
        path = Path(artifact)
        if path.name.lower() != "execution_report.json":
            continue
        if not path.exists():
            return str(path), "artifact path exists in ExecutionResult, but file is missing"
        return str(path), path.read_text(encoding="utf-8", errors="replace")
    return None


def _extract_report_from_content(content: str) -> tuple[str, str] | None:
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
        return "content section: EXECUTION_REPORT.json", match.group("body").strip()

    # Optional fenced fallback. Useful when a backend only returns a final message.
    fenced = re.compile(
        r"```json\s*(?P<body>\{.*?\})\s*```",
        flags=re.IGNORECASE | re.DOTALL,
    )
    match = fenced.search(content)
    if match and "schema_version" in match.group("body") and "changed_files" in match.group("body"):
        return "content fenced json", match.group("body").strip()
    return None


def load_structured_report(result: ExecutionResult) -> StructuredReportLoadResult:
    raw = _read_report_from_artifacts(result) or _extract_report_from_content(result.content)
    if raw is None:
        return StructuredReportLoadResult(report=None, source=None, error=None)

    source, body = raw
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        return StructuredReportLoadResult(
            report=None,
            source=source,
            error=f"EXECUTION_REPORT.json is not valid JSON: {exc}",
        )

    try:
        report = StructuredExecutionReport.model_validate(parsed)
    except ValidationError as exc:
        return StructuredReportLoadResult(
            report=None,
            source=source,
            error=f"EXECUTION_REPORT.json does not match schema: {exc}",
        )
    return StructuredReportLoadResult(report=report, source=source, error=None)


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
