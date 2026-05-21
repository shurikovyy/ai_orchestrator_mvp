from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

from ai_orchestrator.apply import load_run_state
from ai_orchestrator.schemas import ReviewFinding, ReviewFindingsReport


@dataclass(frozen=True)
class RecordFindingsResult:
    run_id: str
    findings_total: int
    overall_decision: str
    blocking_findings: int
    review_findings_path: Path
    review_findings_markdown_path: Path
    state_path: Path


def load_findings_file(path: str | Path) -> ReviewFindingsReport:
    findings_path = Path(path).expanduser().resolve()
    if not findings_path.exists():
        raise FileNotFoundError(f"findings file does not exist: {findings_path}")
    if not findings_path.is_file():
        raise ValueError(f"findings path is not a file: {findings_path}")
    return ReviewFindingsReport.model_validate_json(findings_path.read_text(encoding="utf-8-sig"))


def _finding_location(finding: ReviewFinding) -> str:
    if finding.file and finding.line is not None:
        return f"{finding.file}:{finding.line}"
    if finding.file:
        return finding.file
    return "(none)"


def summarize_findings(report: ReviewFindingsReport) -> str:
    return (
        f"total={report.counts.total} critical={report.counts.critical} major={report.counts.major} "
        f"minor={report.counts.minor} nit={report.counts.nit} blocking_open={report.counts.blocking_open} "
        f"accepted_risk={report.counts.accepted_risk} resolved={report.counts.resolved}"
    )


def has_blocking_findings(report: ReviewFindingsReport) -> bool:
    return report.counts.blocking_open > 0


def build_review_findings_markdown(report: ReviewFindingsReport) -> str:
    lines = [
        f"# Review Findings: {report.run_id}",
        "",
        f"Overall decision: `{report.overall_decision}`",
        f"Created at: `{report.created_at.isoformat()}`",
        "",
        "## Summary",
        "",
        report.summary,
        "",
        "## Counts",
        "",
        f"- total: `{report.counts.total}`",
        f"- critical: `{report.counts.critical}`",
        f"- major: `{report.counts.major}`",
        f"- minor: `{report.counts.minor}`",
        f"- nit: `{report.counts.nit}`",
        f"- blocking_open: `{report.counts.blocking_open}`",
        f"- accepted_risk: `{report.counts.accepted_risk}`",
        f"- resolved: `{report.counts.resolved}`",
        "",
        "## Findings",
        "",
    ]
    if not report.findings:
        lines.extend(["No findings recorded.", ""])
    else:
        for finding in report.findings:
            lines.extend(
                [
                    f"### {finding.id} - {finding.title}",
                    "",
                    f"- reviewer: `{finding.reviewer}`",
                    f"- severity: `{finding.severity}`",
                    f"- category: `{finding.category}`",
                    f"- status: `{finding.status}`",
                    f"- blocking: `{str(finding.blocking).lower()}`",
                    f"- location: `{_finding_location(finding)}`",
                    "",
                    "Evidence:",
                    "",
                    finding.evidence,
                    "",
                    "Required action:",
                    "",
                    finding.required_action or "(none)",
                    "",
                ]
            )
    return "\n".join(lines)


def write_review_findings_artifacts(
    run_id: str,
    runs_dir: str | Path,
    report: ReviewFindingsReport,
) -> tuple[Path, Path]:
    if report.run_id != run_id:
        raise ValueError(f"findings report run_id does not match target run id: {report.run_id} != {run_id}")
    run_dir = Path(runs_dir) / run_id
    json_path = run_dir / "REVIEW_FINDINGS.json"
    markdown_path = run_dir / "REVIEW_FINDINGS.md"
    json_payload = json.dumps(report.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n"
    json_path.write_text(json_payload, encoding="utf-8")
    markdown_path.write_text(build_review_findings_markdown(report) + "\n", encoding="utf-8")
    return json_path, markdown_path


def load_run_findings(run_dir: str | Path) -> ReviewFindingsReport | None:
    json_path = Path(run_dir) / "REVIEW_FINDINGS.json"
    if not json_path.exists():
        return None
    return ReviewFindingsReport.model_validate_json(json_path.read_text(encoding="utf-8-sig"))


def persist_review_findings_report(
    *,
    run_id: str,
    runs_dir: str | Path,
    report: ReviewFindingsReport,
    force: bool = False,
) -> RecordFindingsResult:
    runs_dir_path = Path(runs_dir)
    run_dir = runs_dir_path / run_id
    if not run_dir.exists():
        raise FileNotFoundError(f"run does not exist: {run_id}")

    state = load_run_state(run_dir)
    if report.run_id != run_id:
        raise ValueError(f"findings report run_id mismatch: expected {run_id}, got {report.run_id}")

    json_path = run_dir / "REVIEW_FINDINGS.json"
    markdown_path = run_dir / "REVIEW_FINDINGS.md"
    if (json_path.exists() or markdown_path.exists()) and not force:
        raise ValueError("Review findings already recorded. Pass --force to overwrite them.")

    written_json_path, written_markdown_path = write_review_findings_artifacts(run_id, runs_dir_path, report)
    state.review_findings_path = str(written_json_path.resolve())
    state.review_findings_decision = report.overall_decision
    state.review_findings_blocking_count = report.counts.blocking_open
    state.review_findings_created_at = report.created_at
    state.touch()
    state.save_json(run_dir / "state.json")

    return RecordFindingsResult(
        run_id=run_id,
        findings_total=report.counts.total,
        overall_decision=report.overall_decision,
        blocking_findings=report.counts.blocking_open,
        review_findings_path=written_json_path.resolve(),
        review_findings_markdown_path=written_markdown_path.resolve(),
        state_path=(run_dir / "state.json").resolve(),
    )


def record_review_findings(
    *,
    run_id: str,
    runs_dir: str | Path,
    findings_file: str | Path,
    force: bool = False,
) -> RecordFindingsResult:
    report = load_findings_file(findings_file)
    return persist_review_findings_report(
        run_id=run_id,
        runs_dir=runs_dir,
        report=report,
        force=force,
    )
