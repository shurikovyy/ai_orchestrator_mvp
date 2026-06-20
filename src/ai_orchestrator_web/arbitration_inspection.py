"""Read-only inspection helpers for review arbitration artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

from pydantic import ValidationError

from ai_orchestrator.review_arbitration import is_arbitration_stale, load_run_arbitration
from ai_orchestrator.review_arbitration_schemas import ArbitratedFinding, ReviewArbitrationReport
from ai_orchestrator.review_findings import load_run_findings
from ai_orchestrator.review_findings_schemas import ReviewFinding, ReviewFindingsReport


SAFE_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")

UPHELD_BLOCKING_EXAMPLE = """{
  "schema_version": "1.0",
  "run_id": "run_example",
  "arbiter": "human",
  "summary": "QA blocking finding upheld; rework is required before approval.",
  "overall_decision": "needs_rework",
  "arbitrated_findings": [
    {
      "finding_id": "QA001",
      "source_reviewer": "qa",
      "original_severity": "major",
      "final_severity": "major",
      "original_blocking": true,
      "final_blocking": true,
      "status": "upheld",
      "reason": "The missing regression test is a valid blocker.",
      "final_required_action": "Add a regression test that covers the changed behavior.",
      "human_escalation_required": false,
      "deterministic_hard_gate": false
    }
  ]
}"""

ACCEPTED_RISK_EXAMPLE = """{
  "schema_version": "1.0",
  "run_id": "run_example",
  "arbiter": "human",
  "summary": "Risk accepted by human owner for this run.",
  "overall_decision": "human_escalation",
  "arbitrated_findings": [
    {
      "finding_id": "QA002",
      "source_reviewer": "qa",
      "original_severity": "major",
      "final_severity": "major",
      "original_blocking": true,
      "final_blocking": false,
      "status": "accepted_risk",
      "reason": "The owner explicitly accepts this risk for the current run.",
      "final_required_action": null,
      "human_escalation_required": true,
      "deterministic_hard_gate": false
    }
  ]
}"""


@dataclass(frozen=True)
class ArbitrationFormatHelper:
    command_example: str
    top_level_fields: tuple[str, ...]
    arbitrated_finding_fields: tuple[str, ...]
    validation_rules: tuple[str, ...]
    upheld_blocking_example: str
    accepted_risk_example: str


@dataclass(frozen=True)
class SafeFindingEntry:
    finding_id: str
    safe_for_url: bool


@dataclass(frozen=True)
class ArbitrationIndex:
    run_id: str
    run_dir: Path
    findings_json_path: Path
    findings_markdown_path: Path
    arbitration_json_path: Path
    arbitration_markdown_path: Path
    findings_exists: bool
    findings_markdown_exists: bool
    arbitration_exists: bool
    arbitration_markdown_exists: bool
    findings_load_error: str | None
    arbitration_load_error: str | None
    findings_report: ReviewFindingsReport | None
    arbitration_report: ReviewArbitrationReport | None
    arbitration_stale: bool
    open_blocking_findings: tuple[ReviewFinding, ...]
    arbitrated_findings: tuple[ArbitratedFinding, ...]
    arbitrated_entries: tuple[SafeFindingEntry, ...]
    missing_open_blocking_ids: tuple[str, ...]
    format_helper: ArbitrationFormatHelper


@dataclass(frozen=True)
class ArbitrationDetail:
    run_id: str
    arbitration_json_path: Path
    arbitrated_finding: ArbitratedFinding


class ArbitrationFindingNotFound(FileNotFoundError):
    """Raised when an arbitrated finding cannot be found for read-only inspection."""


def build_arbitration_index(*, run_id: str, runs_dir: str | Path) -> ArbitrationIndex:
    run_dir = (Path(runs_dir) / run_id).resolve()
    findings_json_path = run_dir / "REVIEW_FINDINGS.json"
    findings_markdown_path = run_dir / "REVIEW_FINDINGS.md"
    arbitration_json_path = run_dir / "REVIEW_ARBITRATION.json"
    arbitration_markdown_path = run_dir / "REVIEW_ARBITRATION.md"
    findings_report: ReviewFindingsReport | None = None
    arbitration_report: ReviewArbitrationReport | None = None
    findings_load_error: str | None = None
    arbitration_load_error: str | None = None

    if findings_json_path.is_file():
        try:
            findings_report = load_run_findings(run_dir)
        except (ValueError, ValidationError) as exc:
            findings_load_error = f"REVIEW_FINDINGS.json could not be loaded: {exc}"

    if arbitration_json_path.is_file():
        try:
            arbitration_report = load_run_arbitration(run_dir)
        except (ValueError, ValidationError) as exc:
            arbitration_load_error = f"REVIEW_ARBITRATION.json could not be loaded: {exc}"

    open_blocking_findings = _open_blocking_findings(findings_report)
    arbitrated_findings = tuple(arbitration_report.arbitrated_findings) if arbitration_report is not None else tuple()
    arbitrated_ids = {finding.finding_id for finding in arbitrated_findings}
    missing_open_blocking_ids = tuple(
        finding.id for finding in open_blocking_findings if finding.id not in arbitrated_ids
    )
    stale = is_arbitration_stale(run_dir, arbitration_report) if arbitration_report is not None else False

    return ArbitrationIndex(
        run_id=run_id,
        run_dir=run_dir,
        findings_json_path=findings_json_path,
        findings_markdown_path=findings_markdown_path,
        arbitration_json_path=arbitration_json_path,
        arbitration_markdown_path=arbitration_markdown_path,
        findings_exists=findings_json_path.is_file(),
        findings_markdown_exists=findings_markdown_path.is_file(),
        arbitration_exists=arbitration_json_path.is_file(),
        arbitration_markdown_exists=arbitration_markdown_path.is_file(),
        findings_load_error=findings_load_error,
        arbitration_load_error=arbitration_load_error,
        findings_report=findings_report,
        arbitration_report=arbitration_report,
        arbitration_stale=stale,
        open_blocking_findings=open_blocking_findings,
        arbitrated_findings=arbitrated_findings,
        arbitrated_entries=tuple(
            SafeFindingEntry(finding_id=finding.finding_id, safe_for_url=_is_safe_id(finding.finding_id))
            for finding in arbitrated_findings
        ),
        missing_open_blocking_ids=missing_open_blocking_ids,
        format_helper=build_arbitration_format_helper(run_id=run_id),
    )


def build_arbitration_detail(*, run_id: str, runs_dir: str | Path, finding_id: str) -> ArbitrationDetail:
    index = build_arbitration_index(run_id=run_id, runs_dir=runs_dir)
    if index.arbitration_report is None:
        raise ArbitrationFindingNotFound(f"arbitrated finding not found: {finding_id}")
    for arbitrated in index.arbitrated_findings:
        if arbitrated.finding_id == finding_id:
            return ArbitrationDetail(
                run_id=run_id,
                arbitration_json_path=index.arbitration_json_path,
                arbitrated_finding=arbitrated,
            )
    raise ArbitrationFindingNotFound(f"arbitrated finding not found: {finding_id}")


def build_arbitration_format_helper(*, run_id: str) -> ArbitrationFormatHelper:
    return ArbitrationFormatHelper(
        command_example=(
            "python -m ai_orchestrator.cli record-arbitration "
            f"{run_id} --runs-dir .runs --arbitration-file path/to/REVIEW_ARBITRATION.json"
        ),
        top_level_fields=(
            'schema_version: "1.0"',
            "run_id: string",
            "created_at: datetime (generated by schema if omitted)",
            "source_findings_path: string | null (usually populated by CLI/core)",
            "source_findings_sha256: string | null (usually populated by CLI/core)",
            "source_findings_updated_at: string | null (usually populated by CLI/core)",
            "arbitration_stale: boolean (usually populated by CLI/core)",
            'arbiter: "manual" | "deterministic" | "llm_future" | "human"',
            "summary: string",
            'overall_decision: "pass" | "needs_rework" | "blocked" | "human_escalation"',
            "arbitrated_findings: list[ArbitratedFinding]",
            "counts: computed by schema/core",
        ),
        arbitrated_finding_fields=(
            "finding_id: string",
            "source_reviewer: string",
            'original_severity: "critical" | "major" | "minor" | "nit"',
            'final_severity: "critical" | "major" | "minor" | "nit"',
            "original_blocking: boolean",
            "final_blocking: boolean",
            'status: "upheld" | "downgraded" | "upgraded" | "dismissed" | "needs_evidence" | "conflict" | "accepted_risk"',
            "reason: string",
            "final_required_action: string | null",
            "human_escalation_required: boolean",
            "deterministic_hard_gate: boolean",
        ),
        validation_rules=(
            "final_required_action is required when final_blocking=true.",
            "status=downgraded requires final_severity lower than original_severity.",
            "status=upgraded requires final_severity higher than original_severity.",
            "status=dismissed implies final_blocking=false.",
            "status=accepted_risk implies final_blocking=false.",
            "accepted_risk for critical/major original severity requires human_escalation_required=true.",
            "deterministic_hard_gate findings cannot be dismissed.",
            "deterministic_hard_gate findings must remain final_blocking=true.",
            "deterministic_hard_gate critical/major findings cannot be downgraded below original severity.",
            "overall_decision cannot be pass when final_blocking > 0.",
            "overall_decision cannot be pass when human escalation is required.",
            "core record-arbitration validates arbitrated findings against REVIEW_FINDINGS.json.",
            "core record-arbitration requires all open blocking findings to be included.",
        ),
        upheld_blocking_example=UPHELD_BLOCKING_EXAMPLE,
        accepted_risk_example=ACCEPTED_RISK_EXAMPLE,
    )


def _open_blocking_findings(report: ReviewFindingsReport | None) -> tuple[ReviewFinding, ...]:
    if report is None:
        return tuple()
    return tuple(finding for finding in report.findings if finding.status == "open" and bool(finding.blocking))


def _is_safe_id(value: str) -> bool:
    return bool(value and value not in {".", ".."} and SAFE_ID_PATTERN.fullmatch(value))
