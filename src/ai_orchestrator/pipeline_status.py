from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

from ai_orchestrator.pipeline import PipelineState, PipelineTaskResult
from ai_orchestrator.run_status import RunStatusSummary, build_run_status_summary


@dataclass(frozen=True)
class PipelineTaskStatusSummary:
    task_id: str
    title: str | None
    run_id: str
    validator_status: str
    backend: str | None
    human_review_decision: str | None
    findings_exists: bool
    review_findings_decision: str
    blocking_findings: int
    findings_feedback_exists: bool
    findings_feedback_count: int
    acceptance_status: str
    application_status: str
    is_rework: bool
    source_run_id: str | None
    feedback_present: bool
    next_action: str
    artifacts: dict[str, str]
    exists: dict[str, bool]
    warning: str | None = None


@dataclass(frozen=True)
class PipelineStatusSummary:
    pipeline_id: str
    pipeline_status: str
    tasks_file: str
    next_action: str
    counts: dict[str, int]
    artifacts: dict[str, str]
    exists: dict[str, bool]
    tasks: list[PipelineTaskStatusSummary]
    warnings: list[str]


def _bool_text(value: bool) -> str:
    return str(value).lower()


def _pipeline_paths(runs_dir: Path, pipeline_id: str) -> dict[str, Path]:
    pipeline_dir = runs_dir / "pipelines" / pipeline_id
    return {
        "pipeline_dir": pipeline_dir,
        "pipeline_state": pipeline_dir / "pipeline_state.json",
        "pipeline_report": pipeline_dir / "PIPELINE_REPORT.md",
    }


def _run_artifact_paths_for_missing(task_result: PipelineTaskResult, runs_dir: Path) -> dict[str, str]:
    run_dir = runs_dir / task_result.run_id
    return {
        "state": task_result.state or str((run_dir / "state.json").resolve()),
        "final_report": task_result.final_report or str((run_dir / "final_report.md").resolve()),
        "review_packet": task_result.review_packet or str((run_dir / "REVIEW_PACKET.md").resolve()),
        "review_findings": str((run_dir / "REVIEW_FINDINGS.json").resolve()),
        "review_findings_markdown": str((run_dir / "REVIEW_FINDINGS.md").resolve()),
        "findings_feedback": str((run_dir / "REVIEW_FEEDBACK_FROM_FINDINGS.md").resolve()),
        "review_decision": str((run_dir / "REVIEW_DECISION.json").resolve()),
        "review_decision_md": str((run_dir / "REVIEW_DECISION.md").resolve()),
        "review_feedback": str((run_dir / "REVIEW_FEEDBACK.md").resolve()),
        "rework_feedback": str((run_dir / "REWORK_FEEDBACK.md").resolve()),
        "apply_report": str((run_dir / "APPLY_REPORT.md").resolve()),
        "apply_report_json": str((run_dir / "APPLY_REPORT.json").resolve()),
        "acceptance": str((run_dir / "ACCEPTANCE.md").resolve()),
    }


def _exists_from_artifacts(artifacts: dict[str, str]) -> dict[str, bool]:
    return {
        "final_report": Path(artifacts["final_report"]).exists(),
        "review_packet": Path(artifacts["review_packet"]).exists(),
        "review_findings": Path(artifacts["review_findings"]).exists(),
        "review_findings_markdown": Path(artifacts["review_findings_markdown"]).exists(),
        "findings_feedback": Path(artifacts["findings_feedback"]).exists(),
        "review_decision": Path(artifacts["review_decision"]).exists(),
        "review_decision_md": Path(artifacts["review_decision_md"]).exists(),
        "review_feedback": Path(artifacts["review_feedback"]).exists(),
        "rework_feedback": Path(artifacts["rework_feedback"]).exists(),
        "apply_report": Path(artifacts["apply_report"]).exists(),
        "apply_report_json": Path(artifacts["apply_report_json"]).exists(),
        "acceptance": Path(artifacts["acceptance"]).exists(),
    }


def _build_task_summary_from_run_summary(task_result: PipelineTaskResult, run_summary: RunStatusSummary) -> PipelineTaskStatusSummary:
    return PipelineTaskStatusSummary(
        task_id=task_result.task_id,
        title=task_result.title,
        run_id=task_result.run_id,
        validator_status=run_summary.validator_status,
        backend=run_summary.backend,
        human_review_decision=run_summary.human_review_decision,
        findings_exists=run_summary.findings_exists,
        review_findings_decision=run_summary.review_findings_decision,
        blocking_findings=run_summary.blocking_findings,
        findings_feedback_exists=run_summary.findings_feedback_exists,
        findings_feedback_count=run_summary.findings_feedback_count,
        acceptance_status=run_summary.acceptance_status,
        application_status=run_summary.application_status,
        is_rework=run_summary.is_rework,
        source_run_id=run_summary.source_run_id,
        feedback_present=run_summary.feedback_present,
        next_action=run_summary.next_action,
        artifacts=run_summary.artifacts,
        exists=run_summary.exists,
        warning=None,
    )


