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
from ai_orchestrator.cli import review_run_main
from ai_orchestrator.engine import TaskExecutionEngine
from ai_orchestrator.rework import execute_rework_run
from ai_orchestrator.review import load_run_state
from ai_orchestrator.review_decision import record_review_decision
from ai_orchestrator.schemas import RunState, TaskSpec

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


class ReviewDecisionTests(unittest.TestCase):
    def test_review_run_approved_records_decision_and_writes_artifacts(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            _run_dir, run_id = make_approved_source_run(runs_dir)
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = review_run_main(
                    [run_id, "--runs-dir", str(runs_dir), "--decision", "approved"]
                )
            output = stdout.getvalue()
            state = load_run_state(runs_dir / run_id)
            decision_json_path = runs_dir / run_id / "REVIEW_DECISION.json"
            decision_md_path = runs_dir / run_id / "REVIEW_DECISION.md"
            decision_payload = json.loads(decision_json_path.read_text(encoding="utf-8"))
            decision_md = decision_md_path.read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0, output)
        self.assertEqual(output_value(output, "status"), "review_recorded")
        self.assertEqual(output_value(output, "decision"), "approved")
        self.assertEqual(state.human_review_decision, "approved")
        self.assertIsNotNone(state.human_review_decided_at)
        self.assertIsNone(state.human_review_feedback)
        self.assertIsNone(state.human_review_feedback_path)
        self.assertEqual(state.human_review_decision_path, str(decision_json_path.resolve()))
        self.assertEqual(decision_payload["decision"], "approved")
        self.assertEqual(decision_payload["run_id"], run_id)
        self.assertEqual(decision_payload["schema_version"], "1.0")
        self.assertIn("Decision: `approved`", decision_md)
        self.assertIn("No accept-run or commit was performed by review-run.", decision_md)

    def test_review_run_rejected_requires_feedback(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            _run_dir, run_id = make_approved_source_run(runs_dir)

            with self.assertRaisesRegex(ValueError, r"Feedback is required for rejected review decisions."):
                record_review_decision(
                    run_id=run_id,
                    runs_dir=runs_dir,
                    decision="rejected",
                )

    def test_review_run_rejected_copies_feedback_and_stores_state_fields(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            seed_repo = make_git_seed_repo(tmp)
            task = TaskSpec(
                description="Create deterministic demo artifact",
                acceptance_criteria=["deterministic demo artifact"],
                seed_workspace_path=str(seed_repo),
                max_retries=1,
            )
            run_dir, run_id = make_approved_source_run(runs_dir, task=task)
            head_before = git(seed_repo, "rev-parse", "HEAD").stdout.strip()
            feedback_path = tmp / "review_feedback.md"
            feedback_text = "Reviewer says: add clearer explanation. REVIEW_DECISION_TEST\n"
            feedback_path.write_text(feedback_text, encoding="utf-8")

            result = record_review_decision(
                run_id=run_id,
                runs_dir=runs_dir,
                decision="rejected",
                feedback_path=feedback_path,
            )
            state = load_run_state(run_dir)
            decision_json_path = run_dir / "REVIEW_DECISION.json"
            decision_md_path = run_dir / "REVIEW_DECISION.md"
            feedback_copy_path = run_dir / "REVIEW_FEEDBACK.md"
            decision_payload = json.loads(decision_json_path.read_text(encoding="utf-8"))
            decision_md = decision_md_path.read_text(encoding="utf-8")
            self.assertEqual(result.decision, "rejected")
            self.assertEqual(result.review_decision_path, decision_json_path.resolve())
            self.assertEqual(result.review_feedback_path, feedback_copy_path.resolve())
            self.assertEqual(state.human_review_decision, "rejected")
            self.assertIsNotNone(state.human_review_decided_at)
            self.assertEqual(state.human_review_feedback, feedback_text.strip())
            self.assertEqual(state.human_review_feedback_path, str(feedback_copy_path.resolve()))
            self.assertEqual(state.human_review_decision_path, str(decision_json_path.resolve()))
            self.assertEqual(feedback_copy_path.read_text(encoding="utf-8"), feedback_text)
            self.assertEqual(decision_payload["feedback_path"], str(feedback_copy_path.resolve()))
            self.assertIn("REVIEW_DECISION_TEST", decision_payload["feedback_excerpt"])
            self.assertIn("Decision: `rejected`", decision_md)
            self.assertIn("REVIEW_DECISION_TEST", decision_md)
            self.assertIn("python -m ai_orchestrator.cli rework-run", decision_md)
            self.assertFalse((run_dir / "ACCEPTANCE.md").exists())
            self.assertEqual(git(seed_repo, "rev-parse", "HEAD").stdout.strip(), head_before)
            self.assertEqual(git(seed_repo, "status", "--short").stdout.strip(), "")

    def test_review_run_rejects_empty_feedback_file(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            _run_dir, run_id = make_approved_source_run(runs_dir)
            feedback_path = tmp / "review_feedback.md"
            feedback_path.write_text("\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, r"feedback file is empty"):
                record_review_decision(
                    run_id=run_id,
                    runs_dir=runs_dir,
                    decision="rejected",
                    feedback_path=feedback_path,
                )

    def test_review_run_rejects_missing_feedback_file(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            _run_dir, run_id = make_approved_source_run(runs_dir)

            with self.assertRaisesRegex(FileNotFoundError, r"feedback file does not exist"):
                record_review_decision(
                    run_id=run_id,
                    runs_dir=runs_dir,
                    decision="rejected",
                    feedback_path=tmp / "missing_feedback.md",
                )

    def test_review_run_rejects_missing_source_run(self) -> None:
        with temporary_test_dir() as tmp:
            with self.assertRaisesRegex(FileNotFoundError, r"run does not exist: missing-run"):
                record_review_decision(
                    run_id="missing-run",
                    runs_dir=tmp / ".runs",
                    decision="approved",
                )

    def test_review_run_rejects_non_approved_source_run(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            _run_dir, run_id = make_failed_source_run(runs_dir)

            with self.assertRaisesRegex(
                ValueError,
                r"Only validator-approved runs can receive a human review decision.",
            ):
                record_review_decision(
                    run_id=run_id,
                    runs_dir=runs_dir,
                    decision="approved",
                )

    def test_repeated_review_run_without_force_fails_clearly(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            _run_dir, run_id = make_approved_source_run(runs_dir)
            record_review_decision(run_id=run_id, runs_dir=runs_dir, decision="approved")

            with self.assertRaisesRegex(
                ValueError,
                r"Human review decision already recorded. Pass --force to overwrite it.",
            ):
                record_review_decision(run_id=run_id, runs_dir=runs_dir, decision="approved")

    def test_repeated_review_run_with_force_overwrites_decision(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            run_dir, run_id = make_approved_source_run(runs_dir)
            feedback_path = tmp / "review_feedback.md"
            feedback_path.write_text("Rejected once.\n", encoding="utf-8")
            record_review_decision(
                run_id=run_id,
                runs_dir=runs_dir,
                decision="rejected",
                feedback_path=feedback_path,
            )

            result = record_review_decision(
                run_id=run_id,
                runs_dir=runs_dir,
                decision="approved",
                force=True,
            )
            state = load_run_state(run_dir)
            decision_payload = json.loads((run_dir / "REVIEW_DECISION.json").read_text(encoding="utf-8"))

        self.assertEqual(result.decision, "approved")
        self.assertEqual(state.human_review_decision, "approved")
        self.assertIsNone(state.human_review_feedback)
        self.assertIsNone(state.human_review_feedback_path)
        self.assertEqual(decision_payload["decision"], "approved")
        self.assertIsNone(decision_payload["feedback_path"])

    def test_rework_run_without_feedback_uses_stored_rejected_review_feedback(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            _run_dir, run_id = make_approved_source_run(runs_dir)
            feedback_path = tmp / "review_feedback.md"
            feedback_text = "Reviewer says: add clearer explanation. REVIEW_DECISION_TEST\n"
            feedback_path.write_text(feedback_text, encoding="utf-8")
            record_review_decision(
                run_id=run_id,
                runs_dir=runs_dir,
                decision="rejected",
                feedback_path=feedback_path,
            )

            result = execute_rework_run(
                source_run_id=run_id,
                runs_dir=runs_dir,
                backend_name="mock",
            )
            rework_state = load_run_state(runs_dir / result.rework_run_id)
            feedback_copy_text = (runs_dir / result.rework_run_id / "REWORK_FEEDBACK.md").read_text(encoding="utf-8")

        self.assertEqual(result.status, "approved")
        self.assertIn("REVIEW_DECISION_TEST", rework_state.task.rework_feedback or "")
        self.assertIn("REWORK_FEEDBACK_RECEIVED", rework_state.executions[0].content)
        self.assertIn("REVIEW_DECISION_TEST", feedback_copy_text)

    def test_rework_run_without_feedback_fails_when_no_rejected_review_feedback_exists(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            _run_dir, run_id = make_approved_source_run(runs_dir)

            with self.assertRaisesRegex(
                ValueError,
                r"Feedback is required unless source run has a rejected human review decision with feedback.",
            ):
                execute_rework_run(
                    source_run_id=run_id,
                    runs_dir=runs_dir,
                    backend_name="mock",
                )

    def test_explicit_rework_feedback_overrides_stored_review_feedback(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            _run_dir, run_id = make_approved_source_run(runs_dir)
            stored_feedback_path = tmp / "stored_feedback.md"
            stored_feedback_path.write_text("Stored feedback REVIEW_DECISION_OLD\n", encoding="utf-8")
            record_review_decision(
                run_id=run_id,
                runs_dir=runs_dir,
                decision="rejected",
                feedback_path=stored_feedback_path,
            )
            explicit_feedback_path = tmp / "explicit_feedback.md"
            explicit_feedback_path.write_text("Explicit override REVIEW_DECISION_NEW\n", encoding="utf-8")

            result = execute_rework_run(
                source_run_id=run_id,
                runs_dir=runs_dir,
                feedback_path=explicit_feedback_path,
                backend_name="mock",
            )
            rework_state = load_run_state(runs_dir / result.rework_run_id)

        self.assertIn("REVIEW_DECISION_NEW", rework_state.task.rework_feedback or "")
        self.assertNotIn("REVIEW_DECISION_OLD", rework_state.task.rework_feedback or "")

    def test_old_state_without_human_review_fields_still_loads(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            run_dir, run_id = make_approved_source_run(runs_dir)
            state_path = run_dir / "state.json"
            payload = json.loads(state_path.read_text(encoding="utf-8"))
            payload.pop("human_review_decision", None)
            payload.pop("human_review_decided_at", None)
            payload.pop("human_review_feedback", None)
            payload.pop("human_review_feedback_path", None)
            payload.pop("human_review_decision_path", None)
            state_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

            state = RunState.model_validate_json(state_path.read_text(encoding="utf-8"))
            result = record_review_decision(
                run_id=run_id,
                runs_dir=runs_dir,
                decision="approved",
            )

        self.assertIsNone(state.human_review_decision)
        self.assertEqual(result.decision, "approved")


if __name__ == "__main__":
    unittest.main()
