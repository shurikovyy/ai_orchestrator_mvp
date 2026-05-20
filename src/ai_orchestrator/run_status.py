from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

from ai_orchestrator.review import load_run_state


@dataclass(frozen=True)
class RunStatusSummary:
    run_id: str
    validator_status: str
    backend: str | None
    human_review_decision: str | None
    acceptance_status: str
    is_rework: bool
    source_run_id: str | None
    feedback_present: bool
    final_report_exists: bool
    review_packet_exists: bool
    review_decision_exists: bool
    acceptance_exists: bool
    next_action: str
    artifacts: dict[str, str]
    exists: dict[str, bool]


def _bool_text(value: bool) -> str:
    return str(value).lower()


def _build_artifact_paths(run_dir: Path) -> dict[str, Path]:
    return {
        "state": run_dir / "state.json",
        "final_report": run_dir / "final_report.md",
        "review_packet": run_dir / "REVIEW_PACKET.md",
        "review_decision": run_dir / "REVIEW_DECISION.json",
        "review_decision_md": run_dir / "REVIEW_DECISION.md",
        "review_feedback": run_dir / "REVIEW_FEEDBACK.md",
        "rework_feedback": run_dir / "REWORK_FEEDBACK.md",
        "acceptance": run_dir / "ACCEPTANCE.md",
    }


def _compute_next_action(
    *,
    validator_status: str,
    human_review_decision: str | None,
    acceptance_exists: bool,
) -> str:
    if validator_status != "approved":
        return "rework_or_inspect_failure"
    if human_review_decision == "rejected":
        return "rework_run"
    if acceptance_exists:
        return "done"
    if human_review_decision == "approved":
        return "accept_run"
    return "review_run"


def build_run_status_summary(*, run_id: str, runs_dir: str | Path) -> RunStatusSummary:
    runs_dir_path = Path(runs_dir)
    run_dir = runs_dir_path / run_id
    state_path = run_dir / "state.json"
    if not run_dir.exists() or not state_path.exists():
        raise FileNotFoundError(f"run not found: {run_id}")

    state = load_run_state(run_dir)
    artifact_paths = _build_artifact_paths(run_dir)
    exists = {
        "final_report": artifact_paths["final_report"].exists(),
        "review_packet": artifact_paths["review_packet"].exists(),
        "review_decision": artifact_paths["review_decision"].exists(),
        "review_decision_md": artifact_paths["review_decision_md"].exists(),
        "review_feedback": artifact_paths["review_feedback"].exists(),
        "rework_feedback": artifact_paths["rework_feedback"].exists(),
        "acceptance": artifact_paths["acceptance"].exists(),
    }
    acceptance_exists = exists["acceptance"]
    human_review_decision = state.human_review_decision
    next_action = _compute_next_action(
        validator_status=state.final_status,
        human_review_decision=human_review_decision,
        acceptance_exists=acceptance_exists,
    )
    feedback_present = bool(
        state.human_review_feedback
        or state.task.rework_feedback
        or exists["review_feedback"]
        or exists["rework_feedback"]
    )

    return RunStatusSummary(
        run_id=state.run_id,
        validator_status=state.final_status,
        backend=state.backend_name,
        human_review_decision=human_review_decision,
        acceptance_status="accepted" if acceptance_exists else "not_accepted",
        is_rework=bool(state.task.rework_of_run_id),
        source_run_id=state.task.rework_of_run_id,
        feedback_present=feedback_present,
        final_report_exists=exists["final_report"],
        review_packet_exists=exists["review_packet"],
        review_decision_exists=exists["review_decision"],
        acceptance_exists=acceptance_exists,
        next_action=next_action,
        artifacts={name: str(path.resolve()) for name, path in artifact_paths.items()},
        exists=exists,
    )


def format_run_status_text(summary: RunStatusSummary, *, show_paths: bool = False) -> str:
    lines = [
        f"run_id={summary.run_id}",
        f"validator_status={summary.validator_status}",
        f"backend={summary.backend or ''}",
        f"human_review_decision={summary.human_review_decision or ''}",
        f"acceptance_status={summary.acceptance_status}",
        f"is_rework={_bool_text(summary.is_rework)}",
        f"source_run_id={summary.source_run_id or ''}",
        f"feedback_present={_bool_text(summary.feedback_present)}",
        f"final_report_exists={_bool_text(summary.final_report_exists)}",
        f"review_packet_exists={_bool_text(summary.review_packet_exists)}",
        f"review_decision_exists={_bool_text(summary.review_decision_exists)}",
        f"acceptance_exists={_bool_text(summary.acceptance_exists)}",
        f"next_action={summary.next_action}",
    ]
    if show_paths:
        lines.extend(
            [
                f"final_report={summary.artifacts['final_report']}",
                f"review_packet={summary.artifacts['review_packet']}",
                f"review_decision={summary.artifacts['review_decision']}",
                f"acceptance={summary.artifacts['acceptance']}",
                f"state={summary.artifacts['state']}",
            ]
        )
    return "\n".join(lines)


def format_run_status_json(summary: RunStatusSummary) -> str:
    payload = {
        "run_id": summary.run_id,
        "validator_status": summary.validator_status,
        "backend": summary.backend,
        "human_review_decision": summary.human_review_decision,
        "acceptance_status": summary.acceptance_status,
        "is_rework": summary.is_rework,
        "source_run_id": summary.source_run_id,
        "feedback_present": summary.feedback_present,
        "next_action": summary.next_action,
        "artifacts": summary.artifacts,
        "exists": summary.exists,
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)
