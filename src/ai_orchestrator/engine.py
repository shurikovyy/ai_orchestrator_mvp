from __future__ import annotations

from pathlib import Path

from ai_orchestrator.backends.base import Backend
from ai_orchestrator.review import write_review_packet
from ai_orchestrator.schemas import RunState, TaskSpec


class TaskExecutionEngine:
    """Deterministic plan -> execute -> validate -> rework engine."""

    def __init__(self, backend: Backend, runs_dir: Path) -> None:
        self.backend = backend
        self.runs_dir = runs_dir

    def run(self, task: TaskSpec) -> RunState:
        state = RunState(task=task)
        run_dir = self.runs_dir / state.run_id
        artifacts_dir = run_dir / "artifacts"
        state.save_json(run_dir / "state.json")

        plan = self.backend.plan(task)
        state.plan = plan
        state.final_status = "planned"
        state.touch()
        state.save_json(run_dir / "state.json")

        all_approved = True
        for step in plan.steps:
            previous_feedback: list[str] = []
            approved = False
            for attempt in range(1, task.max_retries + 2):
                state.final_status = "running"
                result = self.backend.execute_step(
                    task=task,
                    step=step,
                    attempt=attempt,
                    previous_feedback=previous_feedback,
                    artifacts_dir=artifacts_dir,
                )
                state.executions.append(result)
                validation = self.backend.validate_step(task=task, step=step, result=result)
                state.validations.append(validation)
                state.touch()
                state.save_json(run_dir / "state.json")
                if validation.approved:
                    approved = True
                    break
                previous_feedback = validation.feedback
            if not approved:
                all_approved = False

        state.final_status = "approved" if all_approved else "failed"
        final_report = self._write_final_report(state, run_dir)
        review_packet = write_review_packet(run_dir)
        # Make reports discoverable through synthetic execution notes.
        state.touch()
        state.save_json(run_dir / "state.json")
        (run_dir / "LATEST_RESULT.txt").write_text(str(final_report), encoding="utf-8")
        (run_dir / "LATEST_REVIEW_PACKET.txt").write_text(str(review_packet), encoding="utf-8")
        return state

    def _write_final_report(self, state: RunState, run_dir: Path) -> Path:
        report_path = run_dir / "final_report.md"
        lines = [
            f"# AI Orchestrator Run: {state.run_id}",
            "",
            f"Status: `{state.final_status}`",
            f"Backend: `{self.backend.name}`",
            "",
            "## Task",
            state.task.description,
            "",
            "## Plan",
        ]
        if state.plan:
            lines.append(state.plan.summary)
            for step in state.plan.steps:
                lines.extend([
                    "",
                    f"### {step.id}: {step.title}",
                    step.description,
                    "",
                    "Acceptance criteria:",
                    *[f"- {item}" for item in step.acceptance_criteria],
                ])
        lines.extend(["", "## Validation timeline"])
        for validation in state.validations:
            lines.extend([
                "",
                f"- step={validation.step_id}, attempt={validation.attempt}, approved={validation.approved}, score={validation.score:.2f}",
            ])
            for item in validation.feedback:
                lines.append(f"  - {item}")
        lines.extend(["", "## Review packet", f"- {run_dir / 'REVIEW_PACKET.md'}", "", "## Artifacts"])
        for execution in state.executions:
            for artifact in execution.artifact_paths:
                lines.append(f"- {artifact}")
        report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return report_path
