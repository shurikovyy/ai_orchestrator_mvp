from __future__ import annotations

from contextlib import contextmanager, redirect_stdout
from io import StringIO
from pathlib import Path
import json
import shutil
import sys
import textwrap
import unittest
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_orchestrator.cli import draft_task_scaffold_main, revise_task_draft_main, validate_task_draft_main
from ai_orchestrator.task_drafts import load_task_draft, load_task_draft_manifest

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
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")


def output_value(output: str, key: str) -> str:
    prefix = f"{key}="
    for line in output.splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :].strip()
    raise AssertionError(f"missing output line for {key!r} in:\n{output}")


def scaffold_draft(
    tmp: Path,
    *,
    request_text: str,
    risk_level: str = "medium",
) -> tuple[str, Path]:
    request_path = tmp / "raw_request.md"
    write_text(request_path, request_text)
    drafts_dir = tmp / ".task_drafts"
    stdout = StringIO()
    with redirect_stdout(stdout):
        exit_code = draft_task_scaffold_main(
            ["--request", str(request_path), "--output-dir", str(drafts_dir), "--risk-level", risk_level]
        )
    output = stdout.getvalue()
    if exit_code != 0:
        raise AssertionError(output)
    return output_value(output, "draft_id"), Path(output_value(output, "draft_dir"))


def revise_draft(tmp: Path, draft_id: str, *extra_args: str) -> tuple[int, str]:
    stdout = StringIO()
    with redirect_stdout(stdout):
        exit_code = revise_task_draft_main([draft_id, "--drafts-dir", str(tmp / ".task_drafts"), *extra_args])
    return exit_code, stdout.getvalue()


