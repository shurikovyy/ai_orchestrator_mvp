from __future__ import annotations

from contextlib import contextmanager, redirect_stdout
from io import StringIO
from pathlib import Path
import json
import shutil
import subprocess
import sys
import unittest
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_orchestrator.backends.mock import MockBackend
from ai_orchestrator.apply import accept_run, apply_run, load_run_state
from ai_orchestrator.cli import show_run_main
from ai_orchestrator.engine import TaskExecutionEngine
from ai_orchestrator.rework import execute_rework_run
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
    run_dir = runs_dir / "run_test_show_accept"
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
    state = RunState(run_id="run_test_show_accept", task=task, final_status=status)
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


def add_workspace_changed_file(run_dir: Path, relative_path: str, content: str) -> None:
    workspace = run_dir / "artifacts" / "workspace"
    file_path = workspace / Path(relative_path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content, encoding="utf-8")

    report_path = workspace / "EXECUTION_REPORT.json"
    report_payload = json.loads(report_path.read_text(encoding="utf-8"))
    if relative_path not in report_payload["changed_files"]:
        report_payload["changed_files"].append(relative_path)
    report_path.write_text(json.dumps(report_payload, indent=2), encoding="utf-8")

    state = load_run_state(run_dir)
    execution = state.executions[-1]
    artifact_path = str(file_path)
    if artifact_path not in execution.artifact_paths:
        execution.artifact_paths.append(artifact_path)
    state.save_json(run_dir / "state.json")


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