def _build_missing_task_summary(task_result: PipelineTaskResult, runs_dir: Path) -> PipelineTaskStatusSummary:
    artifacts = _run_artifact_paths_for_missing(task_result, runs_dir)
    return PipelineTaskStatusSummary(
        task_id=task_result.task_id,
        title=task_result.title,
        run_id=task_result.run_id,
        validator_status="missing",
        backend=None,
        human_review_decision=None,
        findings_exists=False,
        review_findings_decision="empty",
        blocking_findings=0,
        findings_feedback_exists=False,
        findings_feedback_count=0,
        acceptance_status="not_accepted",
        application_status="not_applied",
        is_rework=False,
        source_run_id=None,
        feedback_present=False,
        next_action="inspect_missing_run",
        artifacts=artifacts,
        exists=_exists_from_artifacts(artifacts),
        warning="run_missing",
    )


def _build_task_summary(task_result: PipelineTaskResult, runs_dir: Path) -> PipelineTaskStatusSummary:
    try:
        run_summary = build_run_status_summary(run_id=task_result.run_id, runs_dir=runs_dir)
    except FileNotFoundError:
        return _build_missing_task_summary(task_result, runs_dir)
    return _build_task_summary_from_run_summary(task_result, run_summary)


def _compute_counts(task_summaries: list[PipelineTaskStatusSummary]) -> dict[str, int]:
    return {
        "tasks_total": len(task_summaries),
        "tasks_validator_approved": sum(1 for task in task_summaries if task.validator_status == "approved"),
        "tasks_validator_failed": sum(1 for task in task_summaries if task.validator_status != "approved"),
        "tasks_waiting_review": sum(
            1 for task in task_summaries if task.validator_status == "approved" and task.human_review_decision is None
        ),
        "tasks_human_approved": sum(1 for task in task_summaries if task.human_review_decision == "approved"),
        "tasks_human_rejected": sum(1 for task in task_summaries if task.human_review_decision == "rejected"),
        "tasks_with_findings": sum(1 for task in task_summaries if task.findings_exists),
        "tasks_with_blocking_findings": sum(1 for task in task_summaries if task.blocking_findings > 0),
        "tasks_waiting_findings_feedback": sum(1 for task in task_summaries if task.next_action == "findings_feedback"),
        "tasks_waiting_rejected_review": sum(1 for task in task_summaries if task.next_action == "review_rejected"),
        "tasks_accepted": sum(1 for task in task_summaries if task.acceptance_status == "accepted"),
        "tasks_applied": sum(1 for task in task_summaries if task.application_status == "applied"),
        "tasks_waiting_apply": sum(1 for task in task_summaries if task.next_action == "apply_run"),
        "tasks_waiting_manual_commit": sum(1 for task in task_summaries if task.next_action == "manual_commit"),
    }


def _compute_pipeline_next_action(
    *,
    pipeline_status: str,
    task_summaries: list[PipelineTaskStatusSummary],
) -> str:
    if not task_summaries or any(not task.run_id for task in task_summaries):
        return "inspect_pipeline"
    if any(task.next_action == "inspect_missing_run" for task in task_summaries):
        return "inspect_pipeline"
    if pipeline_status in {"failed", "partial"} and any(task.validator_status != "approved" for task in task_summaries):
        return "rework_or_inspect_failure"
    if any(task.next_action == "rework_run" for task in task_summaries):
        return "rework_run"
    if any(task.next_action == "findings_feedback" for task in task_summaries):
        return "findings_feedback"
    if any(task.next_action == "review_rejected" for task in task_summaries):
        return "review_rejected"
    if any(task.next_action == "review_findings" for task in task_summaries):
        return "review_findings"
    if any(task.next_action == "review_run" for task in task_summaries):
        return "review_runs"
    if any(task.next_action == "apply_run" for task in task_summaries):
        return "apply_runs"
    if any(task.next_action == "manual_commit" for task in task_summaries):
        return "manual_commit"
    if task_summaries and all(task.next_action == "done" for task in task_summaries):
        return "done"
    return "inspect_pipeline"


