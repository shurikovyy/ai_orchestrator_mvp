from __future__ import annotations

from contextlib import contextmanager, redirect_stdout
from io import StringIO
from pathlib import Path
import json
import shutil
import subprocess
import sys
import textwrap
import unittest
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_orchestrator.backends.mock import MockBackend
from ai_orchestrator.apply import apply_run
from ai_orchestrator.cli import show_pipeline_main
from ai_orchestrator.engine import TaskExecutionEngine
from ai_orchestrator.pipeline import PipelineSelectedTask, PipelineState, PipelineTaskResult
from ai_orchestrator.review_decision import record_review_decision
from ai_orchestrator.schemas import ExecutionResult, RunState, TaskSpec, ValidationResult

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


def output_value(output: str, key: str) -> str:
    prefix = f"{key}="
    for line in output.splitlines():
        if line.startswith(prefix):
            return line[len(prefix):].strip()
    raise AssertionError(f"missing output line for {key!r} in:\n{output}")


def write_text(path: Path, content: str) -> None:
    path.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")


def git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(repo), *args], text=True, capture_output=True, check=True)


def make_git_seed_repo(root: Path) -> Path:
    repo = root / "seed_repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "toy_calc.py").write_text(
        "def subtract(a, b):\n    return a + b\n", encoding="utf-8"
    )
    subprocess.run(["git", "init", str(repo)], text=True, capture_output=True, check=True)
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test User")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "seed")
    return repo


