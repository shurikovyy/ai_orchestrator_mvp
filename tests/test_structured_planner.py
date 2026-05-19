from __future__ import annotations

from contextlib import contextmanager, redirect_stdout
from io import StringIO
from pathlib import Path
import json
import shutil
import sys
import textwrap
import unittest
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_orchestrator.backends.base import Backend
from ai_orchestrator.backends.mock import MockBackend
from ai_orchestrator.cli import run_task_main
from ai_orchestrator.engine import TaskExecutionEngine
from ai_orchestrator.schemas import ExecutionResult, PlanStep, TaskPlanStepSpec, TaskSpec, ValidationResult
from ai_orchestrator.task_queue import TaskQueueConfigError, load_task_queue_config, resolve_task_definition

TEST_TEMP_ROOT = Path(__file__).resolve().parents[1] / ".tmp_tests"
TEST_TEMP_ROOT.mkdir(exist_ok=True)


@contextmanager
def temporary_test_dir():
    path = TEST_TEMP_ROOT / f"tmp_{uuid4().hex}"
    path.mkdir(parents=True, exist_ok=False)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def write_text(path: Path, content: str) -> None:
    path.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")


def output_value(output: str, key: str) -> str:
    prefix = f"{key}="
    for line in output.splitlines():
        if line.startswith(prefix):
            return line[len(prefix):].strip()
    raise AssertionError(f"missing output line for {key!r} in:\n{output}")


class AlwaysFailStructuredBackend(Backend):
    name = "always_fail_structured"

    def __init__(self) -> None:
        self.executed_step_ids: list[str] = []

    def plan(self, task: TaskSpec):
        return MockBackend().plan(task)

    def execute_step(
        self,
        *,
        task: TaskSpec,
        step: PlanStep,
        attempt: int,
        previous_feedback: list[str],
        artifacts_dir: Path,
    ) -> ExecutionResult:
        del task, previous_feedback, artifacts_dir
        self.executed_step_ids.append(step.id)
        return ExecutionResult(
            step_id=step.id,
            attempt=attempt,
            status="completed",
            content=f"forced failure for {step.id} attempt {attempt}",
            artifact_paths=[],
            notes=[],
        )

    def validate_step(
        self,
        *,
        task: TaskSpec,
        step: PlanStep,
        result: ExecutionResult,
    ) -> ValidationResult:
        del task
        return ValidationResult(
            step_id=step.id,
            attempt=result.attempt,
            approved=False,
            score=0.0,
            failed_criteria=["forced failure"],
            feedback=["forced failure"],
        )


