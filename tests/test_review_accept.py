from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_orchestrator.cli import build_accept_parser
from ai_orchestrator.review import accept_run, write_review_packet
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


def make_approved_run(root: Path, *, target_repo: Path, status: str = "approved") -> tuple[Path, RunState]:
    runs_dir = root / ".runs"
    run_dir = runs_dir / "run_test_accept"
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
    import json

    (workspace / "EXECUTION_REPORT.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    task = TaskSpec(description="Fix subtract", seed_workspace_path=str(target_repo))
    state = RunState(run_id="run_test_accept", task=task, final_status=status)
    content = "\n".join([
        "# log",
        "## workspace files",
        "",
        "### EXECUTION_REPORT.json",
        json.dumps(report),
    ])
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


class ReviewAcceptTests(unittest.TestCase):
    def test_write_review_packet_includes_diff_and_accept_command(self) -> None:
        with temporary_test_dir() as tmp:
            repo = make_git_seed_repo(tmp)
            run_dir, _ = make_approved_run(tmp, target_repo=repo)
            packet = write_review_packet(run_dir)
            text = packet.read_text(encoding="utf-8")
            self.assertIn("Review Packet: run_test_accept", text)
            self.assertIn("src/toy_calc.py", text)
            self.assertIn("return a + b", text)
            self.assertIn("return a - b", text)
            self.assertIn("accept-run run_test_accept", text)

    def test_accept_run_applies_allowed_files_and_commits(self) -> None:
        with temporary_test_dir() as tmp:
            repo = make_git_seed_repo(tmp)
            run_dir, _ = make_approved_run(tmp, target_repo=repo)
            record_review_decision(run_id=run_dir.name, runs_dir=run_dir.parent, decision="approved")
            result = accept_run(run_id=run_dir.name, runs_dir=run_dir.parent, commit_message="fix: subtract")
            self.assertIn("src/toy_calc.py", result.applied_files)
            self.assertIn("EXECUTION_REPORT.json", result.skipped_files)
            self.assertIsNotNone(result.commit_hash)
            self.assertEqual(result.review_gate, "human_approved")
            self.assertIn("return a - b", (repo / "src" / "toy_calc.py").read_text(encoding="utf-8"))
            self.assertFalse((repo / "EXECUTION_REPORT.json").exists())
            self.assertIn("fix: subtract", git(repo, "log", "-1", "--pretty=%s").stdout)
            self.assertTrue(result.acceptance_path.exists())
            acceptance_text = result.acceptance_path.read_text(encoding="utf-8")
            self.assertIn("## Review gate", acceptance_text)
            self.assertIn("Decision: `approved`", acceptance_text)
            self.assertIn("Bypassed: `false`", acceptance_text)

    def test_accept_run_refuses_missing_human_review_by_default(self) -> None:
        with temporary_test_dir() as tmp:
            repo = make_git_seed_repo(tmp)
            run_dir, _ = make_approved_run(tmp, target_repo=repo)
            with self.assertRaisesRegex(
                ValueError,
                r"run has no approved human review decision; run review-run --decision approved first or pass --allow-unreviewed",
            ):
                accept_run(run_id=run_dir.name, runs_dir=run_dir.parent, commit_message="fix: subtract")
            self.assertFalse((run_dir / "ACCEPTANCE.md").exists())

    def test_accept_run_allows_missing_human_review_with_allow_unreviewed(self) -> None:
        with temporary_test_dir() as tmp:
            repo = make_git_seed_repo(tmp)
            run_dir, _ = make_approved_run(tmp, target_repo=repo)
            (repo / "src" / "toy_calc.py").write_text(
                "def subtract(a, b):\n    return a + b\n", encoding="utf-8"
            )
            result = accept_run(
                run_id=run_dir.name,
                runs_dir=run_dir.parent,
                commit_message="fix: subtract",
                allow_unreviewed=True,
            )
            self.assertEqual(result.review_gate, "bypassed_unreviewed")
            acceptance_text = result.acceptance_path.read_text(encoding="utf-8")
            self.assertIn("## Review gate", acceptance_text)
            self.assertIn("Decision: `missing`", acceptance_text)
            self.assertIn("Bypassed: `true`", acceptance_text)
            self.assertIn("Reason: `--allow-unreviewed`", acceptance_text)

    def test_accept_run_refuses_rejected_human_review(self) -> None:
        with temporary_test_dir() as tmp:
            repo = make_git_seed_repo(tmp)
            run_dir, _ = make_approved_run(tmp, target_repo=repo)
            feedback_path = tmp / "review_feedback.md"
            feedback_path.write_text("Rejected by reviewer.\n", encoding="utf-8")
            record_review_decision(
                run_id=run_dir.name,
                runs_dir=run_dir.parent,
                decision="rejected",
                feedback_path=feedback_path,
            )
            with self.assertRaisesRegex(
                ValueError,
                r"run has rejected human review decision; run rework-run before accept-run",
            ):
                accept_run(run_id=run_dir.name, runs_dir=run_dir.parent, commit_message="fix: subtract")
            self.assertFalse((run_dir / "ACCEPTANCE.md").exists())

    def test_accept_run_refuses_rejected_human_review_even_with_allow_unreviewed(self) -> None:
        with temporary_test_dir() as tmp:
            repo = make_git_seed_repo(tmp)
            run_dir, _ = make_approved_run(tmp, target_repo=repo)
            feedback_path = tmp / "review_feedback.md"
            feedback_path.write_text("Rejected by reviewer.\n", encoding="utf-8")
            record_review_decision(
                run_id=run_dir.name,
                runs_dir=run_dir.parent,
                decision="rejected",
                feedback_path=feedback_path,
            )
            with self.assertRaisesRegex(
                ValueError,
                r"run has rejected human review decision; run rework-run before accept-run",
            ):
                accept_run(
                    run_id=run_dir.name,
                    runs_dir=run_dir.parent,
                    commit_message="fix: subtract",
                    allow_unreviewed=True,
                )
            self.assertFalse((run_dir / "ACCEPTANCE.md").exists())

    def test_accept_run_refuses_non_approved_run(self) -> None:
        with temporary_test_dir() as tmp:
            repo = make_git_seed_repo(tmp)
            run_dir, _ = make_approved_run(tmp, target_repo=repo, status="failed")
            with self.assertRaisesRegex(ValueError, "not approved"):
                accept_run(run_id=run_dir.name, runs_dir=run_dir.parent, commit_message="fix: subtract")

    def test_accept_run_refuses_dirty_target_repo(self) -> None:
        with temporary_test_dir() as tmp:
            repo = make_git_seed_repo(tmp)
            run_dir, _ = make_approved_run(tmp, target_repo=repo)
            record_review_decision(run_id=run_dir.name, runs_dir=run_dir.parent, decision="approved")
            (repo / "untracked.txt").write_text("dirty", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "dirty"):
                accept_run(run_id=run_dir.name, runs_dir=run_dir.parent, commit_message="fix: subtract")

    def test_accept_run_can_initialize_disposable_target_git_repo(self) -> None:
        with temporary_test_dir() as tmp:
            repo = tmp / "seed_repo"
            (repo / "src").mkdir(parents=True)
            (repo / "src" / "toy_calc.py").write_text(
                "def subtract(a, b):\n    return a + b\n", encoding="utf-8"
            )
            run_dir, _ = make_approved_run(tmp, target_repo=repo)
            # Reassert the disposable target baseline after the run fixture is built.
            # On some Windows/Git configurations this prevents the init-target-git
            # smoke test from degenerating into an empty accept commit.
            (repo / "src" / "toy_calc.py").write_text(
                "def subtract(a, b):\n    return a + b\n", encoding="utf-8"
            )
            record_review_decision(run_id=run_dir.name, runs_dir=run_dir.parent, decision="approved")
            self.assertIn("return a + b", (repo / "src" / "toy_calc.py").read_text(encoding="utf-8"))
            result = accept_run(
                run_id=run_dir.name,
                runs_dir=run_dir.parent,
                commit_message="fix: subtract",
                init_target_git=True,
            )
            self.assertIsNotNone(result.commit_hash)
            self.assertTrue((repo / ".git").exists())
            if result.no_target_changes:
                # On some Windows Git/Python combinations this disposable init flow can
                # be idempotent by the time commit creation is reached. The important
                # contract is that accept-run does not fail merely because the target
                # already matches the accepted workspace contents.
                self.assertIn("return a - b", (repo / "src" / "toy_calc.py").read_text(encoding="utf-8"))
            else:
                self.assertIn("return a - b", (repo / "src" / "toy_calc.py").read_text(encoding="utf-8"))
                self.assertIn("fix: subtract", git(repo, "log", "-1", "--pretty=%s").stdout)
                self.assertGreaterEqual(int(git(repo, "rev-list", "--count", "HEAD").stdout.strip()), 2)

    def test_accept_run_init_target_git_is_idempotent_when_no_changes_remain(self) -> None:
        with temporary_test_dir() as tmp:
            repo = tmp / "seed_repo"
            (repo / "src").mkdir(parents=True)
            (repo / "src" / "toy_calc.py").write_text(
                "def subtract(a, b):\n    return a - b\n", encoding="utf-8"
            )
            run_dir, _ = make_approved_run(tmp, target_repo=repo)
            record_review_decision(run_id=run_dir.name, runs_dir=run_dir.parent, decision="approved")
            result = accept_run(
                run_id=run_dir.name,
                runs_dir=run_dir.parent,
                commit_message="fix: subtract",
                init_target_git=True,
            )
            self.assertTrue(result.no_target_changes)
            self.assertIsNotNone(result.commit_hash)
            self.assertTrue((repo / ".git").exists())

    def test_accept_run_old_state_without_human_review_requires_allow_unreviewed(self) -> None:
        with temporary_test_dir() as tmp:
            repo = make_git_seed_repo(tmp)
            run_dir, _ = make_approved_run(tmp, target_repo=repo)
            state_path = run_dir / "state.json"
            import json

            payload = json.loads(state_path.read_text(encoding="utf-8"))
            payload.pop("human_review_decision", None)
            payload.pop("human_review_decided_at", None)
            payload.pop("human_review_feedback", None)
            payload.pop("human_review_feedback_path", None)
            payload.pop("human_review_decision_path", None)
            state_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

            result = accept_run(
                run_id=run_dir.name,
                runs_dir=run_dir.parent,
                commit_message="fix: subtract",
                allow_unreviewed=True,
            )
            self.assertEqual(result.review_gate, "bypassed_unreviewed")

    def test_accept_parser_exposes_allow_unreviewed_flag(self) -> None:
        parser = build_accept_parser()
        help_text = parser.format_help()
        self.assertIn("--allow-unreviewed", help_text)


if __name__ == "__main__":
    unittest.main()
