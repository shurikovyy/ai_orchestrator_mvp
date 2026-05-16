from __future__ import annotations

from pathlib import Path

from ai_orchestrator.backends.base import Backend
from ai_orchestrator.schemas import ExecutionResult, Plan, PlanStep, TaskSpec, ValidationResult


class MockBackend(Backend):
    """Offline backend for local tests and demo runs.

    It intentionally fails the first validation when acceptance criteria exist,
    then includes all criteria on the next attempt. This demonstrates the
    rework loop without calling external LLM APIs.
    """

    name = "mock"

    def plan(self, task: TaskSpec) -> Plan:
        criteria = task.acceptance_criteria or [
            "final artifact exists",
            "answer is structured",
            "no obvious placeholders",
        ]
        step = PlanStep(
            id="step_1",
            title="Create task artifact",
            description=task.description,
            assigned_role="executor",
            acceptance_criteria=criteria,
        )
        return Plan(
            task_id=task.id,
            summary="Single-step MVP plan: produce an artifact, validate it, retry on failed criteria.",
            steps=[step],
        )

    def execute_step(
        self,
        *,
        task: TaskSpec,
        step: PlanStep,
        attempt: int,
        previous_feedback: list[str],
        artifacts_dir: Path,
    ) -> ExecutionResult:
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        artifact_path = artifacts_dir / f"{step.id}_attempt_{attempt}.md"

        criteria_to_include = step.acceptance_criteria
        if attempt == 1 and len(criteria_to_include) > 1:
            # Deliberately omit the last criterion to prove the validation/rework cycle.
            criteria_to_include = criteria_to_include[:-1]

        feedback_block = "\n".join(f"- {item}" for item in previous_feedback) or "- none"
        criteria_block = "\n".join(f"- {item}" for item in criteria_to_include) or "- no explicit criteria"

        content = f"""# Result for: {step.title}

## Task
{task.description}

## Previous feedback
{feedback_block}

## Acceptance criteria satisfied
{criteria_block}

## Output
This is a deterministic demo artifact produced by the offline backend. Replace the backend with `codex_cli` or a CrewAI flow when you want real AI execution.
"""
        artifact_path.write_text(content, encoding="utf-8")
        return ExecutionResult(
            step_id=step.id,
            attempt=attempt,
            status="completed",
            content=content,
            artifact_paths=[str(artifact_path)],
            notes=["offline mock execution"],
        )

    def validate_step(
        self,
        *,
        task: TaskSpec,
        step: PlanStep,
        result: ExecutionResult,
    ) -> ValidationResult:
        content_lower = result.content.lower()
        failed = [criterion for criterion in step.acceptance_criteria if criterion.lower() not in content_lower]
        total = max(1, len(step.acceptance_criteria))
        score = (total - len(failed)) / total
        approved = result.status == "completed" and not failed
        feedback = []
        if result.status != "completed":
            feedback.append("Execution status is not completed.")
        feedback.extend(f"Missing criterion: {item}" for item in failed)
        if approved:
            feedback.append("All explicit acceptance criteria are present in the artifact.")
        return ValidationResult(
            step_id=step.id,
            attempt=result.attempt,
            approved=approved,
            score=score,
            failed_criteria=failed,
            feedback=feedback,
        )
