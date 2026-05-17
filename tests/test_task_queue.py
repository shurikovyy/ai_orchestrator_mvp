from __future__ import annotations

from contextlib import contextmanager, redirect_stdout
from io import StringIO
from pathlib import Path
import shutil
import sys
import textwrap
import unittest
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_orchestrator.cli import build_run_task_command_config_from_args, build_run_task_parser, run_task_main
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


class TaskQueueTests(unittest.TestCase):
    def test_load_tasks_yaml_with_defaults_and_tasks(self) -> None:
        with temporary_test_dir() as tmp:
            tasks_file = tmp / "tasks.yaml"
            write_text(
                tasks_file,
                """
                project: ai_orchestrator_mvp
                defaults:
                  backend: codex_cli
                  max_retries: 2
                  require_structured_report: true
                  rerun_report_test_commands: true
                  validate_workspace_manifest: true
                  validation_command_timeout: 60
                  stream_codex_output: true
                  verbose: true
                  codex_cmd: codex
                tasks:
                  - id: "0.1.8"
                    title: "Add task queue runner"
                    prompt: |
                      Implement task queue support.
                    criteria:
                      - "report.status=completed"
                    commit_message: "feat: add task queue runner"
                  - id: "toy-fix"
                    prompt: |
                      Fix the toy bug.
                    seed_workspace: null
                """,
            )

            config = load_task_queue_config(tasks_file)

        self.assertEqual(config.project, "ai_orchestrator_mvp")
        self.assertEqual(config.defaults.backend, "codex_cli")
        self.assertEqual(config.defaults.max_retries, 2)
        self.assertTrue(config.defaults.require_structured_report)
        self.assertEqual(len(config.tasks), 2)
        self.assertEqual(config.tasks[0].id, "0.1.8")
        self.assertEqual(config.tasks[0].title, "Add task queue runner")
        self.assertEqual(config.tasks[0].criteria, ["report.status=completed"])
        self.assertEqual(config.tasks[0].commit_message, "feat: add task queue runner")
        self.assertEqual(config.tasks[1].id, "toy-fix")
        self.assertIsNone(config.tasks[1].seed_workspace)

    def test_cli_task_config_merges_defaults_and_task_fields(self) -> None:
        with temporary_test_dir() as tmp:
            seed = tmp / "seed_workspace"
            seed.mkdir()
            tasks_file = tmp / "tasks.yaml"
            write_text(
                tasks_file,
                f"""
                defaults:
                  backend: codex_cli
                  max_retries: 2
                  require_structured_report: true
                  validate_workspace_manifest: true
                  validation_command_timeout: 45
                  verbose: false
                tasks:
                  - id: merge-task
                    title: Merge task
                    prompt: |
                      Merge defaults with task-specific values.
                    criteria:
                      - "report.status=completed"
                    backend: mock
                    max_retries: 4
                    rerun_report_test_commands: true
                    seed_workspace: ./seed_workspace
                    commit_message: "feat: merge config"
                """,
            )

            args = build_run_task_parser().parse_args(
                ["merge-task", "--tasks-file", str(tasks_file), "--runs-dir", str(tmp / ".runs")]
            )
            task_id, config = build_run_task_command_config_from_args(args)

        self.assertEqual(task_id, "merge-task")
        self.assertEqual(config.backend_name, "mock")
        self.assertEqual(config.task.id, "merge-task")
        self.assertEqual(config.task.title, "Merge task")
        self.assertEqual(config.task.description, "Merge defaults with task-specific values.")
        self.assertEqual(config.task.acceptance_criteria, ["report.status=completed"])
        self.assertEqual(config.task.max_retries, 4)
        self.assertTrue(config.task.require_structured_report)
        self.assertTrue(config.task.rerun_report_test_commands)
        self.assertTrue(config.task.validate_workspace_manifest)
        self.assertEqual(config.task.validation_command_timeout_seconds, 45)
        self.assertEqual(config.task.seed_workspace_path, str(seed.resolve()))
        self.assertEqual(config.task.commit_message, "feat: merge config")

    def test_cli_overrides_have_priority_over_task_and_defaults(self) -> None:
        with temporary_test_dir() as tmp:
            tasks_file = tmp / "tasks.yaml"
            write_text(
                tasks_file,
                """
                defaults:
                  backend: mock
                  max_retries: 1
                  stream_codex_output: false
                  verbose: false
                  codex_cmd: defaults-codex
                tasks:
                  - id: override-task
                    prompt: |
                      Override backend settings from the CLI.
                    backend: codex_cli
                    max_retries: 4
                    stream_codex_output: false
                    verbose: false
                    codex_cmd: task-codex
                """,
            )

            args = build_run_task_parser().parse_args(
                [
                    "override-task",
                    "--tasks-file",
                    str(tasks_file),
                    "--backend",
                    "mock",
                    "--codex-cmd",
                    "cli-codex",
                    "--max-retries",
                    "9",
                    "--verbose",
                    "--stream-codex-output",
                ]
            )
            _, config = build_run_task_command_config_from_args(args)

        self.assertEqual(config.backend_name, "mock")
        self.assertEqual(config.codex_cmd, "cli-codex")
        self.assertEqual(config.task.max_retries, 9)
        self.assertTrue(config.verbose)
        self.assertTrue(config.stream_codex_output)

    def test_duplicate_task_id_produces_clear_error(self) -> None:
        with temporary_test_dir() as tmp:
            tasks_file = tmp / "tasks.yaml"
            write_text(
                tasks_file,
                """
                tasks:
                  - id: dup
                    prompt: first
                  - id: dup
                    prompt: second
                """,
            )

            with self.assertRaisesRegex(TaskQueueConfigError, "duplicate task id: dup"):
                load_task_queue_config(tasks_file)

    def test_missing_task_id_produces_clear_error(self) -> None:
        with temporary_test_dir() as tmp:
            tasks_file = tmp / "tasks.yaml"
            write_text(
                tasks_file,
                """
                tasks:
                  - id: only-task
                    prompt: do the work
                """,
            )

            config = load_task_queue_config(tasks_file)
            with self.assertRaisesRegex(TaskQueueConfigError, "task id not found: missing-task"):
                resolve_task_definition(config, task_id="missing-task", tasks_file=tasks_file)

    def test_missing_prompt_produces_clear_error(self) -> None:
        with temporary_test_dir() as tmp:
            tasks_file = tmp / "tasks.yaml"
            write_text(
                tasks_file,
                """
                tasks:
                  - id: missing-prompt
                    title: Missing prompt
                """,
            )

            with self.assertRaisesRegex(TaskQueueConfigError, "task `missing-prompt` is missing required field: prompt"):
                load_task_queue_config(tasks_file)

    def test_criteria_must_be_list_of_strings(self) -> None:
        with temporary_test_dir() as tmp:
            tasks_file = tmp / "tasks.yaml"
            write_text(
                tasks_file,
                """
                tasks:
                  - id: bad-criteria
                    prompt: Do work
                    criteria: "report.status=completed"
                """,
            )

            with self.assertRaisesRegex(TaskQueueConfigError, "criteria must be a list of strings"):
                load_task_queue_config(tasks_file)

    def test_relative_seed_workspace_resolves_relative_to_tasks_file_location(self) -> None:
        with temporary_test_dir() as tmp:
            config_dir = tmp / "configs"
            seed_dir = tmp / "toy_seed"
            config_dir.mkdir()
            seed_dir.mkdir()
            tasks_file = config_dir / "tasks.yaml"
            write_text(
                tasks_file,
                """
                tasks:
                  - id: seeded-task
                    prompt: Use a relative seed workspace path.
                    seed_workspace: ../toy_seed
                """,
            )

            config = load_task_queue_config(tasks_file)
            resolved = resolve_task_definition(config, task_id="seeded-task", tasks_file=tasks_file)

        self.assertEqual(resolved.seed_workspace, str(seed_dir.resolve()))

    def test_missing_seed_workspace_produces_clear_error(self) -> None:
        with temporary_test_dir() as tmp:
            tasks_file = tmp / "tasks.yaml"
            write_text(
                tasks_file,
                """
                tasks:
                  - id: missing-seed
                    prompt: Try to use a missing seed workspace.
                    seed_workspace: ./does-not-exist
                """,
            )

            config = load_task_queue_config(tasks_file)
            with self.assertRaisesRegex(FileNotFoundError, r"task `missing-seed` seed_workspace does not exist: .*does-not-exist"):
                resolve_task_definition(config, task_id="missing-seed", tasks_file=tasks_file)

    def test_run_task_with_mock_backend_creates_an_approved_run(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            tasks_file = tmp / "tasks.yaml"
            write_text(
                tasks_file,
                """
                defaults:
                  backend: mock
                  verbose: true
                tasks:
                  - id: mock-run
                    prompt: |
                      Create a deterministic mock artifact.
                    criteria:
                      - "deterministic demo artifact"
                """,
            )

            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = run_task_main(
                    ["mock-run", "--tasks-file", str(tasks_file), "--runs-dir", str(runs_dir)]
                )
            output = stdout.getvalue()
            run_id = output_value(output, "run_id")
            self.assertEqual(exit_code, 0, output)
            self.assertIn("task_id=mock-run", output)
            self.assertIn("status=approved", output)
            self.assertIn("backend=mock", output)
            self.assertTrue((runs_dir / run_id / "final_report.md").exists())
            self.assertTrue((runs_dir / run_id / "REVIEW_PACKET.md").exists())
            self.assertTrue((runs_dir / run_id / "state.json").exists())

    def test_run_task_does_not_commit(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            tasks_file = tmp / "tasks.yaml"
            write_text(
                tasks_file,
                """
                defaults:
                  backend: mock
                tasks:
                  - id: no-commit
                    prompt: |
                      Produce a simple artifact without committing anything.
                    criteria:
                      - "deterministic demo artifact"
                    commit_message: "feat: should stay metadata only"
                """,
            )

            stdout = StringIO()
            with patch("ai_orchestrator.cli.accept_run", side_effect=AssertionError("accept_run must not be called")) as accept_mock:
                with redirect_stdout(stdout):
                    exit_code = run_task_main(
                        ["no-commit", "--tasks-file", str(tasks_file), "--runs-dir", str(runs_dir)]
                    )
            output = stdout.getvalue()
            run_id = output_value(output, "run_id")
            self.assertEqual(exit_code, 0, output)
            accept_mock.assert_not_called()
            self.assertFalse((runs_dir / run_id / "ACCEPTANCE.md").exists())


if __name__ == "__main__":
    unittest.main()
