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

from ai_orchestrator.apply import accept_run, apply_run, load_run_state
from ai_orchestrator.cli import apply_main, build_apply_parser
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
    run_dir = runs_dir / "run_test_apply"
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
    state = RunState(run_id="run_test_apply", task=task, final_status=status)
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


def output_value(output: str, key: str) -> str:
    prefix = f"{key}="
    for line in output.splitlines():
        if line.startswith(prefix):
            return line[len(prefix):].strip()
    raise AssertionError(f"missing output line for {key!r} in:\n{output}")


class ApplyRunTests(unittest.TestCase):
    def test_apply_run_fails_without_human_review_by_default(self) -> None:
        with temporary_test_dir() as tmp:
            repo = make_git_seed_repo(tmp)
            run_dir, _ = make_approved_run(tmp, target_repo=repo)
            with self.assertRaisesRegex(
                ValueError,
                r"run has no approved human review decision; run review-run --decision approved first or pass --allow-unreviewed",
            ):
                apply_run(run_id=run_dir.name, runs_dir=run_dir.parent)

    def test_apply_run_succeeds_after_review_approved_and_leaves_repo_dirty_unstaged(self) -> None:
        with temporary_test_dir() as tmp:
            repo = make_git_seed_repo(tmp)
            run_dir, _ = make_approved_run(tmp, target_repo=repo)
            record_review_decision(run_id=run_dir.name, runs_dir=run_dir.parent, decision="approved")
            (repo / "src" / "toy_calc.py").write_text(
                "def subtract(a, b):\n    return a - b\n", encoding="utf-8"
            )
            git(repo, "add", ".")
            git(repo, "commit", "-m", "align target with workspace")
            add_workspace_changed_file(run_dir, "docs/apply_run_note.md", "# apply run note\n")
            head_before = git(repo, "rev-parse", "HEAD").stdout.strip()
            commit_count_before = git(repo, "rev-list", "--count", "HEAD").stdout.strip()

            result = apply_run(run_id=run_dir.name, runs_dir=run_dir.parent)
            state = load_run_state(run_dir)
            apply_report_md = run_dir / "APPLY_REPORT.md"
            apply_report_json = run_dir / "APPLY_REPORT.json"
            status_short = git(repo, "status", "--short").stdout
            self.assertEqual(result.review_gate, "human_approved")
            self.assertEqual(result.target_status, "dirty")
            self.assertIn("docs/apply_run_note.md", result.applied_files)
            self.assertIn("EXECUTION_REPORT.json", result.skipped_files)
            self.assertEqual(result.deleted_files, [])
            self.assertEqual((repo / "docs" / "apply_run_note.md").read_text(encoding="utf-8"), "# apply run note\n")
            self.assertFalse((repo / "EXECUTION_REPORT.json").exists())
            self.assertEqual(git(repo, "rev-parse", "HEAD").stdout.strip(), head_before)
            self.assertEqual(git(repo, "rev-list", "--count", "HEAD").stdout.strip(), commit_count_before)
            self.assertTrue(status_short.strip())
            self.assertEqual(git(repo, "diff", "--cached", "--name-only").stdout.strip(), "")
            self.assertTrue(apply_report_md.exists())
            self.assertTrue(apply_report_json.exists())
            self.assertFalse((run_dir / "ACCEPTANCE.md").exists())
            self.assertEqual(state.apply_status, "applied")
            self.assertIsNotNone(state.applied_at)
            self.assertEqual(state.apply_report_path, str(apply_report_md.resolve()))
            self.assertEqual(state.apply_target_workspace, str(repo.resolve()))
            self.assertIn("docs/apply_run_note.md", state.applied_files)
            self.assertEqual(state.deleted_files, [])
            self.assertEqual(state.skipped_files, ["EXECUTION_REPORT.json"])
            self.assertIn("Status: `applied`", apply_report_md.read_text(encoding="utf-8"))
            self.assertIn("git diff --stat", apply_report_md.read_text(encoding="utf-8"))
            apply_payload = json.loads(apply_report_json.read_text(encoding="utf-8"))
            self.assertFalse(apply_payload["commit_created"])
            self.assertFalse(apply_payload["git_add_performed"])

    def test_apply_run_rejected_review_fails(self) -> None:
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
            with self.assertRaisesRegex(ValueError, r"run has rejected human review decision; run rework-run before apply-run"):
                apply_run(run_id=run_dir.name, runs_dir=run_dir.parent)

    def test_apply_run_rejected_review_still_fails_with_allow_unreviewed(self) -> None:
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
            with self.assertRaisesRegex(ValueError, r"run has rejected human review decision; run rework-run before apply-run"):
                apply_run(run_id=run_dir.name, runs_dir=run_dir.parent, allow_unreviewed=True)

    def test_apply_run_allows_unreviewed_with_allow_unreviewed(self) -> None:
        with temporary_test_dir() as tmp:
            repo = make_git_seed_repo(tmp)
            run_dir, _ = make_approved_run(tmp, target_repo=repo)
            (repo / "src" / "toy_calc.py").write_text(
                "def subtract(a, b):\n    return a - b\n", encoding="utf-8"
            )
            git(repo, "add", ".")
            git(repo, "commit", "-m", "align target with workspace")
            add_workspace_changed_file(run_dir, "docs/unreviewed_apply_note.md", "# unreviewed apply note\n")

            result = apply_run(run_id=run_dir.name, runs_dir=run_dir.parent, allow_unreviewed=True)
            self.assertEqual(result.review_gate, "bypassed_unreviewed")
            self.assertIn("docs/unreviewed_apply_note.md", result.applied_files)
            self.assertTrue((repo / "docs" / "unreviewed_apply_note.md").exists())
            self.assertTrue((run_dir / "APPLY_REPORT.md").exists())

    def test_apply_run_still_fails_when_clean_target_has_no_changes_to_inspect(self) -> None:
        with temporary_test_dir() as tmp:
            repo = make_git_seed_repo(tmp)
            run_dir, _ = make_approved_run(tmp, target_repo=repo)
            (repo / "src" / "toy_calc.py").write_text(
                "def subtract(a, b):\n    return a - b\n", encoding="utf-8"
            )
            git(repo, "add", ".")
            git(repo, "commit", "-m", "align target with workspace")
            record_review_decision(run_id=run_dir.name, runs_dir=run_dir.parent, decision="approved")

            with self.assertRaisesRegex(ValueError, "apply-run found no target changes to inspect"):
                apply_run(run_id=run_dir.name, runs_dir=run_dir.parent)

            self.assertFalse((run_dir / "APPLY_REPORT.md").exists())
            self.assertFalse((run_dir / "APPLY_REPORT.json").exists())

    def test_apply_run_refuses_dirty_target_repo(self) -> None:
        with temporary_test_dir() as tmp:
            repo = make_git_seed_repo(tmp)
            run_dir, _ = make_approved_run(tmp, target_repo=repo)
            record_review_decision(run_id=run_dir.name, runs_dir=run_dir.parent, decision="approved")
            (repo / "untracked.txt").write_text("dirty", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, r"target git repository is dirty; commit/stash changes before apply-run"):
                apply_run(run_id=run_dir.name, runs_dir=run_dir.parent)

    def test_apply_run_dry_run_does_not_modify_target_or_persist_state(self) -> None:
        with temporary_test_dir() as tmp:
            repo = make_git_seed_repo(tmp)
            run_dir, _ = make_approved_run(tmp, target_repo=repo)
            record_review_decision(run_id=run_dir.name, runs_dir=run_dir.parent, decision="approved")
            before_text = (repo / "src" / "toy_calc.py").read_text(encoding="utf-8")

            result = apply_run(run_id=run_dir.name, runs_dir=run_dir.parent, dry_run=True)
            state = load_run_state(run_dir)
            self.assertEqual(result.target_status, "clean")
            self.assertEqual((repo / "src" / "toy_calc.py").read_text(encoding="utf-8"), before_text)
            self.assertEqual(git(repo, "status", "--short").stdout.strip(), "")
            self.assertIsNone(state.apply_status)
            self.assertIsNone(state.apply_report_path)
            self.assertIsNone(state.apply_target_workspace)
            self.assertEqual(state.applied_files, [])
            self.assertFalse((run_dir / "APPLY_REPORT.md").exists())
            self.assertFalse((run_dir / "APPLY_REPORT.json").exists())

    def test_apply_run_dry_run_still_enforces_missing_human_review_gate(self) -> None:
        with temporary_test_dir() as tmp:
            repo = make_git_seed_repo(tmp)
            run_dir, _ = make_approved_run(tmp, target_repo=repo)

            with self.assertRaisesRegex(
                ValueError,
                r"run has no approved human review decision; run review-run --decision approved first or pass --allow-unreviewed",
            ):
                apply_run(run_id=run_dir.name, runs_dir=run_dir.parent, dry_run=True)

            self.assertFalse((run_dir / "APPLY_REPORT.md").exists())
            self.assertFalse((run_dir / "APPLY_REPORT.json").exists())
            self.assertEqual(git(repo, "status", "--short").stdout.strip(), "")

    def test_apply_run_dry_run_still_fails_on_rejected_human_review(self) -> None:
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

            with self.assertRaisesRegex(ValueError, r"run has rejected human review decision; run rework-run before apply-run"):
                apply_run(run_id=run_dir.name, runs_dir=run_dir.parent, dry_run=True)

            self.assertFalse((run_dir / "APPLY_REPORT.md").exists())
            self.assertFalse((run_dir / "APPLY_REPORT.json").exists())
            self.assertEqual(git(repo, "status", "--short").stdout.strip(), "")

    def test_apply_run_dry_run_still_fails_on_dirty_target_repo(self) -> None:
        with temporary_test_dir() as tmp:
            repo = make_git_seed_repo(tmp)
            run_dir, _ = make_approved_run(tmp, target_repo=repo)
            record_review_decision(run_id=run_dir.name, runs_dir=run_dir.parent, decision="approved")
            (repo / "untracked.txt").write_text("dirty", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, r"target git repository is dirty; commit/stash changes before apply-run"):
                apply_run(run_id=run_dir.name, runs_dir=run_dir.parent, dry_run=True)

            self.assertFalse((run_dir / "APPLY_REPORT.md").exists())
            self.assertFalse((run_dir / "APPLY_REPORT.json").exists())

    def test_apply_main_prints_dry_run_summary(self) -> None:
        with temporary_test_dir() as tmp:
            repo = make_git_seed_repo(tmp)
            run_dir, _ = make_approved_run(tmp, target_repo=repo)
            record_review_decision(run_id=run_dir.name, runs_dir=run_dir.parent, decision="approved")
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = apply_main([run_dir.name, "--runs-dir", str(run_dir.parent), "--dry-run"])
            output = stdout.getvalue()

        self.assertEqual(exit_code, 0, output)
        self.assertEqual(output_value(output, "apply_status"), "dry_run_ok")
        self.assertEqual(output_value(output, "target_status"), "clean")
        self.assertIn("would_apply_files=src/toy_calc.py", output)
        self.assertIn("would_skip_files=EXECUTION_REPORT.json", output)

    def test_accept_run_still_commits_after_shared_refactor(self) -> None:
        with temporary_test_dir() as tmp:
            repo = make_git_seed_repo(tmp)
            run_dir, _ = make_approved_run(tmp, target_repo=repo)
            record_review_decision(run_id=run_dir.name, runs_dir=run_dir.parent, decision="approved")
            workspace = run_dir / "artifacts" / "workspace"
            (workspace / "docs").mkdir(parents=True, exist_ok=True)
            (workspace / "docs" / "accept_note.md").write_text("# accepted change\n", encoding="utf-8")
            report_path = workspace / "EXECUTION_REPORT.json"
            report_payload = json.loads(report_path.read_text(encoding="utf-8"))
            report_payload["changed_files"].append("docs/accept_note.md")
            report_path.write_text(json.dumps(report_payload, indent=2), encoding="utf-8")

            result = accept_run(run_id=run_dir.name, runs_dir=run_dir.parent, commit_message="fix: subtract")
            self.assertIsNotNone(result.commit_hash)
            self.assertTrue(result.acceptance_path.exists())
            self.assertIn("fix: subtract", git(repo, "log", "-1", "--pretty=%s").stdout)

    def test_apply_parser_exposes_allow_unreviewed_flag(self) -> None:
        parser = build_apply_parser()
        help_text = parser.format_help()
        self.assertIn("--allow-unreviewed", help_text)


if __name__ == "__main__":
    unittest.main()
