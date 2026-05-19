from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ai_orchestrator.review import load_run_state
from ai_orchestrator.schemas import RunState, TaskSpec
from ai_orchestrator.task_runner import RunCommandConfig, execute_run, get_run_artifact_paths


@dataclass(frozen=True)
class ReworkRunResult:
    source_run_id: str
    rework_run_id: str
    status: str
    backend_name: str
    final_report: Path
    review_packet: Path
    state_path: Path
    rework_feedback: Path


def load_source_run(runs_dir: Path, source_run_id: str) -> RunState:
    run_dir = runs_dir / source_run_id
    if not run_dir.exists():
        raise FileNotFoundError(f"source run does not exist: {source_run_id}")
    return load_run_state(run_dir)


def load_rework_feedback(feedback_path: str | Path) -> tuple[Path, str]:
    path = Path(feedback_path).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"feedback file does not exist: {path}")
    if not path.is_file():
        raise ValueError(f"feedback path is not a file: {path}")
    text = path.read_text(encoding="utf-8-sig")
    if not text.strip():
        raise ValueError(f"feedback file is empty: {path}")
    return path, text


def resolve_rework_feedback(
    source_state: RunState,
    *,
    source_run_dir: Path,
    feedback_path: str | Path | None,
) -> tuple[Path, str]:
    if feedback_path is not None:
        return load_rework_feedback(feedback_path)

    if source_state.human_review_decision == "rejected" and source_state.human_review_feedback:
        stored_feedback_path = (
            Path(source_state.human_review_feedback_path)
            if source_state.human_review_feedback_path
            else source_run_dir / "REVIEW_FEEDBACK.md"
        )
        return stored_feedback_path, source_state.human_review_feedback

    raise ValueError(
        "Feedback is required unless source run has a rejected human review decision with feedback."
    )


def resolve_rework_backend_name(source_state: RunState, backend_name_override: str | None) -> str:
    if backend_name_override:
        return backend_name_override
    if source_state.backend_name:
        return source_state.backend_name
    raise ValueError("Backend cannot be inferred from source run. Pass --backend.")


def build_rework_task(
    source_state: RunState,
    *,
    feedback_text: str,
    feedback_path: Path,
    max_retries: int | None = None,
) -> TaskSpec:
    update_fields = {
        "rework_of_run_id": source_state.run_id,
        "rework_feedback": feedback_text,
        "rework_feedback_path": str(feedback_path),
    }
    if max_retries is not None:
        update_fields["max_retries"] = max_retries
    payload = source_state.task.model_dump()
    payload.update(update_fields)
    return TaskSpec.model_validate(payload)


def copy_feedback_artifact(run_dir: Path, feedback_text: str) -> Path:
    feedback_copy_path = run_dir / "REWORK_FEEDBACK.md"
    feedback_copy_path.write_text(feedback_text, encoding="utf-8")
    return feedback_copy_path


def execute_rework_run(
    *,
    source_run_id: str,
    runs_dir: str | Path,
    feedback_path: str | Path | None = None,
    backend_name: str | None = None,
    codex_cmd: str | None = None,
    max_retries: int | None = None,
    verbose: bool = False,
    stream_codex_output: bool = False,
) -> ReworkRunResult:
    runs_dir_path = Path(runs_dir)
    source_run_dir = runs_dir_path / source_run_id
    source_state = load_source_run(runs_dir_path, source_run_id)
    resolved_backend_name = resolve_rework_backend_name(source_state, backend_name)
    resolved_feedback_path, feedback_text = resolve_rework_feedback(
        source_state,
        source_run_dir=source_run_dir,
        feedback_path=feedback_path,
    )
    task = build_rework_task(
        source_state,
        feedback_text=feedback_text,
        feedback_path=resolved_feedback_path,
        max_retries=max_retries,
    )
    config = RunCommandConfig(
        task=task,
        backend_name=resolved_backend_name,
        runs_dir=runs_dir_path,
        codex_cmd=codex_cmd,
        verbose=verbose,
        stream_codex_output=stream_codex_output,
    )
    state, backend = execute_run(config)
    run_dir = runs_dir_path / state.run_id
    feedback_copy_path = copy_feedback_artifact(run_dir, feedback_text)
    final_report, review_packet, state_path = get_run_artifact_paths(runs_dir_path, state.run_id)
    return ReworkRunResult(
        source_run_id=source_run_id,
        rework_run_id=state.run_id,
        status=state.final_status,
        backend_name=backend.name,
        final_report=final_report.resolve(),
        review_packet=review_packet.resolve(),
        state_path=state_path.resolve(),
        rework_feedback=feedback_copy_path.resolve(),
    )
