from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ai_orchestrator.apply import load_run_state
from ai_orchestrator.review_findings import ReviewFindingsReport, load_run_findings

_SEVERITY_PRIORITY = {
    "critical": 0,
    "major": 1,
    "minor": 2,
    "nit": 3,
}


@dataclass(frozen=True)
class FindingsFeedbackResult:
    run_id: str
    feedback_path: Path
    source_findings_path: Path
    findings_included: int
    blocking_findings_included: int
    state_path: Path
    reused_existing: bool


def _select_feedback_findings(
    report: ReviewFindingsReport,
    *,
    include_non_blocking: bool,
) -> list:
    selected = []
    for finding in report.findings:
        # accepted_risk is intentionally excluded from rework feedback even when
        # non-blocking findings are requested. It represents an explicitly
        # governed risk acceptance decision, not a rework instruction.
        if finding.status == "accepted_risk":
            continue
        if finding.status != "open":
            continue
        if not include_non_blocking and not finding.blocking:
            continue
        selected.append(finding)
    selected.sort(key=lambda finding: (_SEVERITY_PRIORITY[finding.severity], finding.id))
    return selected


def build_feedback_from_findings(
    report: ReviewFindingsReport,
    *,
    source_findings_path: str | Path,
    include_non_blocking: bool = False,
) -> tuple[str, int, int]:
    selected = _select_feedback_findings(report, include_non_blocking=include_non_blocking)
    if not selected:
        raise ValueError("no open findings available for feedback")

    blocking_count = sum(1 for finding in selected if finding.blocking)
    summary_line = (
        "The run has open blocking review findings. Rework is required before human approval."
        if blocking_count > 0
        else "The run has open review findings. Rework is recommended before human approval."
    )

    lines = [
        "# Rework Feedback From Review Findings",
        "",
        f"Source run: `{report.run_id}`",
        f"Source findings: `{Path(source_findings_path).resolve()}`",
        f"Generated at: `{datetime.now(timezone.utc).isoformat()}`",
        "",
        "## Summary",
        "",
        summary_line,
        "",
        "## Required fixes",
        "",
    ]
    for finding in selected:
        lines.extend(
            [
                f"### {finding.id} - {finding.title}",
                "",
                f"- Reviewer: `{finding.reviewer}`",
                f"- Severity: `{finding.severity}`",
                f"- Category: `{finding.category}`",
                f"- File: `{finding.file or '(none)'}`",
                f"- Line: `{finding.line if finding.line is not None else '(none)'}`",
                "- Evidence:",
                finding.evidence,
                "- Required action:",
                finding.required_action or "(none)",
                "",
            ]
        )
    lines.extend(
        [
            "## Instructions for rework executor",
            "",
            "Address every required action above.",
            "Do not ignore critical or major findings.",
            "Do not weaken validation, review, apply, or safety gates.",
            "If a finding cannot be fixed directly, explain the limitation in EXECUTION_REPORT.json risks/assumptions.",
            "Run the required tests and update EXECUTION_REPORT.json.",
            "",
        ]
    )
    return "\n".join(lines) + "\n", len(selected), blocking_count


def _default_feedback_path(run_dir: Path) -> Path:
    return run_dir / "REVIEW_FEEDBACK_FROM_FINDINGS.md"


def create_findings_feedback_for_run(
    *,
    run_id: str,
    runs_dir: str | Path,
    output_path: str | Path | None = None,
    force: bool = False,
    include_non_blocking: bool = False,
    reuse_existing: bool = False,
) -> FindingsFeedbackResult:
    runs_dir_path = Path(runs_dir)
    run_dir = runs_dir_path / run_id
    if not run_dir.exists():
        raise FileNotFoundError(f"run does not exist: {run_id}")

    report = load_run_findings(run_dir)
    if report is None:
        raise FileNotFoundError(f"review findings not found for run: {run_id}")

    source_findings_path = run_dir / "REVIEW_FINDINGS.json"
    target_path = (
        Path(output_path).expanduser().resolve()
        if output_path is not None
        else _default_feedback_path(run_dir).resolve()
    )

    markdown, findings_included, blocking_included = build_feedback_from_findings(
        report,
        source_findings_path=source_findings_path,
        include_non_blocking=include_non_blocking,
    )

    reused_existing = False
    if target_path.exists():
        if force:
            pass
        elif reuse_existing:
            reused_existing = True
        else:
            raise ValueError(f"findings feedback already exists: {target_path}. Pass --force to overwrite it.")

    if not reused_existing:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(markdown, encoding="utf-8")

    state = load_run_state(run_dir)
    state.findings_feedback_path = str(target_path)
    state.findings_feedback_created_at = datetime.now(timezone.utc)
    state.findings_feedback_source_path = str(source_findings_path.resolve())
    state.findings_feedback_count = findings_included
    state.touch()
    state_path = run_dir / "state.json"
    state.save_json(state_path)

    return FindingsFeedbackResult(
        run_id=run_id,
        feedback_path=target_path,
        source_findings_path=source_findings_path.resolve(),
        findings_included=findings_included,
        blocking_findings_included=blocking_included,
        state_path=state_path.resolve(),
        reused_existing=reused_existing,
    )
