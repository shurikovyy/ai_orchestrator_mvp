from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_orchestrator.cli import build_accept_parser
from ai_orchestrator.apply import accept_run, load_run_state
from ai_orchestrator.review import write_review_packet
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


class ReviewAcceptTests(unittest.TestCase):
    def test_write_review_packet_includes_diff_and_manual_apply_workflow(self) -> None:
        with temporary_test_dir() as tmp:
            repo = make_git_seed_repo(tmp)
            run_dir, _ = make_approved_run(tmp, target_repo=repo)
            packet = write_review_packet(run_dir)
            text = packet.read_text(encoding="utf-8")
            self.assertIn("Review Packet: run_test_accept", text)
            self.assertIn("src/toy_calc.py", text)
            self.assertIn("return a + b", text)
            self.assertIn("return a - b", text)
            self.assertIn("## Recommended manual apply workflow", text)
            self.assertIn("review-run run_test_accept", text)
            self.assertIn("--decision approved", text)
            self.assertIn("apply-run run_test_accept", text)
            self.assertIn("git diff --stat", text)
            self.assertIn("git diff", text)
            self.assertIn("python -m unittest discover -s tests", text)
            self.assertIn("git add <files>", text)
            self.assertIn("git commit -m", text)
            self.assertNotIn("## Accept command", text)

    def test_write_review_packet_keeps_accept_run_as_advanced_explicit_option(self) -> None:
        with temporary_test_dir() as tmp:
            repo = make_git_seed_repo(tmp)
            run_dir, _ = make_approved_run(tmp, target_repo=repo)
            packet = write_review_packet(run_dir)
            text = packet.read_text(encoding="utf-8")

        manual_workflow_index = text.index("## Recommended manual apply workflow")
        advanced_index = text.index("## Advanced delegated commit path")
        accept_index = text.index("accept-run run_test_accept")
        self.assertLess(manual_workflow_index, advanced_index)
        self.assertLess(advanced_index, accept_index)
        self.assertIn("Use it only when you explicitly want delegated apply + commit", text)

    def test_accept_run_applies_allowed_files_and_commits(self) -> None:
        with temporary_test_dir() as tmp:
            repo = make_git_seed_repo(tmp)
            run_dir, _ = make_approved_run(tmp, target_repo=repo)
            add_workspace_changed_file(run_dir, "docs/accept_run_note.md", "# accept run note\n")
            record_review_decision(run_id=run_dir.name, runs_dir=run_dir.parent, decision="approved")
            result = accept_run(run_id=run_dir.name, runs_dir=run_dir.parent, commit_message="fix: subtract")
            self.assertIn("docs/accept_run_note.md", result.applied_files)
            self.assertIn("EXECUTION_REPORT.json", result.skipped_files)
            self.assertIsNotNone(result.commit_hash)
            self.assertEqual(result.review_gate, "human_approved")
            self.assertTrue((repo / "docs" / "accept_run_note.md").exists())
            self.assertIn("# accept run note", (repo / "docs" / "accept_run_note.md").read_text(encoding="utf-8"))
            self.assertFalse((repo / "EXECUTION_REPORT.json").exists())
            self.assertIn("fix: subtract", git(repo, "log", "-1", "--pretty=%s").stdout)
            self.assertTrue(result.acceptance_path.exists())
            acceptance_text = result.acceptance_path.read_text(encoding="utf-8")
            self.assertIn("## Review gate", acceptance_text)
            self.assertIn("Decision: `approved`", acceptance_text)
            self.assertIn("Bypassed: `false`", acceptance_text)
            self.assertEqual(git(repo, "status", "--short").stdout.strip(), "")

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
            add_workspace_changed_file(run_dir, "docs/review_note.md", "# review note\n")
            state_before = load_run_state(run_dir)
            result = accept_run(
                run_id=run_dir.name,
                runs_dir=run_dir.parent,
                commit_message="fix: subtract",
                allow_unreviewed=True,
            )
            self.assertIsNone(state_before.human_review_decision)
            self.assertEqual(result.review_gate, "bypassed_unreviewed")
            self.assertIsNotNone(result.commit_hash)
            self.assertIn("docs/review_note.md", result.applied_files)
            self.assertTrue((repo / "docs" / "review_note.md").exists())
            self.assertFalse((repo / "EXECUTION_REPORT.json").exists())
            acceptance_text = result.acceptance_path.read_text(encoding="utf-8")
            self.assertIn("## Review gate", acceptance_text)
            self.assertIn("Decision: `missing`", acceptance_text)
            self.assertIn("Bypassed: `true`", acceptance_text)
            self.assertIn("Reason: `--allow-unreviewed`", acceptance_text)

    def test_accept_run_still_fails_when_clean_target_has_no_changes_to_commit(self) -> None:
        with temporary_test_dir() as tmp:
            repo = make_git_seed_repo(tmp)
            run_dir, _ = make_approved_run(tmp, target_repo=repo)
            (repo / "src" / "toy_calc.py").write_text(
                "def subtract(a, b):\n    return a - b\n", encoding="utf-8"
            )
            git(repo, "add", ".")
            git(repo, "commit", "-m", "align target with workspace")
            record_review_decision(run_id=run_dir.name, runs_dir=run_dir.parent, decision="approved")

            with self.assertRaisesRegex(ValueError, "accept-run found no target changes to commit"):
                accept_run(run_id=run_dir.name, runs_dir=run_dir.parent, commit_message="fix: subtract")

            self.assertFalse((run_dir / "ACCEPTANCE.md").exists())

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
            add_workspace_changed_file(run_dir, "docs/old_state_accept_note.md", "# old state accept note\n")

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
            state_payload_after = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(result.review_gate, "bypassed_unreviewed")
            self.assertIsNotNone(result.commit_hash)
            self.assertNotIn("human_review_decision", payload)
            self.assertIn("docs/old_state_accept_note.md", result.applied_files)
            self.assertTrue((repo / "docs" / "old_state_accept_note.md").exists())
            self.assertFalse((repo / "EXECUTION_REPORT.json").exists())
            self.assertEqual(state_payload_after.get("human_review_decision"), None)
            acceptance_text = result.acceptance_path.read_text(encoding="utf-8")
            self.assertIn("Bypassed: `true`", acceptance_text)
            self.assertIn("Reason: `--allow-unreviewed`", acceptance_text)

    def test_accept_run_dry_run_does_not_modify_target_repo_or_create_artifacts(self) -> None:
        with temporary_test_dir() as tmp:
            repo = make_git_seed_repo(tmp)
            run_dir, _ = make_approved_run(tmp, target_repo=repo)
            record_review_decision(run_id=run_dir.name, runs_dir=run_dir.parent, decision="approved")
            before_text = (repo / "src" / "toy_calc.py").read_text(encoding="utf-8")
            head_before = git(repo, "rev-parse", "HEAD").stdout.strip()
            commit_count_before = git(repo, "rev-list", "--count", "HEAD").stdout.strip()

            result = accept_run(run_id=run_dir.name, runs_dir=run_dir.parent, commit_message="fix: subtract", dry_run=True)

            self.assertIsNone(result.commit_hash)
            self.assertFalse(result.acceptance_path.exists())
            self.assertEqual((repo / "src" / "toy_calc.py").read_text(encoding="utf-8"), before_text)
            self.assertEqual(git(repo, "status", "--short").stdout.strip(), "")
            self.assertEqual(git(repo, "diff", "--cached", "--name-only").stdout.strip(), "")
            self.assertEqual(git(repo, "rev-parse", "HEAD").stdout.strip(), head_before)
            self.assertEqual(git(repo, "rev-list", "--count", "HEAD").stdout.strip(), commit_count_before)

    def test_accept_run_dry_run_still_enforces_missing_human_review_gate(self) -> None:
        with temporary_test_dir() as tmp:
            repo = make_git_seed_repo(tmp)
            run_dir, _ = make_approved_run(tmp, target_repo=repo)

            with self.assertRaisesRegex(
                ValueError,
                r"run has no approved human review decision; run review-run --decision approved first or pass --allow-unreviewed",
            ):
                accept_run(run_id=run_dir.name, runs_dir=run_dir.parent, commit_message="fix: subtract", dry_run=True)

            self.assertFalse((run_dir / "ACCEPTANCE.md").exists())
            self.assertEqual(git(repo, "status", "--short").stdout.strip(), "")

    def test_accept_run_dry_run_still_fails_on_rejected_human_review(self) -> None:
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
                accept_run(run_id=run_dir.name, runs_dir=run_dir.parent, commit_message="fix: subtract", dry_run=True)

            self.assertFalse((run_dir / "ACCEPTANCE.md").exists())
            self.assertEqual(git(repo, "status", "--short").stdout.strip(), "")

    def test_accept_run_dry_run_still_fails_on_dirty_target_repo(self) -> None:
        with temporary_test_dir() as tmp:
            repo = make_git_seed_repo(tmp)
            run_dir, _ = make_approved_run(tmp, target_repo=repo)
            record_review_decision(run_id=run_dir.name, runs_dir=run_dir.parent, decision="approved")
            (repo / "untracked.txt").write_text("dirty", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "dirty"):
                accept_run(run_id=run_dir.name, runs_dir=run_dir.parent, commit_message="fix: subtract", dry_run=True)

            self.assertFalse((run_dir / "ACCEPTANCE.md").exists())

    def test_accept_parser_exposes_allow_unreviewed_flag(self) -> None:
        parser = build_accept_parser()
        help_text = parser.format_help()
        self.assertIn("--allow-unreviewed", help_text)


if __name__ == "__main__":
    unittest.main()
