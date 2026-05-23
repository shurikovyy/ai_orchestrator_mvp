from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

from ai_orchestrator.apply import load_run_state
from ai_orchestrator.review_findings import load_run_findings
from ai_orchestrator.schemas import ArbitratedFinding, ReviewArbitrationReport, ReviewFinding


@dataclass(frozen=True)
class RecordArbitrationResult:
    run_id: str
    overall_decision: str
    final_blocking_count: int
    human_escalation_required: bool
    review_arbitration_path: Path
    review_arbitration_markdown_path: Path
    state_path: Path


def load_arbitration_file(path: str | Path) -> ReviewArbitrationReport:
    arbitration_path = Path(path).expanduser().resolve()
    if not arbitration_path.exists():
        raise FileNotFoundError(f"arbitration file does not exist: {arbitration_path}")
    if not arbitration_path.is_file():
        raise ValueError(f"arbitration path is not a file: {arbitration_path}")
    return ReviewArbitrationReport.model_validate_json(arbitration_path.read_text(encoding="utf-8-sig"))


def _report_with_source_findings_path(
    report: ReviewArbitrationReport,
    *,
    source_findings_path: str | None,
) -> ReviewArbitrationReport:
    payload = report.model_dump(mode="python")
    payload["source_findings_path"] = source_findings_path
    return ReviewArbitrationReport.model_validate(payload)


def _source_finding_map(findings: list[ReviewFinding]) -> dict[str, ReviewFinding]:
    return {finding.id: finding for finding in findings}


def _validate_arbitrated_finding_against_source(
    arbitrated: ArbitratedFinding,
    *,
    source_finding: ReviewFinding,
) -> None:
    if arbitrated.source_reviewer != source_finding.reviewer:
        raise ValueError(
            f"source reviewer mismatch for arbitrated finding {arbitrated.finding_id}: expected {source_finding.reviewer}, got {arbitrated.source_reviewer}"
        )
    if arbitrated.original_severity != source_finding.severity:
        raise ValueError(
            f"original severity mismatch for arbitrated finding {arbitrated.finding_id}: expected {source_finding.severity}, got {arbitrated.original_severity}"
        )
    if arbitrated.original_blocking != bool(source_finding.blocking):
        raise ValueError(
            f"original blocking mismatch for arbitrated finding {arbitrated.finding_id}: expected {str(bool(source_finding.blocking)).lower()}, got {str(arbitrated.original_blocking).lower()}"
        )
    if (
        source_finding.reviewer == "deterministic"
        and source_finding.severity in {"critical", "major"}
        and not arbitrated.deterministic_hard_gate
    ):
        raise ValueError(
            f"deterministic critical/major findings must set deterministic_hard_gate=true: {arbitrated.finding_id}"
        )


def _validate_against_source_findings(report: ReviewArbitrationReport, *, run_dir: Path) -> ReviewArbitrationReport:
    findings_report = load_run_findings(run_dir)
    if findings_report is None:
        return report

    source_map = _source_finding_map(findings_report.findings)
    open_blocking_ids = {
        finding.id for finding in findings_report.findings if finding.status == "open" and bool(finding.blocking)
    }
    arbitrated_ids = {finding.finding_id for finding in report.arbitrated_findings}

    for arbitrated in report.arbitrated_findings:
        source_finding = source_map.get(arbitrated.finding_id)
        if source_finding is None:
            raise ValueError(f"arbitrated finding_id not found in REVIEW_FINDINGS: {arbitrated.finding_id}")
        _validate_arbitrated_finding_against_source(arbitrated, source_finding=source_finding)

    missing_blocking = sorted(open_blocking_ids - arbitrated_ids)
    if missing_blocking:
        raise ValueError(
            "arbitration report must include all open blocking findings from REVIEW_FINDINGS: "
            + ", ".join(missing_blocking)
        )

    return _report_with_source_findings_path(
        report,
        source_findings_path=str((run_dir / "REVIEW_FINDINGS.json").resolve()),
    )


