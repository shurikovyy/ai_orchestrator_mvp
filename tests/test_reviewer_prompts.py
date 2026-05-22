from __future__ import annotations

from contextlib import contextmanager, redirect_stderr, redirect_stdout
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

from ai_orchestrator.cli import prepare_review_main, show_run_main
from ai_orchestrator.review_profiles import list_review_profiles
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
            return line[len(prefix) :].strip()
    raise AssertionError(f"missing output line for {key!r} in:\n{output}")


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")


def git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(repo), *args], text=True, capture_output=True, check=True)


def make_git_seed_repo(root: Path) -> Path:
    repo = root / "seed_repo"
    repo.mkdir(parents=True)
    write_text(repo / "README.md", "# Seed Repo\n")
    subprocess.run(["git", "init", str(repo)], text=True, capture_output=True, check=True)
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test User")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "seed")
    return repo


def make_prompt_run_fixture(
    root: Path,
    *,
    run_id: str = "run_test_reviewer_prompt",
    seed_workspace_path: str | None = None,
) -> tuple[Path, Path]:
    runs_dir = root / ".runs"
    run_dir = runs_dir / run_id
    workspace = run_dir / "artifacts" / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    write_text(workspace / "docs" / "guide.md", "# Guide\n")
    report = {
        "schema_version": "1.0",
        "status": "completed",
        "summary": "Synthetic reviewer prompt fixture.",
        "changed_files": ["docs/guide.md", "EXECUTION_REPORT.json"],
        "commands_run": [
            {
                "command": "python -m unittest discover -s tests",
                "exit_code": 0,
                "status": "passed",
                "summary": "ok",
            }
        ],
        "tests": [
            {
                "name": "tests",
                "command": "python -m unittest discover -s tests",
                "status": "passed",
                "total": 1,
                "passed": 1,
                "failed": 0,
                "output": "OK",
            }
        ],
        "risks": [],
        "assumptions": [],
        "validation_notes": [],
    }
    report_path = workspace / "EXECUTION_REPORT.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    state = RunState(
        run_id=run_id,
        task=TaskSpec(
            description="Document the operator workflow clearly.",
            acceptance_criteria=["docs/operator_quickstart.md exists", "tests pass"],
            seed_workspace_path=seed_workspace_path,
        ),
        backend_name="mock",
        final_status="approved",
    )
    state.executions.append(
        ExecutionResult(
            step_id="step_1",
            attempt=1,
            status="completed",
            content="\n".join(
                [
                    "# execution log",
                    "### EXECUTION_REPORT.json",
                    json.dumps(report),
                ]
            ),
            artifact_paths=[str(report_path), str(workspace / "docs" / "guide.md")],
        )
    )
    state.validations.append(
        ValidationResult(step_id="step_1", attempt=1, approved=True, score=1.0, feedback=["ok"])
    )
    state.save_json(run_dir / "state.json")
    write_text(run_dir / "final_report.md", "# Final report\n\nAll explicit criteria passed.\n")
    write_text(run_dir / "REVIEW_PACKET.md", "# Review packet\n\nChanged files look reasonable.\n")
    return run_dir, runs_dir


