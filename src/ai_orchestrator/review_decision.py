from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path

from ai_orchestrator.review import load_run_state
from ai_orchestrator.schemas import RunState


@dataclass(frozen=True)
class ReviewDecisionResult:
    run_id: str
    decision: str
    review_decision_path: Path
    review_feedback_path: Path | None
    state_path: Path


def _feedback_excerpt(feedback_text: str, *, limit: int = 1000) -> str:
    excerpt = feedback_text.strip().replace("\r\n", "\n")
    if len(excerpt) > limit:
        return excerpt[:limit].rstrip() + "\n... feedback excerpt clipped ..."
    return excerpt


def read_feedback_file(feedback_path: str | Path) -> tuple[Path, str]:
    path = Path(feedback_path).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"feedback file does not exist: {path}")
    if not path.is_file():
        raise ValueError(f"feedback path is not a file: {path}")
    text = path.read_text(encoding="utf-8-sig")
    if not text.strip():
        raise ValueError(f"feedback file is empty: {path}")
    return path, text


def load_review_target_run(runs_dir: Path, run_id: str) -> tuple[Path, RunState]:
    run_dir = runs_dir / run_id
    if not run_dir.exists():
        raise FileNotFoundError(f"run does not exist: {run_id}")
    state = load_run_state(run_dir)
    if state.final_status != "approved":
        raise ValueError("Only validator-approved runs can receive a human review decision.")
    return run_dir, state


def _write_feedback_copy(run_dir: Path, feedback_text: str) -> Path:
    feedback_path = run_dir / "REVIEW_FEEDBACK.md"
    feedback_path.write_text(feedback_text, encoding="utf-8")
    return feedback_path


def write_review_decision_artifacts(
    run_dir: Path,
    *,
    run_id: str,
    decision: str,
    decided_at: datetime,
    feedback_text: str | None,
    feedback_copy_path: Path | None,
) -> tuple[Path, Path]:
    json_path = run_dir / "REVIEW_DECISION.json"
    md_path = run_dir / "REVIEW_DECISION.md"
    excerpt = _feedback_excerpt(feedback_text or "")
    json_payload = {
        "schema_version": "1.0",
        "run_id": run_id,
        "decision": decision,
        "decided_at": decided_at.isoformat(),
        "feedback_path": str(feedback_copy_path.resolve()) if feedback_copy_path is not None else None,
        "feedback_excerpt": excerpt or "",
    }
    json_path.write_text(json.dumps(json_payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    lines = [
        f"# Review Decision: {run_id}",
        "",
        f"Decision: `{decision}`",
        f"Decided at: `{decided_at.isoformat()}`",
        "",
    ]
    if feedback_text is not None:
        lines.extend(
            [
                "## Feedback",
                "",
                feedback_text.strip(),
                "",
            ]
        )
    else:
        lines.extend(["No feedback was recorded for this decision.", ""])
    lines.extend(
        [
            "## Related artifacts",
            f"- `final_report.md`: `{(run_dir / 'final_report.md').resolve()}`",
            f"- `REVIEW_PACKET.md`: `{(run_dir / 'REVIEW_PACKET.md').resolve()}`",
            "",
            "## Next step",
        ]
    )
    if decision == "rejected":
        lines.extend(
            [
                "Run rework:",
                "",
                "```bash",
                f"python -m ai_orchestrator.cli rework-run {run_id} --runs-dir {run_dir.parent}",
                "```",
            ]
        )
    else:
        lines.extend(
            [
                "No accept-run or commit was performed by review-run.",
                "",
                "You may manually proceed to accept/apply/commit later.",
            ]
        )
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, md_path


def record_review_decision(
    *,
    run_id: str,
    runs_dir: str | Path,
    decision: str,
    feedback_path: str | Path | None = None,
    force: bool = False,
) -> ReviewDecisionResult:
    runs_dir_path = Path(runs_dir)
    run_dir, state = load_review_target_run(runs_dir_path, run_id)
    if state.human_review_decision and not force:
        raise ValueError("Human review decision already recorded. Pass --force to overwrite it.")

    normalized_decision = decision.strip().lower()
    if normalized_decision not in {"approved", "rejected"}:
        raise ValueError("decision must be one of: approved, rejected")

    if normalized_decision == "rejected" and feedback_path is None:
        raise ValueError("Feedback is required for rejected review decisions.")

    feedback_text: str | None = None
    feedback_copy_path: Path | None = None
    if feedback_path is not None:
        _source_feedback_path, feedback_text = read_feedback_file(feedback_path)
        feedback_copy_path = _write_feedback_copy(run_dir, feedback_text)

    decided_at = datetime.now(timezone.utc)
    review_decision_json_path, _review_decision_md_path = write_review_decision_artifacts(
        run_dir,
        run_id=run_id,
        decision=normalized_decision,
        decided_at=decided_at,
        feedback_text=feedback_text,
        feedback_copy_path=feedback_copy_path,
    )

    state.human_review_decision = normalized_decision
    state.human_review_decided_at = decided_at
    state.human_review_feedback = feedback_text
    state.human_review_feedback_path = str(feedback_copy_path.resolve()) if feedback_copy_path is not None else None
    state.human_review_decision_path = str(review_decision_json_path.resolve())
    state.touch()
    state.save_json(run_dir / "state.json")

    return ReviewDecisionResult(
        run_id=run_id,
        decision=normalized_decision,
        review_decision_path=review_decision_json_path.resolve(),
        review_feedback_path=feedback_copy_path.resolve() if feedback_copy_path is not None else None,
        state_path=(run_dir / "state.json").resolve(),
    )
