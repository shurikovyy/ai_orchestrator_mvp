from __future__ import annotations

from contextlib import contextmanager, redirect_stdout
from io import StringIO
from pathlib import Path
import json
import shutil
import sys
import textwrap
import unittest
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_orchestrator.cli import run_pipeline_main
from ai_orchestrator.pipeline import select_pipeline_tasks
from ai_orchestrator.task_queue import TaskQueueConfigError, load_task_queue_config

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


class PipelineTests(unittest.TestCase):
    def test_select_all_enabled_tasks_in_declaration_order(self) -> None:
        with temporary_test_dir() as tmp:
            tasks_file = tmp / "tasks.yaml"
            write_text(
                tasks_file,
                """
                tasks:
                  - id: task-a
                    prompt: First task
                  - id: task-b
                    prompt: Second task
                """,
            )

            config = load_task_queue_config(tasks_file)
            selection = select_pipeline_tasks(config, tasks_file=tasks_file)

        self.assertEqual(selection.task_ids, ["task-a", "task-b"])
        self.assertEqual(selection.enabled_task_ids, ["task-a", "task-b"])

    def test_disabled_task_is_skipped(self) -> None:
        with temporary_test_dir() as tmp:
            tasks_file = tmp / "tasks.yaml"
            write_text(
                tasks_file,
                """
                tasks:
                  - id: task-a
                    prompt: First task
                  - id: task-b
                    prompt: Disabled task
                    enabled: false
                  - id: task-c
                    prompt: Third task
                """,
            )

            config = load_task_queue_config(tasks_file)
            selection = select_pipeline_tasks(config, tasks_file=tasks_file)

        self.assertEqual(selection.task_ids, ["task-a", "task-b", "task-c"])
        self.assertEqual(selection.skipped_task_ids, ["task-b"])
        self.assertEqual([task.task_id for task in selection.tasks_to_run], ["task-a", "task-c"])

    def test_from_task_selects_task_and_following_tasks(self) -> None:
        with temporary_test_dir() as tmp:
            tasks_file = tmp / "tasks.yaml"
            write_text(
                tasks_file,
                """
                tasks:
                  - id: task-a
                    prompt: First task
                  - id: task-b
                    prompt: Second task
                  - id: task-c
                    prompt: Third task
                """,
            )

            config = load_task_queue_config(tasks_file)
            selection = select_pipeline_tasks(config, tasks_file=tasks_file, from_task="task-b")

        self.assertEqual(selection.task_ids, ["task-b", "task-c"])

    def test_from_task_missing_id_gives_clear_error(self) -> None:
        with temporary_test_dir() as tmp:
            tasks_file = tmp / "tasks.yaml"
            write_text(
                tasks_file,
                """
                tasks:
                  - id: task-a
                    prompt: First task
                """,
            )

            config = load_task_queue_config(tasks_file)
            with self.assertRaisesRegex(TaskQueueConfigError, "task id not found for --from-task: task-z"):
                select_pipeline_tasks(config, tasks_file=tasks_file, from_task="task-z")

    def test_only_selects_selected_ids_in_declaration_order(self) -> None:
        with temporary_test_dir() as tmp:
            tasks_file = tmp / "tasks.yaml"
            write_text(
                tasks_file,
                """
                tasks:
                  - id: task-a
                    prompt: First task
                  - id: task-b
                    prompt: Second task
                  - id: task-c
                    prompt: Third task
                """,
            )

            config = load_task_queue_config(tasks_file)
            selection = select_pipeline_tasks(config, tasks_file=tasks_file, only=["task-c", "task-a"])

        self.assertEqual(selection.task_ids, ["task-a", "task-c"])

    def test_only_missing_id_gives_clear_error(self) -> None:
        with temporary_test_dir() as tmp:
            tasks_file = tmp / "tasks.yaml"
            write_text(
                tasks_file,
                """
                tasks:
                  - id: task-a
                    prompt: First task
                """,
            )

            config = load_task_queue_config(tasks_file)
            with self.assertRaisesRegex(TaskQueueConfigError, "task id not found for --only: task-z"):
                select_pipeline_tasks(config, tasks_file=tasks_file, only=["task-a", "task-z"])

    def test_from_task_and_only_together_is_rejected(self) -> None:
        with temporary_test_dir() as tmp:
            tasks_file = tmp / "tasks.yaml"
            write_text(
                tasks_file,
                """
                tasks:
                  - id: task-a
                    prompt: First task
                """,
            )

            config = load_task_queue_config(tasks_file)
            with self.assertRaisesRegex(TaskQueueConfigError, "--from-task and --only cannot be used together"):
                select_pipeline_tasks(config, tasks_file=tasks_file, from_task="task-a", only=["task-a"])

    def test_dry_run_prints_planned_tasks_and_does_not_create_run_directories(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            tasks_file = tmp / "tasks.yaml"
            write_text(
                tasks_file,
                """
                defaults:
                  backend: mock
                tasks:
                  - id: task-a
                    prompt: First task
                  - id: task-b
                    prompt: Disabled task
                    enabled: false
                """,
            )

            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = run_pipeline_main(
                    ["--tasks-file", str(tasks_file), "--runs-dir", str(runs_dir), "--dry-run"]
                )
            output = stdout.getvalue()

        self.assertEqual(exit_code, 0, output)
        self.assertIn("dry_run=true", output)
        self.assertIn("planned_task=task-a action=run", output)
        self.assertIn("planned_task=task-b action=skip_disabled", output)
        self.assertNotIn("pipeline_id=", output)
        self.assertFalse(runs_dir.exists())

    def test_run_pipeline_with_mock_backend_runs_two_tasks_and_creates_pipeline_state_json(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            tasks_file = tmp / "tasks.yaml"
            write_text(
                tasks_file,
                """
                defaults:
                  backend: mock
                tasks:
                  - id: task-a
                    title: First task
                    prompt: |
                      Create a deterministic demo artifact for task A.
                    criteria:
                      - "deterministic demo artifact"
                  - id: task-b
                    title: Second task
                    prompt: |
                      Create a deterministic demo artifact for task B.
                    criteria:
                      - "deterministic demo artifact"
                """,
            )

            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = run_pipeline_main(
                    ["--tasks-file", str(tasks_file), "--runs-dir", str(runs_dir), "--verbose"]
                )
            output = stdout.getvalue()
            pipeline_state_path = Path(output_value(output, "pipeline_state"))
            pipeline_report_path = Path(output_value(output, "pipeline_report"))
            payload = json.loads(pipeline_state_path.read_text(encoding="utf-8"))
            self.assertEqual(exit_code, 0, output)
            self.assertIn("task_id=task-a run_id=", output)
            self.assertIn("task_id=task-b run_id=", output)
            self.assertEqual(payload["status"], "approved")
            self.assertEqual([task["task_id"] for task in payload["tasks"]], ["task-a", "task-b"])
            self.assertTrue(pipeline_state_path.exists())
            self.assertTrue(pipeline_report_path.exists())

    def test_run_pipeline_with_mock_backend_creates_pipeline_report_md(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            tasks_file = tmp / "tasks.yaml"
            write_text(
                tasks_file,
                """
                defaults:
                  backend: mock
                tasks:
                  - id: task-a
                    title: First task
                    prompt: First task prompt
                  - id: task-b
                    title: Second task
                    prompt: Second task prompt
                """,
            )

            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = run_pipeline_main(["--tasks-file", str(tasks_file), "--runs-dir", str(runs_dir)])
            output = stdout.getvalue()
            report_path = Path(output_value(output, "pipeline_report"))
            report_text = report_path.read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0, output)
        self.assertIn("Pipeline Report:", report_text)
        self.assertIn("| `task-a` | First task | `approved` |", report_text)
        self.assertIn("| `task-b` | Second task | `approved` |", report_text)
        self.assertIn("No accept-run or commit was performed by run-pipeline.", report_text)

    def test_run_pipeline_stops_on_first_failed_task_by_default(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            tasks_file = tmp / "tasks.yaml"
            write_text(
                tasks_file,
                """
                defaults:
                  backend: mock
                tasks:
                  - id: task-a
                    prompt: First successful task
                    criteria:
                      - "deterministic demo artifact"
                  - id: task-b
                    prompt: This task requires a structured report and must fail on mock backend
                    require_structured_report: true
                  - id: task-c
                    prompt: Third task should not run
                    criteria:
                      - "deterministic demo artifact"
                """,
            )

            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = run_pipeline_main(
                    ["--tasks-file", str(tasks_file), "--runs-dir", str(runs_dir), "--verbose"]
                )
            output = stdout.getvalue()
            pipeline_state_path = Path(output_value(output, "pipeline_state"))
            payload = json.loads(pipeline_state_path.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 1, output)
        self.assertIn("status=partial", output)
        self.assertIn("[pipeline] stopping on failed task=task-b", output)
        self.assertEqual(payload["status"], "partial")
        self.assertEqual([task["task_id"] for task in payload["tasks"]], ["task-a", "task-b"])
        self.assertEqual(payload["not_run_task_ids"], ["task-c"])

    def test_run_pipeline_continues_with_continue_on_failure(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            tasks_file = tmp / "tasks.yaml"
            write_text(
                tasks_file,
                """
                defaults:
                  backend: mock
                tasks:
                  - id: task-a
                    prompt: First successful task
                    criteria:
                      - "deterministic demo artifact"
                  - id: task-b
                    prompt: This task requires a structured report and must fail on mock backend
                    require_structured_report: true
                  - id: task-c
                    prompt: Third task should still run
                    criteria:
                      - "deterministic demo artifact"
                """,
            )

            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = run_pipeline_main(
                    [
                        "--tasks-file",
                        str(tasks_file),
                        "--runs-dir",
                        str(runs_dir),
                        "--continue-on-failure",
                    ]
                )
            output = stdout.getvalue()
            pipeline_state_path = Path(output_value(output, "pipeline_state"))
            payload = json.loads(pipeline_state_path.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 1, output)
        self.assertIn("status=failed", output)
        self.assertEqual(payload["status"], "failed")
        self.assertEqual([task["task_id"] for task in payload["tasks"]], ["task-a", "task-b", "task-c"])
        self.assertEqual(payload["not_run_task_ids"], [])

    def test_run_pipeline_does_not_call_accept_run_and_does_not_commit(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            tasks_file = tmp / "tasks.yaml"
            write_text(
                tasks_file,
                """
                defaults:
                  backend: mock
                tasks:
                  - id: task-a
                    prompt: First task
                  - id: task-b
                    prompt: Second task
                """,
            )

            stdout = StringIO()
            with patch("ai_orchestrator.cli.accept_run", side_effect=AssertionError("accept_run must not be called")) as accept_mock:
                with patch("ai_orchestrator.apply._run_git", side_effect=AssertionError("_run_git must not be called")) as git_mock:
                    with redirect_stdout(stdout):
                        exit_code = run_pipeline_main(
                            ["--tasks-file", str(tasks_file), "--runs-dir", str(runs_dir)]
                        )
            output = stdout.getvalue()

        self.assertEqual(exit_code, 0, output)
        accept_mock.assert_not_called()
        git_mock.assert_not_called()
        self.assertEqual(list(runs_dir.rglob("ACCEPTANCE.md")), [])


if __name__ == "__main__":
    unittest.main()