class StructuredTaskQueueTests(unittest.TestCase):
    def test_load_task_with_plan_steps_preserves_order_and_legacy_task_unchanged(self) -> None:
        with temporary_test_dir() as tmp:
            tasks_file = tmp / "tasks.yaml"
            write_text(
                tasks_file,
                """
                tasks:
                  - id: structured-task
                    prompt: |
                      Overall task description.
                    criteria:
                      - "final acceptance marker"
                    plan_steps:
                      - id: inspect
                        title: Inspect current state
                        description: |
                          Inspect current state.
                        criteria:
                          - "inspection summary"
                      - id: implement
                        description: |
                          Implement requested change.
                        criteria:
                          - "implementation completed"
                  - id: legacy-task
                    prompt: |
                      Legacy one-step task.
                    criteria:
                      - "legacy criterion"
                """,
            )

            config = load_task_queue_config(tasks_file)
            structured = resolve_task_definition(config, task_id="structured-task", tasks_file=tasks_file)
            legacy = resolve_task_definition(config, task_id="legacy-task", tasks_file=tasks_file)

        self.assertEqual([step.id for step in config.tasks[0].plan_steps], ["inspect", "implement"])
        self.assertEqual([step.id for step in structured.plan_steps], ["inspect", "implement"])
        self.assertEqual(structured.plan_steps[0].criteria, ["inspection summary"])
        self.assertEqual(structured.plan_steps[1].criteria, ["implementation completed"])
        self.assertEqual(legacy.criteria, ["legacy criterion"])
        self.assertEqual(legacy.plan_steps, [])

    def test_duplicate_plan_step_id_gives_clear_error(self) -> None:
        with temporary_test_dir() as tmp:
            tasks_file = tmp / "tasks.yaml"
            write_text(
                tasks_file,
                """
                tasks:
                  - id: structured-task
                    prompt: Overall task description.
                    plan_steps:
                      - id: inspect
                        description: First step.
                      - id: inspect
                        description: Duplicate step.
                """,
            )

            with self.assertRaisesRegex(
                TaskQueueConfigError,
                r"task `structured-task` has duplicate plan step id: inspect",
            ):
                load_task_queue_config(tasks_file)

    def test_missing_plan_step_id_gives_clear_error(self) -> None:
        with temporary_test_dir() as tmp:
            tasks_file = tmp / "tasks.yaml"
            write_text(
                tasks_file,
                """
                tasks:
                  - id: structured-task
                    prompt: Overall task description.
                    plan_steps:
                      - title: Inspect
                        description: First step.
                """,
            )

            with self.assertRaisesRegex(
                TaskQueueConfigError,
                r"task `structured-task` plan step at index 1 is missing required field: id",
            ):
                load_task_queue_config(tasks_file)

    def test_missing_plan_step_description_gives_clear_error(self) -> None:
        with temporary_test_dir() as tmp:
            tasks_file = tmp / "tasks.yaml"
            write_text(
                tasks_file,
                """
                tasks:
                  - id: structured-task
                    prompt: Overall task description.
                    plan_steps:
                      - id: inspect
                        title: Inspect
                """,
            )

            with self.assertRaisesRegex(
                TaskQueueConfigError,
                r"task `structured-task` plan step `inspect` is missing required field: description",
            ):
                load_task_queue_config(tasks_file)

    def test_plan_steps_criteria_must_be_list_of_strings(self) -> None:
        with temporary_test_dir() as tmp:
            tasks_file = tmp / "tasks.yaml"
            write_text(
                tasks_file,
                """
                tasks:
                  - id: structured-task
                    prompt: Overall task description.
                    plan_steps:
                      - id: inspect
                        description: First step.
                        criteria: "inspection summary"
                """,
            )

            with self.assertRaisesRegex(
                TaskQueueConfigError,
                r"task `structured-task` plan step `inspect` criteria must be a list of strings",
            ):
                load_task_queue_config(tasks_file)

    def test_plan_steps_must_be_list_if_provided(self) -> None:
        with temporary_test_dir() as tmp:
            tasks_file = tmp / "tasks.yaml"
            write_text(
                tasks_file,
                """
                tasks:
                  - id: structured-task
                    prompt: Overall task description.
                    plan_steps: inspect
                """,
            )

            with self.assertRaisesRegex(
                TaskQueueConfigError,
                r"task `structured-task` plan_steps must be a list",
            ):
                load_task_queue_config(tasks_file)


