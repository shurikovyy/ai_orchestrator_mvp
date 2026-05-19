from __future__ import annotations

from contextlib import contextmanager, redirect_stdout
from io import StringIO
from pathlib import Path
import shutil
import sys
import unittest
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_orchestrator.cli import run_pipeline_main
from ai_orchestrator.task_queue import load_task_queue_config

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


class TaskQueueExampleTests(unittest.TestCase):
    def test_tasks_yaml_example_exists_and_loads(self) -> None:
        example_path = Path(__file__).resolve().parents[1] / "tasks.yaml.example"

        self.assertTrue(example_path.exists(), f"missing example file: {example_path}")
        config = load_task_queue_config(example_path)

        task_ids = [task.id for task in config.tasks]
        self.assertIn("mock-smoke", task_ids)
        self.assertIn("toy-fix", task_ids)
        self.assertIn("disabled-example", task_ids)
        self.assertIn("structured-plan-example", task_ids)

        mock_task = next(task for task in config.tasks if task.id == "mock-smoke")
        self.assertTrue(mock_task.enabled)

        toy_fix_task = next(task for task in config.tasks if task.id == "toy-fix")
        self.assertFalse(toy_fix_task.enabled)

        disabled_task = next(task for task in config.tasks if task.id == "disabled-example")
        self.assertFalse(disabled_task.enabled)

        structured_plan_task = next(task for task in config.tasks if task.id == "structured-plan-example")
        self.assertFalse(structured_plan_task.enabled)
        self.assertEqual([step.id for step in structured_plan_task.plan_steps], ["inspect", "report"])

    def test_run_pipeline_dry_run_with_tasks_yaml_example_succeeds_without_seed_workspace(self) -> None:
        example_path = Path(__file__).resolve().parents[1] / "tasks.yaml.example"

        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = run_pipeline_main(
                    ["--tasks-file", str(example_path), "--runs-dir", str(runs_dir), "--dry-run", "--verbose"]
                )
            output = stdout.getvalue()

        self.assertEqual(exit_code, 0, output)
        self.assertIn("dry_run=true", output)
        self.assertIn("planned_task=mock-smoke action=run", output)
        self.assertIn("planned_task=toy-fix action=skip_disabled", output)
        self.assertIn("planned_task=disabled-example action=skip_disabled", output)
        self.assertIn("planned_task=structured-plan-example action=skip_disabled", output)
        self.assertNotIn("error=", output)
        self.assertFalse((runs_dir / "pipelines").exists())
        self.assertFalse(runs_dir.exists())


if __name__ == "__main__":
    unittest.main()
