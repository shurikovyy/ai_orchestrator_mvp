from __future__ import annotations

from pathlib import Path

from ai_orchestrator.backends.base import Backend
from ai_orchestrator.schemas import ExecutionResult, Plan, PlanStep, TaskSpec, ValidationResult
from ai_orchestrator.validation import evaluate_structured_criterion, load_structured_report


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
        failed: list[str] = []
        feedback: list[str] = []

        structured = load_structured_report(result)
        structured_report = structured.report

        if result.status != "completed":
            failed.append("execution_status_completed")
            feedback.append("Execution status is not completed.")

        if structured.error:
            failed.append("valid_structured_execution_report")
            feedback.append(structured.error)
        elif structured_report is None:
            if task.require_structured_report:
                failed.append("structured_execution_report_present")
                feedback.append("Structured execution report is required, but EXECUTION_REPORT.json was not found.")
        else:
            feedback.append(f"Structured execution report parsed successfully from {structured.source}.")
            if structured_report.status != "completed":
                failed.append("report.status=completed")
                feedback.append(f"Structured report status is not completed: {structured_report.status}.")
            for test in structured_report.tests:
                if test.status != "passed":
                    failed.append(f"test_status_passed:{test.name}")
                    feedback.append(f"Test report `{test.name}` is not passed: {test.status}.")
            if task.require_structured_report and not structured_report.changed_files:
                failed.append("changed_files_non_empty")
                feedback.append("Structured report changed_files is empty.")
            if task.require_structured_report and not structured_report.commands_run:
                failed.append("commands_run_non_empty")
                feedback.append("Structured report commands_run is empty.")

            task_mentions_tests = "test" in task.description.lower() or any(
                "test" in criterion.lower() for criterion in step.acceptance_criteria
            )
            if task.require_structured_report and task_mentions_tests and not structured_report.tests:
                failed.append("tests_non_empty")
                feedback.append("Task appears to require tests, but structured report tests is empty.")

        for criterion in step.acceptance_criteria:
            if structured_report is not None:
                handled_ok, reason = evaluate_structured_criterion(criterion, structured_report)
                if reason is None:
                    if handled_ok:
                        continue
                    failed.append(criterion)
                    feedback.append(f"Structured criterion failed: {criterion}")
                    continue
                if reason != "UNHANDLED":
                    failed.append(criterion)
                    feedback.append(reason)
                    continue

            if criterion.lower() not in content_lower:
                failed.append(criterion)
                feedback.append(f"Missing criterion: {criterion}")

        explicit_total = len(step.acceptance_criteria)
        structural_total = 1 if (task.require_structured_report or structured_report is not None) else 0
        total = max(1, explicit_total + structural_total)
        # Internal structural failures are counted pessimistically. Duplicate
        # entries are removed for cleaner reports but still preserve order.
        deduped_failed = list(dict.fromkeys(failed))
        score = max(0.0, (total - len(deduped_failed)) / total)
        approved = not deduped_failed
        if approved:
            if structured_report is not None:
                feedback.append("Structured report and explicit acceptance criteria passed.")
            else:
                feedback.append("All explicit acceptance criteria are present in the artifact.")
        return ValidationResult(
            step_id=step.id,
            attempt=result.attempt,
            approved=approved,
            score=score,
            failed_criteria=deduped_failed,
            feedback=feedback,
        )