class StructuredPlannerTests(unittest.TestCase):
    def test_mock_backend_plan_returns_legacy_single_step_without_plan_steps(self) -> None:
        backend = MockBackend()
        task = TaskSpec(
            description="Create one artifact.",
            acceptance_criteria=["legacy criterion"],
        )

        plan = backend.plan(task)

        self.assertEqual(plan.summary, "Single-step MVP plan: produce an artifact, validate it, retry on failed criteria.")
        self.assertEqual(len(plan.steps), 1)
        self.assertEqual(plan.steps[0].id, "step_1")
        self.assertEqual(plan.steps[0].acceptance_criteria, ["legacy criterion"])

    def test_mock_backend_plan_returns_structured_steps_when_plan_steps_exist(self) -> None:
        backend = MockBackend()
        task = TaskSpec(
            description="Overall task description.",
            acceptance_criteria=["legacy criterion should not be copied"],
            plan_steps=[
                TaskPlanStepSpec(
                    id="inspect",
                    title="Inspect current state",
                    description="Inspect the repository.",
                    criteria=["inspection summary"],
                ),
                TaskPlanStepSpec(
                    id="verify",
                    description="Run verification.",
                    criteria=["tests.status=passed"],
                ),
            ],
        )

        plan = backend.plan(task)

        self.assertEqual(plan.summary, "Structured task-defined plan with 2 step(s).")
        self.assertEqual([step.id for step in plan.steps], ["inspect", "verify"])
        self.assertEqual(plan.steps[0].title, "Inspect current state")
        self.assertEqual(plan.steps[0].acceptance_criteria, ["inspection summary"])
        self.assertEqual(plan.steps[1].title, "verify")
        self.assertEqual(plan.steps[1].acceptance_criteria, ["tests.status=passed"])


class StructuredEngineTests(unittest.TestCase):
    def test_run_task_with_mock_backend_and_plan_steps_executes_all_steps(self) -> None:
        with temporary_test_dir() as tmp:
            tasks_file = tmp / "tasks.yaml"
            runs_dir = tmp / ".runs"
            write_text(
                tasks_file,
                """
                defaults:
                  backend: mock
                tasks:
                  - id: structured-mock
                    prompt: |
                      Overall task description.
                    plan_steps:
                      - id: inspect
                        title: Inspect current state
                        description: |
                          Inspect current state.
                        criteria:
                          - "inspection summary"
                      - id: report
                        title: Report result
                        description: |
                          Produce a final report.
                        criteria:
                          - "final report"
                """,
            )

            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = run_task_main(
                    ["structured-mock", "--tasks-file", str(tasks_file), "--runs-dir", str(runs_dir)]
                )
            output = stdout.getvalue()
            run_id = output_value(output, "run_id")
            state_path = Path(output_value(output, "state"))
            final_report_path = Path(output_value(output, "final_report"))
            review_packet_path = runs_dir / run_id / "REVIEW_PACKET.md"
            state_payload = json.loads(state_path.read_text(encoding="utf-8"))
            final_report = final_report_path.read_text(encoding="utf-8")
            review_packet_exists = review_packet_path.exists()

        self.assertEqual(exit_code, 0, output)
        self.assertEqual(state_payload["final_status"], "approved")
        self.assertEqual([step["id"] for step in state_payload["plan"]["steps"]], ["inspect", "report"])
        self.assertEqual([execution["step_id"] for execution in state_payload["executions"]], ["inspect", "report"])
        self.assertIn("### inspect: Inspect current state", final_report)
        self.assertIn("### report: Report result", final_report)
        self.assertTrue(review_packet_exists)

    def test_engine_stops_after_first_structured_step_fails_permanently(self) -> None:
        with temporary_test_dir() as tmp:
            backend = AlwaysFailStructuredBackend()
            task = TaskSpec(
                description="Overall task description.",
                max_retries=1,
                plan_steps=[
                    TaskPlanStepSpec(
                        id="inspect",
                        title="Inspect",
                        description="Inspect current state.",
                        criteria=["inspection summary"],
                    ),
                    TaskPlanStepSpec(
                        id="report",
                        title="Report",
                        description="Produce report.",
                        criteria=["final report"],
                    ),
                ],
            )

            state = TaskExecutionEngine(backend, tmp).run(task)

        self.assertEqual(state.final_status, "failed")
        self.assertEqual(backend.executed_step_ids, ["inspect", "inspect"])
        self.assertEqual([execution.step_id for execution in state.executions], ["inspect", "inspect"])
        self.assertEqual([validation.step_id for validation in state.validations], ["inspect", "inspect"])
        self.assertEqual([step.id for step in state.plan.steps], ["inspect", "report"])


if __name__ == "__main__":
    unittest.main()
