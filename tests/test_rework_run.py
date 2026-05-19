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
from ai_orchestrator.cli import rework_run_main
from ai_orchestrator.engine import TaskExecutionEngine
from ai_orchestrator.rework import execute_rework_run, resolve_rework_backend_name
from ai_orchestrator.schemas import TaskPlanStepSpec, TaskSpec

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
    (repo / "src" / "demo.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "init", str(repo)], text=True, capture_output=True, check=True)
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test User")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "seed")
    return repo


def make_source_run(
    runs_dir: Path,
    *,
    task: TaskSpec | None = None,
) -> tuple[Path, str]:
    source_task = task or TaskSpec(
        description="Create deterministic demo artifact",
        acceptance_criteria=["deterministic demo artifact"],
        max_retries=1,
    )
    state = TaskExecutionEngine(MockBackend(), runs_dir).run(source_task)
    run_dir = runs_dir / state.run_id
    return run_dir, state.run_id


class ReworkRunTests(unittest.TestCase):
    def test_rework_run_reads_existing_source_run_and_creates_new_run(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            seed_repo = make_git_seed_repo(tmp)
            source_task = TaskSpec(
                description="Create deterministic demo artifact",
                acceptance_criteria=["deterministic demo artifact"],
                seed_workspace_path=str(seed_repo),
                max_retries=1,
            )
            source_run_dir, source_run_id = make_source_run(runs_dir, task=source_task)
            source_state_before = (source_run_dir / "state.json").read_text(encoding="utf-8")
            source_head_before = git(seed_repo, "rev-parse", "HEAD").stdout.strip()
            feedback_path = tmp / "review_feedback.md"
            feedback_text = (
                "Reviewer rejected the previous output.\n"
                'Please make the report explicitly mention "REWORK_FEEDBACK_APPLIED".\n'
            )
            feedback_path.write_text(feedback_text, encoding="utf-8")

            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = rework_run_main(
                    [
                        source_run_id,
                        "--runs-dir",
                        str(runs_dir),
                        "--feedback",
                        str(feedback_path),
                        "--backend",
                        "mock",
                        "--verbose",
                    ]
                )
            output = stdout.getvalue()
            rework_run_id = output_value(output, "rework_run_id")
            rework_run_dir = runs_dir / rework_run_id
            rework_state = json.loads((rework_run_dir / "state.json").read_text(encoding="utf-8"))
            final_report = (rework_run_dir / "final_report.md").read_text(encoding="utf-8")
            review_packet = (rework_run_dir / "REVIEW_PACKET.md").read_text(encoding="utf-8")
            feedback_copy_path = rework_run_dir / "REWORK_FEEDBACK.md"
            self.assertEqual(exit_code, 0, output)
            self.assertNotEqual(rework_run_id, source_run_id)
            self.assertEqual((source_run_dir / "state.json").read_text(encoding="utf-8"), source_state_before)
            self.assertEqual(rework_state["task"]["rework_of_run_id"], source_run_id)
            self.assertEqual(rework_state["task"]["rework_feedback"], feedback_text.strip())
            self.assertEqual(rework_state["task"]["rework_feedback_path"], str(feedback_path.resolve()))
            self.assertEqual(rework_state["backend_name"], "mock")
            self.assertIn("REWORK_FEEDBACK_RECEIVED", rework_state["executions"][0]["content"])
            self.assertIn("REWORK_FEEDBACK_APPLIED", rework_state["executions"][0]["content"])
            self.assertTrue(feedback_copy_path.exists())
            self.assertEqual(feedback_copy_path.read_text(encoding="utf-8"), feedback_text)
            self.assertIn("## Rework context", review_packet)
            self.assertIn(source_run_id, review_packet)
            self.assertIn(str(feedback_path.resolve()), review_packet)
            self.assertIn("REWORK_FEEDBACK_APPLIED", review_packet)
            self.assertIn("## Rework context", final_report)
            self.assertIn(source_run_id, final_report)
            self.assertIn(str(feedback_path.resolve()), final_report)
            self.assertIn("REWORK_FEEDBACK_APPLIED", final_report)
            self.assertFalse((rework_run_dir / "ACCEPTANCE.md").exists())
            self.assertEqual(git(seed_repo, "rev-parse", "HEAD").stdout.strip(), source_head_before)
            self.assertEqual(git(seed_repo, "status", "--short").stdout.strip(), "")

    def test_missing_source_run_gives_clear_error(self) -> None:
        with temporary_test_dir() as tmp:
            feedback_path = tmp / "review_feedback.md"
            feedback_path.write_text("Fix the previous output.\n", encoding="utf-8")

            with self.assertRaisesRegex(FileNotFoundError, r"source run does not exist: missing-run"):
                execute_rework_run(
                    source_run_id="missing-run",
                    runs_dir=tmp / ".runs",
                    feedback_path=feedback_path,
                    backend_name="mock",
                )

    def test_missing_feedback_file_gives_clear_error(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            _source_run_dir, source_run_id = make_source_run(runs_dir)

            with self.assertRaisesRegex(FileNotFoundError, r"feedback file does not exist"):
                execute_rework_run(
                    source_run_id=source_run_id,
                    runs_dir=runs_dir,
                    feedback_path=tmp / "missing_feedback.md",
                    backend_name="mock",
                )

    def test_empty_feedback_file_gives_clear_error(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            _source_run_dir, source_run_id = make_source_run(runs_dir)
            feedback_path = tmp / "empty_feedback.md"
            feedback_path.write_text("\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, r"feedback file is empty"):
                execute_rework_run(
                    source_run_id=source_run_id,
                    runs_dir=runs_dir,
                    feedback_path=feedback_path,
                    backend_name="mock",
                )

    def test_backend_inferred_from_source_state_when_present(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            _source_run_dir, source_run_id = make_source_run(runs_dir)
            feedback_path = tmp / "review_feedback.md"
            feedback_path.write_text("Please rework the output.\n", encoding="utf-8")

            result = execute_rework_run(
                source_run_id=source_run_id,
                runs_dir=runs_dir,
                feedback_path=feedback_path,
            )

        self.assertEqual(result.backend_name, "mock")
        self.assertEqual(result.status, "approved")

    def test_backend_override_has_priority_over_source_backend_name(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            source_run_dir, source_run_id = make_source_run(runs_dir)
            state_path = source_run_dir / "state.json"
            payload = json.loads(state_path.read_text(encoding="utf-8"))
            payload["backend_name"] = "codex_cli"
            state_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            feedback_path = tmp / "review_feedback.md"
            feedback_path.write_text("Please rework the output.\n", encoding="utf-8")

            result = execute_rework_run(
                source_run_id=source_run_id,
                runs_dir=runs_dir,
                feedback_path=feedback_path,
                backend_name="mock",
            )

        self.assertEqual(result.backend_name, "mock")

    def test_old_state_without_backend_name_requires_explicit_backend(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            source_run_dir, source_run_id = make_source_run(runs_dir)
            state_path = source_run_dir / "state.json"
            payload = json.loads(state_path.read_text(encoding="utf-8"))
            payload.pop("backend_name", None)
            state_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            feedback_path = tmp / "review_feedback.md"
            feedback_path.write_text("Please rework the output.\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, r"Backend cannot be inferred from source run. Pass --backend."):
                execute_rework_run(
                    source_run_id=source_run_id,
                    runs_dir=runs_dir,
                    feedback_path=feedback_path,
                )

            result = execute_rework_run(
                source_run_id=source_run_id,
                runs_dir=runs_dir,
                feedback_path=feedback_path,
                backend_name="mock",
            )

        self.assertEqual(result.backend_name, "mock")

    def test_plan_steps_are_preserved_in_rework_task(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            source_task = TaskSpec(
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
                        description="Produce final report.",
                        criteria=["final report"],
                    ),
                ],
            )
            _source_run_dir, source_run_id = make_source_run(runs_dir, task=source_task)
            feedback_path = tmp / "review_feedback.md"
            feedback_path.write_text("Please rework the output.\n", encoding="utf-8")

            result = execute_rework_run(
                source_run_id=source_run_id,
                runs_dir=runs_dir,
                feedback_path=feedback_path,
                backend_name="mock",
            )
            state_payload = json.loads(result.state_path.read_text(encoding="utf-8"))

        self.assertEqual([step["id"] for step in state_payload["task"]["plan_steps"]], ["inspect", "report"])
        self.assertEqual([step["id"] for step in state_payload["plan"]["steps"]], ["inspect", "report"])

    def test_resolve_rework_backend_name_prefers_override(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            source_run_dir, _source_run_id = make_source_run(runs_dir)
            payload = json.loads((source_run_dir / "state.json").read_text(encoding="utf-8"))

        from ai_orchestrator.schemas import RunState

        state = RunState.model_validate(payload)
        self.assertEqual(resolve_rework_backend_name(state, "mock"), "mock")


if __name__ == "__main__":
    unittest.main()
