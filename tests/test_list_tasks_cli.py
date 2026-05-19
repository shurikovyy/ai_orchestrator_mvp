from __future__ import annotations

from contextlib import contextmanager, redirect_stdout
from io import StringIO
from pathlib import Path
import json
import os
import shutil
import sys
import unittest
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_orchestrator.cli import list_tasks_main

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_TEMP_ROOT = PROJECT_ROOT / ".tmp_tests"
TEST_TEMP_ROOT.mkdir(exist_ok=True)


@contextmanager
def temporary_test_dir():
    path = TEST_TEMP_ROOT / f"tmp_{uuid4().hex}"
    path.mkdir(parents=True, exist_ok=False)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@contextmanager
def temporary_cwd(path: Path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


def run_list_tasks(*args: str, cwd: Path) -> tuple[int, str]:
    stdout = StringIO()
    with temporary_cwd(cwd):
        with redirect_stdout(stdout):
            exit_code = list_tasks_main(list(args))
    return exit_code, stdout.getvalue()


class ListTasksCliTests(unittest.TestCase):
    def test_list_tasks_text_output_on_tasks_yaml_example_succeeds(self) -> None:
        example_path = PROJECT_ROOT / "tasks.yaml.example"

        with temporary_test_dir() as tmp:
            exit_code, output = run_list_tasks("--tasks-file", str(example_path), cwd=tmp)

            self.assertEqual(exit_code, 0, output)
            self.assertIn(f"tasks_file={example_path.resolve()}", output)
            self.assertIn("tasks_total=4", output)
            self.assertIn("tasks_enabled=1", output)
            self.assertIn("tasks_disabled=3", output)
            self.assertIn('task_id=mock-smoke enabled=true backend=mock title="Mock smoke test" seed_workspace=', output)
            self.assertIn(
                'task_id=toy-fix enabled=false backend=codex_cli title="Fix toy subtract bug" seed_workspace=toy_seed_project_0172',
                output,
            )
            self.assertIn(
                'task_id=disabled-example enabled=false backend=mock title="Disabled example task" seed_workspace=',
                output,
            )
            self.assertIn(
                'task_id=structured-plan-example enabled=false backend=mock title="Structured plan example" seed_workspace=',
                output,
            )
            self.assertLess(output.index("task_id=mock-smoke"), output.index("task_id=toy-fix"))
            self.assertLess(output.index("task_id=toy-fix"), output.index("task_id=disabled-example"))
            self.assertLess(output.index("task_id=disabled-example"), output.index("task_id=structured-plan-example"))
            self.assertFalse((tmp / ".runs").exists())
            self.assertFalse((tmp / ".runs" / "pipelines").exists())

    def test_list_tasks_does_not_require_toy_seed_project_0172_to_exist(self) -> None:
        example_source = PROJECT_ROOT / "tasks.yaml.example"

        with temporary_test_dir() as tmp:
            example_copy = tmp / "tasks.yaml.example"
            example_copy.write_text(example_source.read_text(encoding="utf-8"), encoding="utf-8")

            self.assertFalse((tmp / "toy_seed_project_0172").exists())
            exit_code, output = run_list_tasks("--tasks-file", str(example_copy), cwd=tmp)

            self.assertEqual(exit_code, 0, output)
            self.assertIn("task_id=toy-fix", output)
            self.assertNotIn("error=", output)
            self.assertFalse((tmp / ".runs").exists())

    def test_enabled_only_shows_only_enabled_tasks(self) -> None:
        example_path = PROJECT_ROOT / "tasks.yaml.example"

        with temporary_test_dir() as tmp:
            exit_code, output = run_list_tasks("--tasks-file", str(example_path), "--enabled-only", cwd=tmp)

            self.assertEqual(exit_code, 0, output)
            self.assertIn("tasks_total=1", output)
            self.assertIn("tasks_enabled=1", output)
            self.assertIn("tasks_disabled=0", output)
            self.assertIn("task_id=mock-smoke", output)
            self.assertNotIn("task_id=toy-fix", output)
            self.assertNotIn("task_id=disabled-example", output)
            self.assertNotIn("task_id=structured-plan-example", output)
            self.assertFalse((tmp / ".runs").exists())

    def test_disabled_only_shows_only_disabled_tasks(self) -> None:
        example_path = PROJECT_ROOT / "tasks.yaml.example"

        with temporary_test_dir() as tmp:
            exit_code, output = run_list_tasks("--tasks-file", str(example_path), "--disabled-only", cwd=tmp)

            self.assertEqual(exit_code, 0, output)
            self.assertIn("tasks_total=3", output)
            self.assertIn("tasks_enabled=0", output)
            self.assertIn("tasks_disabled=3", output)
            self.assertNotIn("task_id=mock-smoke", output)
            self.assertIn("task_id=toy-fix", output)
            self.assertIn("task_id=disabled-example", output)
            self.assertIn("task_id=structured-plan-example", output)
            self.assertFalse((tmp / ".runs").exists())

    def test_enabled_only_and_disabled_only_together_returns_clear_error(self) -> None:
        example_path = PROJECT_ROOT / "tasks.yaml.example"

        with temporary_test_dir() as tmp:
            exit_code, output = run_list_tasks(
                "--tasks-file",
                str(example_path),
                "--enabled-only",
                "--disabled-only",
                cwd=tmp,
            )

            self.assertEqual(exit_code, 1, output)
            self.assertIn("status=failed", output)
            self.assertIn("error=--enabled-only and --disabled-only cannot be used together", output)
            self.assertFalse((tmp / ".runs").exists())

    def test_format_json_returns_valid_json_with_expected_tasks(self) -> None:
        example_path = PROJECT_ROOT / "tasks.yaml.example"

        with temporary_test_dir() as tmp:
            exit_code, output = run_list_tasks("--tasks-file", str(example_path), "--format", "json", cwd=tmp)

            self.assertEqual(exit_code, 0, output)
            payload = json.loads(output)
            self.assertEqual(payload["tasks_file"], str(example_path.resolve()))
            self.assertEqual(payload["tasks_total"], 4)
            self.assertEqual(payload["tasks_enabled"], 1)
            self.assertEqual(payload["tasks_disabled"], 3)

            tasks = payload["tasks"]
            self.assertEqual(
                [task["id"] for task in tasks],
                ["mock-smoke", "toy-fix", "disabled-example", "structured-plan-example"],
            )
            self.assertEqual([task["enabled"] for task in tasks], [True, False, False, False])
            self.assertEqual(tasks[0]["backend"], "mock")
            self.assertIsNone(tasks[0]["seed_workspace"])
            self.assertEqual(tasks[0]["criteria_count"], 1)
            self.assertEqual(tasks[1]["backend"], "codex_cli")
            self.assertEqual(tasks[1]["seed_workspace"], "toy_seed_project_0172")
            self.assertEqual(tasks[3]["backend"], "mock")
            self.assertIsNone(tasks[3]["seed_workspace"])
            self.assertFalse((tmp / ".runs").exists())


if __name__ == "__main__":
    unittest.main()
