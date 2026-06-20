"""Controlled submission helpers for human review decision artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


MAX_REVIEW_FEEDBACK_CHARS = 100_000


@dataclass(frozen=True)
class ReviewDecisionSubmission:
    decision: str
    normalized_feedback: str | None


def validate_review_decision_submission(
    *,
    decision: str | None,
    feedback: str | None,
) -> tuple[ReviewDecisionSubmission | None, str | None]:
    normalized_decision = (decision or "").strip().lower()
    if normalized_decision not in {"approved", "rejected"}:
        return None, "Review decision must be approved or rejected."

    if normalized_decision == "approved":
        return ReviewDecisionSubmission(decision=normalized_decision, normalized_feedback=None), None

    normalized_feedback = _normalize_feedback_text(feedback or "")
    if not normalized_feedback.strip():
        return None, "Feedback is required for rejected review decisions."
    if len(normalized_feedback) > MAX_REVIEW_FEEDBACK_CHARS:
        return None, f"Review feedback must be {MAX_REVIEW_FEEDBACK_CHARS} characters or fewer."
    return ReviewDecisionSubmission(decision=normalized_decision, normalized_feedback=normalized_feedback), None


def write_review_feedback_input_file(
    *,
    project_root: str | Path,
    run_id: str,
    normalized_feedback: str,
) -> tuple[str, Path]:
    inputs_dir = review_feedback_inputs_dir(project_root)
    inputs_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    input_id = f"{run_id}_{timestamp}_{uuid4().hex[:6]}.md"
    input_path = (inputs_dir / input_id).resolve()
    if not _is_relative_to(input_path, inputs_dir):
        raise ValueError("review feedback input file must remain under .web/review_feedback_inputs")
    input_path.write_text(normalized_feedback, encoding="utf-8")
    return input_id, input_path


def review_feedback_inputs_dir(project_root: str | Path) -> Path:
    return (Path(project_root) / ".web" / "review_feedback_inputs").resolve()


def _normalize_feedback_text(value: str) -> str:
    text = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    return text + "\n" if text else ""


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True