class ReviewerPromptsTests(unittest.TestCase):
    def test_prepare_review_creates_prompt_for_qa_profile(self) -> None:
        with temporary_test_dir() as tmp:
            run_dir, runs_dir = make_prompt_run_fixture(tmp)
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = prepare_review_main([run_dir.name, "--runs-dir", str(runs_dir), "--profile", "qa"])
            output = stdout.getvalue()
            prompt_path = run_dir / "reviewer_prompts" / "qa_review_prompt.md"
            prompt_exists = prompt_path.exists()

        self.assertEqual(exit_code, 0, output)
        self.assertTrue(prompt_exists)
        self.assertEqual(output_value(output, "status"), "review_prompts_prepared")

    def test_prompt_contains_profile_contract_sections(self) -> None:
        with temporary_test_dir() as tmp:
            run_dir, runs_dir = make_prompt_run_fixture(tmp)
            with redirect_stdout(StringIO()):
                self.assertEqual(prepare_review_main([run_dir.name, "--runs-dir", str(runs_dir), "--profile", "qa"]), 0)
            prompt_text = (run_dir / "reviewer_prompts" / "qa_review_prompt.md").read_text(encoding="utf-8")

        self.assertIn("QA Reviewer", prompt_text)
        self.assertIn("## Focus areas", prompt_text)
        self.assertIn("## Finding categories", prompt_text)
        self.assertIn("## Non-goals", prompt_text)

    def test_prompt_contains_task_description(self) -> None:
        with temporary_test_dir() as tmp:
            run_dir, runs_dir = make_prompt_run_fixture(tmp)
            with redirect_stdout(StringIO()):
                self.assertEqual(prepare_review_main([run_dir.name, "--runs-dir", str(runs_dir), "--profile", "qa"]), 0)
            prompt_text = (run_dir / "reviewer_prompts" / "qa_review_prompt.md").read_text(encoding="utf-8")

        self.assertIn("Document the operator workflow clearly.", prompt_text)

    def test_prompt_contains_review_packet_content(self) -> None:
        with temporary_test_dir() as tmp:
            run_dir, runs_dir = make_prompt_run_fixture(tmp)
            with redirect_stdout(StringIO()):
                self.assertEqual(prepare_review_main([run_dir.name, "--runs-dir", str(runs_dir), "--profile", "qa"]), 0)
            prompt_text = (run_dir / "reviewer_prompts" / "qa_review_prompt.md").read_text(encoding="utf-8")

        self.assertIn("Changed files look reasonable.", prompt_text)

    def test_prompt_contains_execution_report_content(self) -> None:
        with temporary_test_dir() as tmp:
            run_dir, runs_dir = make_prompt_run_fixture(tmp)
            with redirect_stdout(StringIO()):
                self.assertEqual(prepare_review_main([run_dir.name, "--runs-dir", str(runs_dir), "--profile", "qa"]), 0)
            prompt_text = (run_dir / "reviewer_prompts" / "qa_review_prompt.md").read_text(encoding="utf-8")

        self.assertIn('"changed_files": [', prompt_text)
        self.assertIn("docs/guide.md", prompt_text)

    def test_prompt_contains_expected_review_findings_template(self) -> None:
        with temporary_test_dir() as tmp:
            run_dir, runs_dir = make_prompt_run_fixture(tmp)
            with redirect_stdout(StringIO()):
                self.assertEqual(prepare_review_main([run_dir.name, "--runs-dir", str(runs_dir), "--profile", "qa"]), 0)
            prompt_text = (run_dir / "reviewer_prompts" / "qa_review_prompt.md").read_text(encoding="utf-8")

        self.assertIn('"schema_version": "1.0"', prompt_text)
        self.assertIn('"overall_decision": "pass | needs_rework | blocked"', prompt_text)
        self.assertIn('"reviewer": "qa"', prompt_text)

    def test_manifest_is_written(self) -> None:
        with temporary_test_dir() as tmp:
            run_dir, runs_dir = make_prompt_run_fixture(tmp)
            with redirect_stdout(StringIO()):
                self.assertEqual(prepare_review_main([run_dir.name, "--runs-dir", str(runs_dir), "--profile", "qa"]), 0)
            manifest_path = run_dir / "reviewer_prompts" / "MANIFEST.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        self.assertEqual(manifest["run_id"], run_dir.name)
        self.assertEqual(manifest["profiles"], ["qa"])

    def test_repeated_prepare_review_without_force_fails_if_prompt_exists(self) -> None:
        with temporary_test_dir() as tmp:
            run_dir, runs_dir = make_prompt_run_fixture(tmp)
            with redirect_stdout(StringIO()):
                self.assertEqual(prepare_review_main([run_dir.name, "--runs-dir", str(runs_dir), "--profile", "qa"]), 0)
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = prepare_review_main([run_dir.name, "--runs-dir", str(runs_dir), "--profile", "qa"])
            output = stdout.getvalue()

        self.assertEqual(exit_code, 1, output)
        self.assertIn("reviewer prompt already exists", output)

    def test_repeated_prepare_review_with_force_overwrites_prompt(self) -> None:
        with temporary_test_dir() as tmp:
            run_dir, runs_dir = make_prompt_run_fixture(tmp)
            with redirect_stdout(StringIO()):
                self.assertEqual(prepare_review_main([run_dir.name, "--runs-dir", str(runs_dir), "--profile", "qa"]), 0)
            prompt_path = run_dir / "reviewer_prompts" / "qa_review_prompt.md"
            prompt_path.write_text("CUSTOM MARKER\n", encoding="utf-8")
            with redirect_stdout(StringIO()):
                exit_code = prepare_review_main([run_dir.name, "--runs-dir", str(runs_dir), "--profile", "qa", "--force"])
            prompt_text = prompt_path.read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0)
        self.assertNotIn("CUSTOM MARKER", prompt_text)
        self.assertIn("Reviewer Prompt Packet: qa", prompt_text)

    def test_profile_can_be_repeated_to_create_multiple_prompts(self) -> None:
        with temporary_test_dir() as tmp:
            run_dir, runs_dir = make_prompt_run_fixture(tmp)
            with redirect_stdout(StringIO()):
                exit_code = prepare_review_main(
                    [run_dir.name, "--runs-dir", str(runs_dir), "--profile", "qa", "--profile", "architecture"]
                )
            qa_prompt = run_dir / "reviewer_prompts" / "qa_review_prompt.md"
            arch_prompt = run_dir / "reviewer_prompts" / "architecture_review_prompt.md"
            qa_prompt_exists = qa_prompt.exists()
            arch_prompt_exists = arch_prompt.exists()

        self.assertEqual(exit_code, 0)
        self.assertTrue(qa_prompt_exists)
        self.assertTrue(arch_prompt_exists)

    def test_all_profiles_creates_prompts_for_all_non_deterministic_profiles(self) -> None:
        with temporary_test_dir() as tmp:
            run_dir, runs_dir = make_prompt_run_fixture(tmp)
            with redirect_stdout(StringIO()):
                exit_code = prepare_review_main([run_dir.name, "--runs-dir", str(runs_dir), "--all-profiles"])
            prompts_dir = run_dir / "reviewer_prompts"
            generated = {path.name for path in prompts_dir.glob("*_review_prompt.md")}
            expected = {
                f"{profile.id}_review_prompt.md"
                for profile in list_review_profiles()
                if profile.id != "deterministic"
            }

        self.assertEqual(exit_code, 0)
        self.assertEqual(generated, expected)
        self.assertNotIn("deterministic_review_prompt.md", generated)

    def test_profile_and_all_profiles_together_fail(self) -> None:
        with temporary_test_dir() as tmp:
            run_dir, runs_dir = make_prompt_run_fixture(tmp)
            stderr = StringIO()
            with redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as ctx:
                    prepare_review_main([run_dir.name, "--runs-dir", str(runs_dir), "--profile", "qa", "--all-profiles"])

        self.assertEqual(ctx.exception.code, 2)

    def test_missing_run_fails_clearly(self) -> None:
        stdout = StringIO()
        with redirect_stdout(stdout):
            exit_code = prepare_review_main(["run_missing", "--runs-dir", ".runs", "--profile", "qa"])
        output = stdout.getvalue()

        self.assertEqual(exit_code, 1, output)
        self.assertIn("run does not exist: run_missing", output)

    def test_missing_profile_fails_clearly(self) -> None:
        with temporary_test_dir() as tmp:
            run_dir, runs_dir = make_prompt_run_fixture(tmp)
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = prepare_review_main([run_dir.name, "--runs-dir", str(runs_dir), "--profile", "missing"])
            output = stdout.getvalue()

        self.assertEqual(exit_code, 1, output)
        self.assertIn("review profile not found: missing", output)

    def test_json_output_is_valid(self) -> None:
        with temporary_test_dir() as tmp:
            run_dir, runs_dir = make_prompt_run_fixture(tmp)
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = prepare_review_main([run_dir.name, "--runs-dir", str(runs_dir), "--profile", "qa", "--format", "json"])
            payload = json.loads(stdout.getvalue())

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "review_prompts_prepared")
        self.assertEqual(payload["profiles"], ["qa"])

    def test_show_run_displays_reviewer_prompts_status_after_prepare_review(self) -> None:
        with temporary_test_dir() as tmp:
            run_dir, runs_dir = make_prompt_run_fixture(tmp)
            with redirect_stdout(StringIO()):
                self.assertEqual(prepare_review_main([run_dir.name, "--runs-dir", str(runs_dir), "--profile", "qa"]), 0)
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = show_run_main([run_dir.name, "--runs-dir", str(runs_dir), "--show-paths"])
            output = stdout.getvalue()

        self.assertEqual(exit_code, 0, output)
        self.assertEqual(output_value(output, "reviewer_prompts_exists"), "true")
        self.assertEqual(output_value(output, "reviewer_prompts_count"), "1")
        self.assertIn("reviewer_prompts_dir=", output)
        self.assertIn("reviewer_prompts_manifest=", output)

    def test_prepare_review_does_not_create_review_findings_json(self) -> None:
        with temporary_test_dir() as tmp:
            run_dir, runs_dir = make_prompt_run_fixture(tmp)
            with redirect_stdout(StringIO()):
                self.assertEqual(prepare_review_main([run_dir.name, "--runs-dir", str(runs_dir), "--profile", "qa"]), 0)

        self.assertFalse((run_dir / "REVIEW_FINDINGS.json").exists())

    def test_prepare_review_does_not_change_target_repo(self) -> None:
        with temporary_test_dir() as tmp:
            repo = make_git_seed_repo(tmp)
            run_dir, runs_dir = make_prompt_run_fixture(tmp, seed_workspace_path=str(repo))
            with redirect_stdout(StringIO()):
                exit_code = prepare_review_main([run_dir.name, "--runs-dir", str(runs_dir), "--profile", "qa"])
            repo_status = git(repo, "status", "--short").stdout.strip()

        self.assertEqual(exit_code, 0)
        self.assertEqual(repo_status, "")
