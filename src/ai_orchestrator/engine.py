from __future__ import annotations

from pathlib import Path

from ai_orchestrator.backends.base import Backend
from ai_orchestrator.review import write_review_packet
from ai_orchestrator.schemas import RunState, TaskSpec


class TaskExecutionEngine:
    """Deterministic plan -> execute -> validate -> rework engine."""

    def __init__(self, backend: Backend, runs_dir: Path, *, verbose: bool = False) -> None:
        self.backend = backend
        self.runs_dir = runs_dir
        self.verbose = verbose

    def _log(self, message: str) -> None:
        if self.verbose:
            print(f"[orchestrator] {message}", flush=True)

    def run(self, task: TaskSpec) -> RunState:
        state = RunState(task=task)
        run_dir = self.runs_dir / state.run_id
        artifacts_dir = run_dir / "artifacts"
        self._log(f"created run_id={state.run_id}")
        state.save_json(run_dir / "state.json")

        self._log("planning task")
        plan = self.backend.plan(task)
        state.plan = plan
        state.final_status = "planned"
        state.touch()
        state.save_json(run_dir / "state.json")
        self._log(f"plan ready: {len(plan.steps)} step(s)")

        all_approved = True
        for step in plan.steps:
            previous_feedback: list[str] = []
            approved = False
            self._log(f"starting step={step.id} title={step.title!r}")
            for attempt in range(1, task.max_retries + 2):
                self._log(f"executing step={step.id} attempt={attempt}")
                state.final_status = "running"
                result = self.backend.execute_step(
                    task=task,
                    step=step,
                    attempt=attempt,
                    previous_feedback=previous_feedback,
                    artifacts_dir=artifacts_dir,
                )
                state.executions.append(result)
                self._log(f"execution finished step={step.id} attempt={attempt} status={result.status}")
                self._log(f"validating step={step.id} attempt={attempt}")
                validation = self.backend.validate_step(task=task, step=step, result=result)
                state.validations.append(validation)
                self._log(
                    f"validation finished step={step.id} attempt={attempt} "
                    f"approved={validation.approved} score={validation.score:.2f}"
                )
                state.touch()
                state.save_json(run_dir / "state.json")
                if validation.approved:
                    approved = True
                    break
                previous_feedback = validation.feedback
                if previous_feedback:
                    self._log("validator feedback: " + " | ".join(previous_feedback))
            if not approved:
                all_approved = False
                self._log(f"step={step.id} failed after retries; stopping remaining plan steps")
                break

        state.final_status = "approved" if all_approved else "failed"
        state.touch()
        # Save the final status before writing REVIEW_PACKET.md; the review packet
        # is built from state.json and must not show a stale `running` status.
        state.save_json(run_dir / "state.json")
        self._log(f"run finished status={state.final_status}; writing reports")
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