class TaskDraftRevisionCliTests(unittest.TestCase):
    def test_revise_task_draft_updates_risk_level(self) -> None:
        with temporary_test_dir() as tmp:
            draft_id, draft_dir = scaffold_draft(tmp, request_text="# Docs\n\nDocument flow.\n", risk_level="unknown")
            exit_code, output = revise_draft(tmp, draft_id, "--risk-level", "medium")
            draft = load_task_draft(draft_dir / "task_draft.yaml")

        self.assertEqual(exit_code, 0, output)
        self.assertEqual(draft.risk_level, "medium")
        self.assertEqual(output_value(output, "validation_status"), "not_validated")

    def test_revise_task_draft_can_clear_and_add_files_allowed(self) -> None:
        with temporary_test_dir() as tmp:
            draft_id, draft_dir = scaffold_draft(
                tmp,
                request_text="# Docs\n\nCreate documentation for operator flow.\n",
                risk_level="medium",
            )
            exit_code, _output = revise_draft(
                tmp,
                draft_id,
                "--clear-files-allowed",
                "--allow-file",
                "docs/operator_quickstart.md",
                "--allow-file",
                "tests/test_operator_docs.py",
            )
            draft = load_task_draft(draft_dir / "task_draft.yaml")

        self.assertEqual(exit_code, 0)
        self.assertEqual(draft.files_allowed, ["docs/operator_quickstart.md", "tests/test_operator_docs.py"])

    def test_revise_task_draft_can_resolve_open_questions(self) -> None:
        with temporary_test_dir() as tmp:
            draft_id, draft_dir = scaffold_draft(tmp, request_text="# Docs\n\nDocument flow.\n")
            question = "Confirm exact files_allowed before promotion."
            exit_code, _output = revise_draft(tmp, draft_id, "--resolve-open-question", question)
            draft = load_task_draft(draft_dir / "task_draft.yaml")

        self.assertEqual(exit_code, 0)
        self.assertNotIn(question, draft.open_questions)

    def test_revise_task_draft_can_add_required_review_profiles(self) -> None:
        with temporary_test_dir() as tmp:
            draft_id, draft_dir = scaffold_draft(tmp, request_text="# Docs\n\nDocument flow.\n", risk_level="low")
            exit_code, _output = revise_draft(
                tmp,
                draft_id,
                "--require-profile",
                "qa",
                "--require-profile",
                "architecture",
            )
            draft = load_task_draft(draft_dir / "task_draft.yaml")

        self.assertEqual(exit_code, 0)
        self.assertEqual(draft.required_review_profiles, ["qa", "architecture"])

    def test_unknown_required_reviewer_profile_fails(self) -> None:
        with temporary_test_dir() as tmp:
            draft_id, draft_dir = scaffold_draft(tmp, request_text="# Docs\n\nDocument flow.\n")
            original_yaml = (draft_dir / "task_draft.yaml").read_text(encoding="utf-8")
            exit_code, output = revise_draft(tmp, draft_id, "--require-profile", "made_up_profile")
            current_yaml = (draft_dir / "task_draft.yaml").read_text(encoding="utf-8")

        self.assertEqual(exit_code, 1)
        self.assertIn("unknown review profile: made_up_profile", output)
        self.assertEqual(current_yaml, original_yaml)

    def test_unsafe_allowed_file_path_fails_and_original_draft_remains_unchanged(self) -> None:
        with temporary_test_dir() as tmp:
            draft_id, draft_dir = scaffold_draft(tmp, request_text="# Docs\n\nDocument flow.\n")
            original_yaml = (draft_dir / "task_draft.yaml").read_text(encoding="utf-8")
            exit_code, output = revise_draft(
                tmp,
                draft_id,
                "--clear-files-allowed",
                "--allow-file",
                "../escape.py",
            )
            current_yaml = (draft_dir / "task_draft.yaml").read_text(encoding="utf-8")

        self.assertEqual(exit_code, 1)
        self.assertIn("task draft file path", output)
        self.assertEqual(current_yaml, original_yaml)

    def test_dangerous_command_fails_and_original_draft_remains_unchanged(self) -> None:
        with temporary_test_dir() as tmp:
            draft_id, draft_dir = scaffold_draft(tmp, request_text="# Docs\n\nDocument flow.\n")
            original_yaml = (draft_dir / "task_draft.yaml").read_text(encoding="utf-8")
            exit_code, output = revise_draft(tmp, draft_id, "--add-command", "git reset --hard")
            current_yaml = (draft_dir / "task_draft.yaml").read_text(encoding="utf-8")

        self.assertEqual(exit_code, 1)
        self.assertIn("dangerous command snippet: git reset --hard", output)
        self.assertEqual(current_yaml, original_yaml)

    def test_codex_prompt_is_regenerated_with_updated_files_allowed_profile_and_risk(self) -> None:
        with temporary_test_dir() as tmp:
            draft_id, draft_dir = scaffold_draft(tmp, request_text="# Docs\n\nDocument flow.\n", risk_level="unknown")
            exit_code, _output = revise_draft(
                tmp,
                draft_id,
                "--risk-level",
                "high",
                "--clear-files-allowed",
                "--allow-file",
                "docs/example.md",
                "--require-profile",
                "qa",
                "--require-profile",
                "architecture",
            )
            codex_prompt = (draft_dir / "codex_prompt.md").read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0)
        self.assertIn("Risk level: `high`", codex_prompt)
        self.assertIn("Files allowed: `docs/example.md`", codex_prompt)
        self.assertIn("Required reviewer profiles: `qa, architecture`", codex_prompt)

    def test_task_review_is_regenerated_with_updated_risk_and_open_questions(self) -> None:
        with temporary_test_dir() as tmp:
            draft_id, draft_dir = scaffold_draft(tmp, request_text="# Docs\n\nDocument flow.\n", risk_level="unknown")
            exit_code, _output = revise_draft(tmp, draft_id, "--risk-level", "medium", "--clear-open-questions")
            task_review = (draft_dir / "task_review.md").read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0)
        self.assertIn("Risk level: `medium`", task_review)
        self.assertIn("- Open questions:", task_review)
        self.assertIn("  - (none)", task_review)

    def test_manifest_revision_count_increments(self) -> None:
        with temporary_test_dir() as tmp:
            draft_id, draft_dir = scaffold_draft(tmp, request_text="# Docs\n\nDocument flow.\n")
            self.assertEqual(revise_draft(tmp, draft_id, "--risk-level", "medium")[0], 0)
            self.assertEqual(revise_draft(tmp, draft_id, "--add-assumption", "Validated by reviewer.")[0], 0)
            manifest = load_task_draft_manifest(draft_dir / "MANIFEST.json")

        self.assertEqual(manifest.revision_count, 2)
        self.assertIsNotNone(manifest.revised_at)
        self.assertIn("assumptions", manifest.last_revision_summary or "")

    def test_existing_validation_becomes_stale_after_revision(self) -> None:
        with temporary_test_dir() as tmp:
            draft_id, draft_dir = scaffold_draft(
                tmp,
                request_text="# Docs\n\nDocument flow.\n",
                risk_level="medium",
            )
            with redirect_stdout(StringIO()):
                self.assertEqual(validate_task_draft_main([draft_id, "--drafts-dir", str(tmp / ".task_drafts")]), 0)
            exit_code, output = revise_draft(tmp, draft_id, "--clear-open-questions")
            manifest = load_task_draft_manifest(draft_dir / "MANIFEST.json")

        self.assertEqual(exit_code, 0, output)
        self.assertEqual(output_value(output, "validation_status"), "stale")
        self.assertEqual(manifest.validation_status, "stale")
        self.assertFalse(manifest.valid_for_promotion)
        self.assertEqual(manifest.validation_stale_reason, "task draft revised after last validation")

    def test_raw_request_remains_unchanged(self) -> None:
        with temporary_test_dir() as tmp:
            request_text = "# Docs\n\nDocument flow.\n"
            draft_id, draft_dir = scaffold_draft(tmp, request_text=request_text)
            original_raw_request = (draft_dir / "raw_request.md").read_text(encoding="utf-8")
            self.assertEqual(revise_draft(tmp, draft_id, "--risk-level", "medium")[0], 0)
            revised_raw_request = (draft_dir / "raw_request.md").read_text(encoding="utf-8")

        self.assertEqual(revised_raw_request, original_raw_request)

    def test_revise_does_not_modify_tasks_yaml_or_create_runs(self) -> None:
        with temporary_test_dir() as tmp:
            draft_id, _draft_dir = scaffold_draft(tmp, request_text="# Docs\n\nDocument flow.\n")
            tasks_yaml = tmp / "tasks.yaml"
            tasks_yaml.write_text("project: local\n", encoding="utf-8")
            original_tasks_yaml = tasks_yaml.read_text(encoding="utf-8")
            exit_code, _output = revise_draft(tmp, draft_id, "--risk-level", "medium")
            current_tasks_yaml = tasks_yaml.read_text(encoding="utf-8")
            runs_exists = (tmp / ".runs").exists()

        self.assertEqual(exit_code, 0)
        self.assertEqual(current_tasks_yaml, original_tasks_yaml)
        self.assertFalse(runs_exists)

    def test_revise_task_draft_json_output_is_valid(self) -> None:
        with temporary_test_dir() as tmp:
            draft_id, _draft_dir = scaffold_draft(tmp, request_text="# Docs\n\nDocument flow.\n")
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = revise_task_draft_main(
                    [draft_id, "--drafts-dir", str(tmp / ".task_drafts"), "--risk-level", "medium", "--format", "json"]
                )
            payload = json.loads(stdout.getvalue())

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "draft_revised")
        self.assertEqual(payload["validation_status"], "not_validated")

    def test_remove_exact_non_goal_works(self) -> None:
        with temporary_test_dir() as tmp:
            draft_id, draft_dir = scaffold_draft(tmp, request_text="# Docs\n\nDocument flow.\n")
            non_goal = "Do not make unrelated cleanup changes."
            exit_code, _output = revise_draft(tmp, draft_id, "--remove-non-goal", non_goal)
            draft = load_task_draft(draft_dir / "task_draft.yaml")

        self.assertEqual(exit_code, 0)
        self.assertNotIn(non_goal, draft.non_goals)

    def test_remove_command_works(self) -> None:
        with temporary_test_dir() as tmp:
            draft_id, draft_dir = scaffold_draft(tmp, request_text="# Docs\n\nDocument flow.\n")
            extra_command = "python -m pytest tests/test_specific.py"
            self.assertEqual(revise_draft(tmp, draft_id, "--add-command", extra_command)[0], 0)
            exit_code, output = revise_draft(tmp, draft_id, "--remove-command", extra_command)
            draft = load_task_draft(draft_dir / "task_draft.yaml")

        self.assertEqual(exit_code, 0, output)
        self.assertNotIn(extra_command, draft.commands_to_run)
        self.assertEqual(draft.commands_to_run, ["python -m unittest discover -s tests"])

    def test_remove_required_and_optional_profile_works(self) -> None:
        with temporary_test_dir() as tmp:
            draft_id, draft_dir = scaffold_draft(tmp, request_text="# Docs\n\nDocument flow.\n", risk_level="medium")
            exit_code, _output = revise_draft(
                tmp,
                draft_id,
                "--remove-required-profile",
                "qa",
                "--remove-optional-profile",
                "architecture",
            )
            draft = load_task_draft(draft_dir / "task_draft.yaml")

        self.assertEqual(exit_code, 0)
        self.assertEqual(draft.required_review_profiles, [])
        self.assertEqual(draft.optional_review_profiles, [])