class ShowRunTests(unittest.TestCase):
    def test_show_run_for_validator_approved_run_without_human_review(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            _run_dir, run_id = make_approved_source_run(runs_dir)
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = show_run_main([run_id, "--runs-dir", str(runs_dir)])
            output = stdout.getvalue()

        self.assertEqual(exit_code, 0, output)
        self.assertEqual(output_value(output, "validator_status"), "approved")
        self.assertEqual(output_value(output, "acceptance_status"), "not_accepted")
        self.assertEqual(output_value(output, "next_action"), "review_run")

    def test_show_run_for_human_approved_run_without_acceptance(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            _run_dir, run_id = make_approved_source_run(runs_dir)
            record_review_decision(run_id=run_id, runs_dir=runs_dir, decision="approved")
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = show_run_main([run_id, "--runs-dir", str(runs_dir)])
            output = stdout.getvalue()

        self.assertEqual(exit_code, 0, output)
        self.assertEqual(output_value(output, "human_review_decision"), "approved")
        self.assertEqual(output_value(output, "application_status"), "not_applied")
        self.assertEqual(output_value(output, "next_action"), "apply_run")

    def test_show_run_after_apply_run_reports_manual_commit_state(self) -> None:
        with temporary_test_dir() as tmp:
            repo = make_git_seed_repo(tmp)
            run_dir, _state = make_approved_accept_run(tmp, target_repo=repo)
            record_review_decision(run_id=run_dir.name, runs_dir=run_dir.parent, decision="approved")
            (repo / "src" / "toy_calc.py").write_text(
                "def subtract(a, b):\n    return a - b\n", encoding="utf-8"
            )
            git(repo, "add", ".")
            git(repo, "commit", "-m", "align target with workspace")
            add_workspace_changed_file(run_dir, "docs/show_run_apply_note.md", "# show-run apply note\n")
            result = apply_run(run_id=run_dir.name, runs_dir=run_dir.parent)
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = show_run_main([run_dir.name, "--runs-dir", str(run_dir.parent)])
            output = stdout.getvalue()
            repo_status_after_apply = git(repo, "status", "--short").stdout.strip()

        self.assertEqual(exit_code, 0, output)
        self.assertEqual(result.target_status, "dirty")
        self.assertIn("docs/show_run_apply_note.md", result.applied_files)
        self.assertTrue(repo_status_after_apply)
        self.assertEqual(output_value(output, "application_status"), "applied")
        self.assertEqual(output_value(output, "apply_report_exists"), "true")
        self.assertEqual(output_value(output, "next_action"), "manual_commit")

    def test_show_run_regression_noop_apply_run_still_fails(self) -> None:
        with temporary_test_dir() as tmp:
            repo = make_git_seed_repo(tmp)
            run_dir, _state = make_approved_accept_run(tmp, target_repo=repo)
            record_review_decision(run_id=run_dir.name, runs_dir=run_dir.parent, decision="approved")
            (repo / "src" / "toy_calc.py").write_text(
                "def subtract(a, b):\n    return a - b\n", encoding="utf-8"
            )
            git(repo, "add", ".")
            git(repo, "commit", "-m", "align target with workspace")

            with self.assertRaisesRegex(ValueError, "apply-run found no target changes to inspect"):
                apply_run(run_id=run_dir.name, runs_dir=run_dir.parent)

    def test_show_run_for_human_rejected_run(self) -> None:
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
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = show_run_main([run_id, "--runs-dir", str(runs_dir)])
            output = stdout.getvalue()

        self.assertEqual(exit_code, 0, output)
        self.assertEqual(output_value(output, "human_review_decision"), "rejected")
        self.assertEqual(output_value(output, "feedback_present"), "true")
        self.assertEqual(output_value(output, "next_action"), "rework_run")

    def test_show_run_for_accepted_run(self) -> None:
        with temporary_test_dir() as tmp:
            repo = make_git_seed_repo(tmp)
            run_dir, _state = make_approved_accept_run(tmp, target_repo=repo)
            record_review_decision(run_id=run_dir.name, runs_dir=run_dir.parent, decision="approved")
            workspace = run_dir / "artifacts" / "workspace"
            (workspace / "docs").mkdir(parents=True, exist_ok=True)
            (workspace / "docs" / "accept_note.md").write_text("# accepted change\n", encoding="utf-8")
            report_path = workspace / "EXECUTION_REPORT.json"
            report_payload = json.loads(report_path.read_text(encoding="utf-8"))
            report_payload["changed_files"].append("docs/accept_note.md")
            report_path.write_text(json.dumps(report_payload, indent=2), encoding="utf-8")
            accept_run(run_id=run_dir.name, runs_dir=run_dir.parent, commit_message="fix: subtract")
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = show_run_main([run_dir.name, "--runs-dir", str(run_dir.parent)])
            output = stdout.getvalue()

        self.assertEqual(exit_code, 0, output)
        self.assertEqual(output_value(output, "acceptance_exists"), "true")
        self.assertEqual(output_value(output, "acceptance_status"), "accepted")
        self.assertEqual(output_value(output, "next_action"), "done")

    def test_show_run_for_failed_validator_run(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            _run_dir, run_id = make_failed_source_run(runs_dir)
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = show_run_main([run_id, "--runs-dir", str(runs_dir)])
            output = stdout.getvalue()

        self.assertEqual(exit_code, 0, output)
        self.assertEqual(output_value(output, "validator_status"), "failed")
        self.assertEqual(output_value(output, "next_action"), "rework_or_inspect_failure")

    def test_show_run_for_rework_run(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            _source_run_dir, source_run_id = make_approved_source_run(runs_dir)
            feedback_path = tmp / "review_feedback.md"
            feedback_path.write_text("Please rework the output.\n", encoding="utf-8")
            result = execute_rework_run(
                source_run_id=source_run_id,
                runs_dir=runs_dir,
                feedback_path=feedback_path,
                backend_name="mock",
            )
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = show_run_main([result.rework_run_id, "--runs-dir", str(runs_dir)])
            output = stdout.getvalue()

        self.assertEqual(exit_code, 0, output)
        self.assertEqual(output_value(output, "is_rework"), "true")
        self.assertEqual(output_value(output, "source_run_id"), source_run_id)

    def test_show_run_json_returns_valid_json(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            _run_dir, run_id = make_approved_source_run(runs_dir)
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = show_run_main([run_id, "--runs-dir", str(runs_dir), "--format", "json"])
            payload = json.loads(stdout.getvalue())

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["run_id"], run_id)

    def test_show_run_json_contains_expected_fields(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            _run_dir, run_id = make_approved_source_run(runs_dir)
            record_review_decision(run_id=run_id, runs_dir=runs_dir, decision="approved")
            stdout = StringIO()
            with redirect_stdout(stdout):
                show_run_main([run_id, "--runs-dir", str(runs_dir), "--format", "json"])
            payload = json.loads(stdout.getvalue())

        self.assertEqual(payload["validator_status"], "approved")
        self.assertEqual(payload["human_review_decision"], "approved")
        self.assertEqual(payload["application_status"], "not_applied")
        self.assertEqual(payload["next_action"], "apply_run")
        self.assertIn("artifacts", payload)
        self.assertIn("exists", payload)
        self.assertIn("final_report", payload["artifacts"])
        self.assertIn("review_packet", payload["artifacts"])
        self.assertIn("review_decision", payload["artifacts"])
        self.assertIn("apply_report", payload["artifacts"])
        self.assertIn("apply_report_json", payload["artifacts"])
        self.assertIn("acceptance", payload["artifacts"])
        self.assertIn("apply_report", payload["exists"])
        self.assertIn("apply_report_json", payload["exists"])
        self.assertIn("review_decision_md", payload["exists"])

    def test_show_run_show_paths_includes_artifact_paths(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            _run_dir, run_id = make_approved_source_run(runs_dir)
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = show_run_main([run_id, "--runs-dir", str(runs_dir), "--show-paths"])
            output = stdout.getvalue()

        self.assertEqual(exit_code, 0, output)
        self.assertIn("final_report=", output)
        self.assertIn("review_packet=", output)
        self.assertIn("review_decision=", output)
        self.assertIn("apply_report=", output)
        self.assertIn("apply_report_json=", output)
        self.assertIn("acceptance=", output)
        self.assertIn("state=", output)

    def test_show_run_missing_run_id_gives_clear_error_and_nonzero_exit(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = show_run_main(["missing-run", "--runs-dir", str(runs_dir)])
            output = stdout.getvalue()

        self.assertEqual(exit_code, 1, output)
        self.assertEqual(output_value(output, "status"), "failed")
        self.assertEqual(output_value(output, "error"), "run not found: missing-run")

    def test_show_run_does_not_create_or_modify_artifacts(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            run_dir, run_id = make_approved_source_run(runs_dir)
            before = sorted(str(path.relative_to(run_dir)) for path in run_dir.rglob("*"))
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = show_run_main([run_id, "--runs-dir", str(runs_dir), "--show-paths"])
            after = sorted(str(path.relative_to(run_dir)) for path in run_dir.rglob("*"))

        self.assertEqual(exit_code, 0, stdout.getvalue())
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