def build_pipeline_status_summary(*, pipeline_id: str, runs_dir: str | Path) -> PipelineStatusSummary:
    runs_dir_path = Path(runs_dir)
    pipeline_paths = _pipeline_paths(runs_dir_path, pipeline_id)
    pipeline_state_path = pipeline_paths["pipeline_state"]
    if not pipeline_state_path.exists():
        raise FileNotFoundError(f"pipeline not found: {pipeline_id}")

    state = PipelineState.model_validate_json(pipeline_state_path.read_text(encoding="utf-8-sig"))
    task_summaries = [_build_task_summary(task_result, runs_dir_path) for task_result in state.tasks]
    warnings = [
        f"task `{task.task_id}` references missing run `{task.run_id}`"
        for task in task_summaries
        if task.warning == "run_missing"
    ]
    counts = _compute_counts(task_summaries)
    next_action = _compute_pipeline_next_action(pipeline_status=state.status, task_summaries=task_summaries)
    artifacts = {
        "pipeline_state": str(pipeline_paths["pipeline_state"].resolve()),
        "pipeline_report": str(pipeline_paths["pipeline_report"].resolve()),
    }
    exists = {
        "pipeline_report": pipeline_paths["pipeline_report"].exists(),
    }

    return PipelineStatusSummary(
        pipeline_id=state.pipeline_id,
        pipeline_status=state.status,
        tasks_file=state.tasks_file,
        next_action=next_action,
        counts=counts,
        artifacts=artifacts,
        exists=exists,
        tasks=task_summaries,
        warnings=warnings,
    )


def format_pipeline_status_text(summary: PipelineStatusSummary, *, show_paths: bool = False) -> str:
    lines = [
        f"pipeline_id={summary.pipeline_id}",
        f"pipeline_status={summary.pipeline_status}",
        f"tasks_file={summary.tasks_file}",
        f"tasks_total={summary.counts['tasks_total']}",
        f"tasks_validator_approved={summary.counts['tasks_validator_approved']}",
        f"tasks_validator_failed={summary.counts['tasks_validator_failed']}",
        f"tasks_waiting_review={summary.counts['tasks_waiting_review']}",
        f"tasks_human_approved={summary.counts['tasks_human_approved']}",
        f"tasks_human_rejected={summary.counts['tasks_human_rejected']}",
        f"tasks_with_findings={summary.counts['tasks_with_findings']}",
        f"tasks_with_blocking_findings={summary.counts['tasks_with_blocking_findings']}",
        f"tasks_waiting_findings_feedback={summary.counts['tasks_waiting_findings_feedback']}",
        f"tasks_waiting_rejected_review={summary.counts['tasks_waiting_rejected_review']}",
        f"tasks_accepted={summary.counts['tasks_accepted']}",
        f"tasks_applied={summary.counts['tasks_applied']}",
        f"tasks_waiting_apply={summary.counts['tasks_waiting_apply']}",
        f"tasks_waiting_manual_commit={summary.counts['tasks_waiting_manual_commit']}",
        f"next_action={summary.next_action}",
    ]
    if show_paths:
        lines.extend(
            [
                f"pipeline_state={summary.artifacts['pipeline_state']}",
                f"pipeline_report={summary.artifacts['pipeline_report']}",
            ]
        )
    for task in summary.tasks:
        parts = [
            f"task_id={task.task_id}",
            f"run_id={task.run_id}",
            f"backend={task.backend or ''}",
            f"validator_status={task.validator_status}",
            f"human_review_decision={task.human_review_decision or ''}",
            f"findings_exists={_bool_text(task.findings_exists)}",
            f"blocking_findings={task.blocking_findings}",
            f"findings_feedback_exists={_bool_text(task.findings_feedback_exists)}",
            f"findings_feedback_count={task.findings_feedback_count}",
            f"acceptance_status={task.acceptance_status}",
            f"application_status={task.application_status}",
            f"is_rework={_bool_text(task.is_rework)}",
            f"source_run_id={task.source_run_id or ''}",
            f"next_action={task.next_action}",
        ]
        if task.warning:
            parts.append(f"warning={task.warning}")
        lines.append(" ".join(parts))
    return "\n".join(lines)


def format_pipeline_status_json(summary: PipelineStatusSummary) -> str:
    payload = {
        "pipeline_id": summary.pipeline_id,
        "pipeline_status": summary.pipeline_status,
        "tasks_file": summary.tasks_file,
        "next_action": summary.next_action,
        "counts": summary.counts,
        "artifacts": summary.artifacts,
        "exists": summary.exists,
        "warnings": summary.warnings,
        "tasks": [
            {
                "task_id": task.task_id,
                "title": task.title,
                "run_id": task.run_id,
                "validator_status": task.validator_status,
                "backend": task.backend,
                "human_review_decision": task.human_review_decision,
                "findings_exists": task.findings_exists,
                "review_findings_decision": task.review_findings_decision,
                "blocking_findings": task.blocking_findings,
                "findings_feedback_exists": task.findings_feedback_exists,
                "findings_feedback_count": task.findings_feedback_count,
                "acceptance_status": task.acceptance_status,
                "application_status": task.application_status,
                "is_rework": task.is_rework,
                "source_run_id": task.source_run_id,
                "feedback_present": task.feedback_present,
                "next_action": task.next_action,
                "warning": task.warning,
                "artifacts": task.artifacts,
                "exists": task.exists,
            }
            for task in summary.tasks
        ],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)
