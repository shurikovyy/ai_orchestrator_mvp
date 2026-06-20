"""Inspection and controlled submission helpers for review findings artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from uuid import uuid4

from pydantic import ValidationError

from ai_orchestrator.review_findings import load_run_findings
from ai_orchestrator.review_findings_schemas import ReviewFinding, ReviewFindingsReport


SAFE_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")
MAX_FINDINGS_JSON_CHARS = 200_000

NO_FINDINGS_EXAMPLE = """{
  "schema_version": "1.0",
  "run_id": "run_example",
  "summary": "No findings from reviewer.",
  "findings": [],
  "overall_decision": "pass",
  "source_profile": "qa",
  "source_kind": "reviewer_profile"
}"""

MAJOR_FINDING_EXAMPLE = """{
  "schema_version": "1.0",
  "run_id": "run_example",
  "summary": "QA reviewer found one blocking issue.",
  "overall_decision": "needs_rework",
  "source_profile": "qa",
  "source_kind": "reviewer_profile",
  "findings": [
    {
      "id": "QA001",
      "reviewer": "qa",
      "category": "qa",
      "severity": "major",
      "title": "Missing regression test for changed behavior",
      "evidence": "The diff changes route behavior but no test covers the new branch.",
      "required_action": "Add a regression test that covers the changed route behavior.",
      "file": "tests/test_web_app.py",
      "line": null,
      "status": "open"
    }
  ]
}"""


@dataclass(frozen=True)
class FindingsFormatHelper:
    command_example: str
    profile_command_example: str
    top_level_fields: tuple[str, ...]
    finding_fields: tuple[str, ...]
    validation_rules: tuple[str, ...]
    no_findings_example: str
    major_finding_example: str


@dataclass(frozen=True)
class FindingsIndex:
    run_id: str
    run_dir: Path
    json_path: Path
    markdown_path: Path
    json_exists: bool
    markdown_exists: bool
    report: ReviewFindingsReport | None
    load_error: str | None
    findings: tuple["FindingListEntry", ...]
    format_helper: FindingsFormatHelper


@dataclass(frozen=True)
class FindingListEntry:
    finding: ReviewFinding
    safe_for_url: bool


@dataclass(frozen=True)
class FindingDetail:
    run_id: str
    json_path: Path
    finding: ReviewFinding


@dataclass(frozen=True)
class FindingsSubmission:
    report: ReviewFindingsReport
    normalized_json: str
    profile: str | None


class FindingNotFound(FileNotFoundError):
    """Raised when a review finding cannot be found for read-only inspection."""


def build_findings_index(*, run_id: str, runs_dir: str | Path) -> FindingsIndex:
    run_dir = (Path(runs_dir) / run_id).resolve()
    json_path = run_dir / "REVIEW_FINDINGS.json"
    markdown_path = run_dir / "REVIEW_FINDINGS.md"
    report: ReviewFindingsReport | None = None
    load_error: str | None = None

    if json_path.is_file():
        try:
            report = load_run_findings(run_dir)
        except (ValueError, ValidationError, json.JSONDecodeError) as exc:
            load_error = f"REVIEW_FINDINGS.json could not be loaded: {exc}"

    return FindingsIndex(
        run_id=run_id,
        run_dir=run_dir,
        json_path=json_path,
        markdown_path=markdown_path,
        json_exists=json_path.is_file(),
        markdown_exists=markdown_path.is_file(),
        report=report,
        load_error=load_error,
        findings=tuple(
            FindingListEntry(finding=finding, safe_for_url=_is_safe_id(finding.id))
            for finding in report.findings
        )
        if report is not None
        else tuple(),
        format_helper=build_findings_format_helper(run_id=run_id),
    )


def build_finding_detail(*, run_id: str, runs_dir: str | Path, finding_id: str) -> FindingDetail:
    index = build_findings_index(run_id=run_id, runs_dir=runs_dir)
    if index.report is None:
        raise FindingNotFound(f"review finding not found: {finding_id}")
    for entry in index.findings:
        finding = entry.finding
        if finding.id == finding_id:
            return FindingDetail(
                run_id=run_id,
                json_path=index.json_path,
                finding=finding,
            )
    raise FindingNotFound(f"review finding not found: {finding_id}")


def build_findings_format_helper(*, run_id: str) -> FindingsFormatHelper:
    return FindingsFormatHelper(
        command_example=(
            "python -m ai_orchestrator.cli record-findings "
            f"{run_id} --runs-dir .runs --findings-file path/to/REVIEW_FINDINGS.json"
        ),
        profile_command_example=(
            "python -m ai_orchestrator.cli record-findings "
            f"{run_id} --runs-dir .runs --findings-file path/to/REVIEW_FINDINGS.json --profile qa"
        ),
        top_level_fields=(
            'schema_version: "1.0"',
            "run_id: string",
            "summary: string",
            "findings: list[ReviewFinding]",
            'overall_decision: "pass" | "needs_rework" | "blocked"',
            "source_profile: string | null",
            'source_kind: "manual" | "deterministic" | "reviewer_profile" | "external" | null',
        ),
        finding_fields=(
            "id: string",
            "reviewer: string",
            "category: architecture | qa | business | ops | data | security | maintainability | documentation | correctness | other",
            "severity: critical | major | minor | nit",
            "title: string",
            "evidence: string",
            "required_action: string | null",
            "file: safe relative path | null",
            "line: positive int | null",
            "blocking: boolean | null",
            "status: open | resolved | accepted_risk",
        ),
        validation_rules=(
            "critical and major findings are blocking.",
            "critical/major findings require required_action.",
            "blocking must match severity policy if provided.",
            "overall_decision cannot be pass when blocking_open > 0.",
            "critical open findings require overall_decision = blocked.",
            "major open findings without critical require overall_decision = needs_rework.",
            "source_kind = reviewer_profile requires source_profile.",
            "with CLI --profile <profile>, each finding reviewer must match profile and category must be allowed for the profile.",
        ),
        no_findings_example=NO_FINDINGS_EXAMPLE,
        major_finding_example=MAJOR_FINDING_EXAMPLE,
    )


def validate_findings_submission(
    *,
    run_id: str,
    findings_json: str,
    profile: str | None,
) -> tuple[FindingsSubmission | None, str | None]:
    if not findings_json.strip():
        return None, "Findings JSON is required."
    if len(findings_json) > MAX_FINDINGS_JSON_CHARS:
        return None, f"Findings JSON must be {MAX_FINDINGS_JSON_CHARS} characters or fewer."
    try:
        report = ReviewFindingsReport.model_validate_json(findings_json)
    except (ValueError, ValidationError) as exc:
        return None, f"Findings JSON did not match ReviewFindingsReport: {exc}"
    if report.run_id != run_id:
        return None, f"Findings report run_id must match this run: expected {run_id}, got {report.run_id}."
    form_profile = profile.strip() if profile else ""
    if form_profile:
        if not _is_safe_id(form_profile):
            return None, "Profile may contain only letters, numbers, dot, dash, and underscore."
        if report.source_profile is not None and report.source_profile != form_profile:
            return (
                None,
                f"Findings report source_profile must be empty or match selected profile: {form_profile}.",
            )
        effective_profile = form_profile
    elif report.source_kind == "reviewer_profile" and report.source_profile:
        if not _is_safe_id(report.source_profile):
            return None, "Findings report source_profile is not a safe reviewer profile id."
        effective_profile = report.source_profile
    else:
        effective_profile = None
    normalized_json = json.dumps(report.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n"
    return FindingsSubmission(
        report=report,
        normalized_json=normalized_json,
        profile=effective_profile,
    ), None


def write_findings_input_file(
    *,
    project_root: str | Path,
    run_id: str,
    normalized_json: str,
) -> tuple[str, Path]:
    inputs_dir = findings_inputs_dir(project_root)
    inputs_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    input_id = f"{run_id}_{timestamp}_{uuid4().hex[:6]}.json"
    input_path = (inputs_dir / input_id).resolve()
    if not _is_relative_to(input_path, inputs_dir):
        raise ValueError("findings input file must remain under .web/findings_inputs")
    input_path.write_text(normalized_json, encoding="utf-8")
    return input_id, input_path


def findings_inputs_dir(project_root: str | Path) -> Path:
    return (Path(project_root) / ".web" / "findings_inputs").resolve()


def _is_safe_id(value: str) -> bool:
    return bool(value and value not in {".", ".."} and SAFE_ID_PATTERN.fullmatch(value))


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True