def build_review_arbitration_markdown(report: ReviewArbitrationReport) -> str:
    lines = [
        f"# Review Arbitration: {report.run_id}",
        "",
        f"Arbiter: `{report.arbiter}`",
        f"Overall decision: `{report.overall_decision}`",
        f"Created at: `{report.created_at.isoformat()}`",
        f"Source findings path: `{report.source_findings_path or '(none)'}`",
        "",
        "## Summary",
        "",
        report.summary,
        "",
        "## Counts",
        "",
        f"- total: `{report.counts.total}`",
        f"- upheld: `{report.counts.upheld}`",
        f"- downgraded: `{report.counts.downgraded}`",
        f"- upgraded: `{report.counts.upgraded}`",
        f"- dismissed: `{report.counts.dismissed}`",
        f"- needs_evidence: `{report.counts.needs_evidence}`",
        f"- conflict: `{report.counts.conflict}`",
        f"- accepted_risk: `{report.counts.accepted_risk}`",
        f"- final_blocking: `{report.counts.final_blocking}`",
        f"- human_escalation_required: `{report.counts.human_escalation_required}`",
        "",
        "## Arbitrated findings",
        "",
    ]
    if not report.arbitrated_findings:
        lines.extend(["No arbitrated findings recorded.", ""])
    else:
        for finding in report.arbitrated_findings:
            lines.extend(
                [
                    f"### {finding.finding_id}",
                    "",
                    f"- source_reviewer: `{finding.source_reviewer}`",
                    f"- severity: `{finding.original_severity}` -> `{finding.final_severity}`",
                    f"- blocking: `{str(finding.original_blocking).lower()}` -> `{str(finding.final_blocking).lower()}`",
                    f"- status: `{finding.status}`",
                    f"- deterministic_hard_gate: `{str(finding.deterministic_hard_gate).lower()}`",
                    f"- human_escalation_required: `{str(finding.human_escalation_required).lower()}`",
                    "",
                    "Reason:",
                    "",
                    finding.reason,
                    "",
                    "Final required action:",
                    "",
                    finding.final_required_action or "(none)",
                    "",
                ]
            )
    return "\n".join(lines)


def write_review_arbitration_artifacts(
    run_id: str,
    runs_dir: str | Path,
    report: ReviewArbitrationReport,
) -> tuple[Path, Path]:
    if report.run_id != run_id:
        raise ValueError(f"arbitration report run_id does not match target run id: {report.run_id} != {run_id}")
    run_dir = Path(runs_dir) / run_id
    json_path = run_dir / "REVIEW_ARBITRATION.json"
    markdown_path = run_dir / "REVIEW_ARBITRATION.md"
    json_payload = json.dumps(report.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n"
    json_path.write_text(json_payload, encoding="utf-8")
    markdown_path.write_text(build_review_arbitration_markdown(report) + "\n", encoding="utf-8")
    return json_path, markdown_path


def load_run_arbitration(run_dir: str | Path) -> ReviewArbitrationReport | None:
    json_path = Path(run_dir) / "REVIEW_ARBITRATION.json"
    if not json_path.exists():
        return None
    return ReviewArbitrationReport.model_validate_json(json_path.read_text(encoding="utf-8-sig"))


def persist_review_arbitration_report(
    *,
    run_id: str,
    runs_dir: str | Path,
    report: ReviewArbitrationReport,
    force: bool = False,
) -> RecordArbitrationResult:
    runs_dir_path = Path(runs_dir)
    run_dir = runs_dir_path / run_id
    if not run_dir.exists():
        raise FileNotFoundError(f"run does not exist: {run_id}")

    state = load_run_state(run_dir)
    if report.run_id != run_id:
        raise ValueError(f"arbitration report run_id mismatch: expected {run_id}, got {report.run_id}")

    json_path = run_dir / "REVIEW_ARBITRATION.json"
    markdown_path = run_dir / "REVIEW_ARBITRATION.md"
    if (json_path.exists() or markdown_path.exists()) and not force:
        raise ValueError("Review arbitration already recorded. Pass --force to overwrite it.")

    written_json_path, written_markdown_path = write_review_arbitration_artifacts(run_id, runs_dir_path, report)
    state.review_arbitration_path = str(written_json_path.resolve())
    state.review_arbitration_decision = report.overall_decision
    state.review_arbitration_final_blocking_count = report.counts.final_blocking
    state.review_arbitration_human_escalation_required = report.counts.human_escalation_required > 0
    state.review_arbitration_created_at = report.created_at
    state.touch()
    state.save_json(run_dir / "state.json")

    return RecordArbitrationResult(
        run_id=run_id,
        overall_decision=report.overall_decision,
        final_blocking_count=report.counts.final_blocking,
        human_escalation_required=report.counts.human_escalation_required > 0,
        review_arbitration_path=written_json_path.resolve(),
        review_arbitration_markdown_path=written_markdown_path.resolve(),
        state_path=(run_dir / "state.json").resolve(),
    )


def record_review_arbitration(
    *,
    run_id: str,
    runs_dir: str | Path,
    arbitration_file: str | Path,
    force: bool = False,
) -> RecordArbitrationResult:
    report = load_arbitration_file(arbitration_file)
    run_dir = Path(runs_dir) / run_id
    if report.run_id != run_id:
        raise ValueError(f"arbitration report run_id mismatch: expected {run_id}, got {report.run_id}")
    report = _validate_against_source_findings(report, run_dir=run_dir)
    return persist_review_arbitration_report(
        run_id=run_id,
        runs_dir=runs_dir,
        report=report,
        force=force,
    )