def make_approved_accept_run(root: Path, *, target_repo: Path, status: str = "approved") -> tuple[Path, RunState]:
    runs_dir = root / ".runs"
    run_dir = runs_dir / "run_test_show_pipeline_apply"
    workspace = run_dir / "artifacts" / "workspace"
    (workspace / "src").mkdir(parents=True)
    (workspace / "src" / "toy_calc.py").write_text(
        "def subtract(a, b):\n    return a - b\n", encoding="utf-8"
    )
    report = {
        "schema_version": "1.0",
        "status": "completed",
        "summary": "Fixed subtract.",
        "changed_files": ["src/toy_calc.py", "EXECUTION_REPORT.json"],
        "commands_run": [
            {"command": "python -m unittest discover -s tests -t .", "exit_code": 0, "status": "passed", "summary": "ok"}
        ],
        "tests": [
            {"name": "tests", "command": "python -m unittest discover -s tests -t .", "status": "passed", "total": 1, "passed": 1, "failed": 0, "output": "OK"}
        ],
        "risks": [],
        "assumptions": [],
        "validation_notes": [],
    }
    (workspace / "EXECUTION_REPORT.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    task = TaskSpec(description="Fix subtract", seed_workspace_path=str(target_repo))
    state = RunState(run_id="run_test_show_pipeline_apply", task=task, final_status=status)
    content = "\n".join(
        [
            "# log",
            "## workspace files",
            "",
            "### EXECUTION_REPORT.json",
            json.dumps(report),
        ]
    )
    state.executions.append(
        ExecutionResult(
            step_id="step_1",
            attempt=1,
            status="completed",
            content=content,
            artifact_paths=[str(workspace / "EXECUTION_REPORT.json"), str(workspace / "src" / "toy_calc.py")],
        )
    )
    state.validations.append(
        ValidationResult(step_id="step_1", attempt=1, approved=status == "approved", score=1.0, feedback=["ok"])
    )
    state.save_json(run_dir / "state.json")
    return run_dir, state


def make_approved_source_run(runs_dir: Path, *, task: TaskSpec | None = None) -> tuple[Path, str]:
    source_task = task or TaskSpec(
        description="Create deterministic demo artifact",
        acceptance_criteria=["deterministic demo artifact"],
        max_retries=1,
    )
    state = TaskExecutionEngine(MockBackend(), runs_dir).run(source_task)
    return runs_dir / state.run_id, state.run_id


def make_failed_source_run(runs_dir: Path) -> tuple[Path, str]:
    task = TaskSpec(
        description="Create deterministic demo artifact",
        acceptance_criteria=["criterion one", "criterion two"],
        max_retries=0,
    )
    state = TaskExecutionEngine(MockBackend(), runs_dir).run(task)
    return runs_dir / state.run_id, state.run_id


def create_pipeline_fixture(
    root: Path,
    *,
    pipeline_id: str,
    tasks: list[PipelineTaskResult],
    selected_tasks: list[PipelineSelectedTask] | None = None,
    status: str = "approved",
    tasks_file: str | None = None,
    write_report: bool = True,
) -> tuple[Path, Path]:
    pipeline_dir = root / ".runs" / "pipelines" / pipeline_id
    pipeline_dir.mkdir(parents=True, exist_ok=True)
    state = PipelineState(
        pipeline_id=pipeline_id,
        tasks_file=tasks_file or str((root / "tasks.yaml").resolve()),
        status=status,
        selected_tasks=selected_tasks or [
            PipelineSelectedTask(task_id=task.task_id, title=task.title, enabled=True) for task in tasks
        ],
        tasks=tasks,
    )
    state_path = pipeline_dir / "pipeline_state.json"
    state.save_json(state_path)
    report_path = pipeline_dir / "PIPELINE_REPORT.md"
    if write_report:
        report_path.write_text(f"# Pipeline Report: {pipeline_id}\n", encoding="utf-8")
    return state_path, report_path


def build_pipeline_task_result(task_id: str, run_id: str, runs_dir: Path, *, title: str | None = None, status: str = "approved") -> PipelineTaskResult:
    run_dir = runs_dir / run_id
    return PipelineTaskResult(
        task_id=task_id,
        title=title,
        status=status,
        run_id=run_id,
        final_report=str((run_dir / "final_report.md").resolve()),
        review_packet=str((run_dir / "REVIEW_PACKET.md").resolve()),
        state=str((run_dir / "state.json").resolve()),
    )


class ShowPipelineTests(unittest.TestCase):
    def test_show_pipeline_for_approved_run_without_human_review(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            _run_dir, run_id = make_approved_source_run(runs_dir)
            create_pipeline_fixture(
                tmp,
                pipeline_id="pipeline_show_1",
                tasks=[build_pipeline_task_result("task-a", run_id, runs_dir)],
            )
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = show_pipeline_main(["pipeline_show_1", "--runs-dir", str(runs_dir)])
            output = stdout.getvalue()

        self.assertEqual(exit_code, 0, output)
        self.assertEqual(output_value(output, "tasks_waiting_review"), "1")
        self.assertEqual(output_value(output, "next_action"), "review_runs")

    def test_show_pipeline_with_human_approved_run(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            _run_dir, run_id = make_approved_source_run(runs_dir)
            record_review_decision(run_id=run_id, runs_dir=runs_dir, decision="approved")
            create_pipeline_fixture(
                tmp,
                pipeline_id="pipeline_show_2",
                tasks=[build_pipeline_task_result("task-a", run_id, runs_dir)],
            )
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = show_pipeline_main(["pipeline_show_2", "--runs-dir", str(runs_dir)])
            output = stdout.getvalue()

        self.assertEqual(exit_code, 0, output)
        self.assertEqual(output_value(output, "tasks_human_approved"), "1")
        self.assertEqual(output_value(output, "tasks_waiting_apply"), "1")
        self.assertEqual(output_value(output, "next_action"), "apply_runs")

    def test_show_pipeline_with_human_rejected_run(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            _run_dir, run_id = make_approved_source_run(runs_dir)
            feedback_path = tmp / "review_feedback.md"
            feedback_path.write_text("Reviewer rejected the output.\n", encoding="utf-8")
            record_review_decision(
                run_id=run_id,
                runs_dir=runs_dir,
                decision="rejected",
                feedback_path=feedback_path,
            )
            create_pipeline_fixture(
                tmp,
                pipeline_id="pipeline_show_3",
                tasks=[build_pipeline_task_result("task-a", run_id, runs_dir)],
            )
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = show_pipeline_main(["pipeline_show_3", "--runs-dir", str(runs_dir)])
            output = stdout.getvalue()

        self.assertEqual(exit_code, 0, output)
        self.assertEqual(output_value(output, "tasks_human_rejected"), "1")
        self.assertEqual(output_value(output, "next_action"), "rework_run")

    def test_show_pipeline_with_accepted_run(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            run_dir, run_id = make_approved_source_run(runs_dir)
            record_review_decision(run_id=run_id, runs_dir=runs_dir, decision="approved")
            (run_dir / "ACCEPTANCE.md").write_text("# acceptance\n", encoding="utf-8")
            create_pipeline_fixture(
                tmp,
                pipeline_id="pipeline_show_4",
                tasks=[build_pipeline_task_result("task-a", run_id, runs_dir)],
            )
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = show_pipeline_main(["pipeline_show_4", "--runs-dir", str(runs_dir)])
            output = stdout.getvalue()

        self.assertEqual(exit_code, 0, output)
        self.assertEqual(output_value(output, "tasks_accepted"), "1")
        self.assertEqual(output_value(output, "next_action"), "done")

    def test_show_pipeline_with_applied_not_accepted_run(self) -> None:
        with temporary_test_dir() as tmp:
            repo = make_git_seed_repo(tmp)
            run_dir, _state = make_approved_accept_run(tmp, target_repo=repo)
            record_review_decision(run_id=run_dir.name, runs_dir=run_dir.parent, decision="approved")
            (repo / "src" / "toy_calc.py").write_text(
                "def subtract(a, b):\n    return a + b\n", encoding="utf-8"
            )
            apply_run(run_id=run_dir.name, runs_dir=run_dir.parent)
            create_pipeline_fixture(
                tmp,
                pipeline_id="pipeline_show_4b",
                tasks=[build_pipeline_task_result("task-a", run_dir.name, run_dir.parent)],
            )
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = show_pipeline_main(["pipeline_show_4b", "--runs-dir", str(run_dir.parent)])
            output = stdout.getvalue()

        self.assertEqual(exit_code, 0, output)
        self.assertEqual(output_value(output, "tasks_applied"), "1")
        self.assertEqual(output_value(output, "tasks_waiting_manual_commit"), "1")
        self.assertEqual(output_value(output, "next_action"), "manual_commit")

    def test_show_pipeline_with_validator_failed_run(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            _run_dir, run_id = make_failed_source_run(runs_dir)
            create_pipeline_fixture(
                tmp,
                pipeline_id="pipeline_show_5",
                tasks=[build_pipeline_task_result("task-a", run_id, runs_dir, status="failed")],
                status="failed",
            )
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = show_pipeline_main(["pipeline_show_5", "--runs-dir", str(runs_dir)])
            output = stdout.getvalue()

        self.assertEqual(exit_code, 0, output)
        self.assertEqual(output_value(output, "tasks_validator_failed"), "1")
        self.assertEqual(output_value(output, "next_action"), "rework_or_inspect_failure")

    def test_show_pipeline_with_mixed_rejected_and_unreviewed_prefers_rework(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            _run_dir_a, run_id_a = make_approved_source_run(runs_dir)
            _run_dir_b, run_id_b = make_approved_source_run(runs_dir)
            feedback_path = tmp / "review_feedback.md"
            feedback_path.write_text("Rejected.\n", encoding="utf-8")
            record_review_decision(
                run_id=run_id_a,
                runs_dir=runs_dir,
                decision="rejected",
                feedback_path=feedback_path,
            )
            create_pipeline_fixture(
                tmp,
                pipeline_id="pipeline_show_6",
                tasks=[
                    build_pipeline_task_result("task-a", run_id_a, runs_dir),
                    build_pipeline_task_result("task-b", run_id_b, runs_dir),
                ],
            )
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = show_pipeline_main(["pipeline_show_6", "--runs-dir", str(runs_dir)])
            output = stdout.getvalue()

        self.assertEqual(exit_code, 0, output)
        self.assertEqual(output_value(output, "tasks_waiting_review"), "1")
        self.assertEqual(output_value(output, "tasks_human_rejected"), "1")
        self.assertEqual(output_value(output, "next_action"), "rework_run")

    def test_show_pipeline_with_missing_referenced_run_does_not_crash(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            create_pipeline_fixture(
                tmp,
                pipeline_id="pipeline_show_7",
                tasks=[build_pipeline_task_result("task-a", "run_missing_task", runs_dir)],
            )
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = show_pipeline_main(["pipeline_show_7", "--runs-dir", str(runs_dir)])
            output = stdout.getvalue()

        self.assertEqual(exit_code, 0, output)
        self.assertEqual(output_value(output, "next_action"), "inspect_pipeline")
        self.assertIn("validator_status=missing", output)
        self.assertIn("next_action=inspect_missing_run", output)
        self.assertIn("warning=run_missing", output)

    def test_show_pipeline_json_returns_valid_json(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            _run_dir, run_id = make_approved_source_run(runs_dir)
            create_pipeline_fixture(
                tmp,
                pipeline_id="pipeline_show_8",
                tasks=[build_pipeline_task_result("task-a", run_id, runs_dir)],
            )
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = show_pipeline_main(["pipeline_show_8", "--runs-dir", str(runs_dir), "--format", "json"])
            payload = json.loads(stdout.getvalue())

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["pipeline_id"], "pipeline_show_8")

    def test_show_pipeline_json_contains_counts_and_tasks(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            _run_dir, run_id = make_approved_source_run(runs_dir)
            record_review_decision(run_id=run_id, runs_dir=runs_dir, decision="approved")
            create_pipeline_fixture(
                tmp,
                pipeline_id="pipeline_show_9",
                tasks=[build_pipeline_task_result("task-a", run_id, runs_dir, title="Task A")],
            )
            stdout = StringIO()
            with redirect_stdout(stdout):
                show_pipeline_main(["pipeline_show_9", "--runs-dir", str(runs_dir), "--format", "json"])
            payload = json.loads(stdout.getvalue())

        self.assertEqual(payload["counts"]["tasks_total"], 1)
        self.assertEqual(payload["counts"]["tasks_human_approved"], 1)
        self.assertEqual(payload["counts"]["tasks_waiting_apply"], 1)
        self.assertEqual(payload["next_action"], "apply_runs")
        self.assertEqual(payload["tasks"][0]["task_id"], "task-a")
        self.assertEqual(payload["tasks"][0]["title"], "Task A")
        self.assertEqual(payload["tasks"][0]["application_status"], "not_applied")
        self.assertIn("final_report", payload["tasks"][0]["artifacts"])

    def test_show_pipeline_show_paths_includes_pipeline_paths(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            _run_dir, run_id = make_approved_source_run(runs_dir)
            create_pipeline_fixture(
                tmp,
                pipeline_id="pipeline_show_10",
                tasks=[build_pipeline_task_result("task-a", run_id, runs_dir)],
            )
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = show_pipeline_main(["pipeline_show_10", "--runs-dir", str(runs_dir), "--show-paths"])
            output = stdout.getvalue()

        self.assertEqual(exit_code, 0, output)
        self.assertIn("pipeline_state=", output)
        self.assertIn("pipeline_report=", output)

    def test_show_pipeline_missing_pipeline_id_gives_clear_error_and_nonzero_exit(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = show_pipeline_main(["missing-pipeline", "--runs-dir", str(runs_dir)])
            output = stdout.getvalue()

        self.assertEqual(exit_code, 1, output)
        self.assertEqual(output_value(output, "status"), "failed")
        self.assertEqual(output_value(output, "error"), "pipeline not found: missing-pipeline")

    def test_show_pipeline_does_not_create_or_modify_artifacts(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            run_dir, run_id = make_approved_source_run(runs_dir)
            pipeline_state_path, pipeline_report_path = create_pipeline_fixture(
                tmp,
                pipeline_id="pipeline_show_11",
                tasks=[build_pipeline_task_result("task-a", run_id, runs_dir)],
            )
            before_pipeline = sorted(str(path.relative_to(pipeline_state_path.parent)) for path in pipeline_state_path.parent.rglob("*"))
            before_run = sorted(str(path.relative_to(run_dir)) for path in run_dir.rglob("*"))
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = show_pipeline_main(["pipeline_show_11", "--runs-dir", str(runs_dir), "--show-paths"])
            after_pipeline = sorted(str(path.relative_to(pipeline_state_path.parent)) for path in pipeline_state_path.parent.rglob("*"))
            after_run = sorted(str(path.relative_to(run_dir)) for path in run_dir.rglob("*"))
            self.assertTrue(pipeline_report_path.exists())

        self.assertEqual(exit_code, 0, stdout.getvalue())
        self.assertEqual(before_pipeline, after_pipeline)
        self.assertEqual(before_run, after_run)


if __name__ == "__main__":
    unittest.main()
