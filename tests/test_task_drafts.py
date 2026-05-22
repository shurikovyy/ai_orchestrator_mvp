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

from ai_orchestrator.cli import draft_task_scaffold_main
from ai_orchestrator.task_drafts import TaskDraft, TargetTaskDraft

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


def load_yaml(path: Path) -> object:
    import yaml

    return yaml.safe_load(path.read_text(encoding="utf-8"))


class TaskDraftModelTests(unittest.TestCase):
    def test_task_draft_rejects_dangerous_commands(self) -> None:
        with self.assertRaisesRegex(ValueError, "dangerous command snippet: git reset --hard"):
            TaskDraft(
                draft_id="draft_1",
                title="Draft",
                objective="Objective",
                non_goals=["No unrelated cleanup."],
                files_forbidden=["src/ai_orchestrator/apply.py"],
                invariants=["Tests must pass."],
                tests_required=["Add tests if source changes."],
                commands_to_run=["git reset --hard"],
                acceptance_criteria=["tests.status=passed"],
                validation_requirements=["EXECUTION_REPORT.json must be valid."],
                target_task=TargetTaskDraft(id="draft-task-1", title="Draft"),
            )

    def test_task_draft_rejects_unsafe_file_path(self) -> None:
        with self.assertRaisesRegex(ValueError, "task draft file path"):
            TaskDraft(
                draft_id="draft_1",
                title="Draft",
                objective="Objective",
                non_goals=["No unrelated cleanup."],
                files_forbidden=["../escape.py"],
                invariants=["Tests must pass."],
                tests_required=["Add tests if source changes."],
                commands_to_run=["python -m unittest discover -s tests"],
                acceptance_criteria=["tests.status=passed"],
                validation_requirements=["EXECUTION_REPORT.json must be valid."],
                target_task=TargetTaskDraft(id="draft-task-1", title="Draft"),
            )


class DraftTaskScaffoldCliTests(unittest.TestCase):
    def test_draft_task_scaffold_creates_expected_artifacts(self) -> None:
        with temporary_test_dir() as tmp:
            request_path = tmp / "raw_request.md"
            write_text(
                request_path,
                """
                # Document operator quickstart

                Create a small documentation task draft for operator quickstart guidance.
                """,
            )
            output_dir = tmp / ".task_drafts"
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = draft_task_scaffold_main(
                    ["--request", str(request_path), "--output-dir", str(output_dir)]
                )
            output = stdout.getvalue()
            draft_dir = Path(output_value(output, "draft_dir"))
            task_draft_path = Path(output_value(output, "task_draft"))
            codex_prompt_path = Path(output_value(output, "codex_prompt"))
            task_review_path = Path(output_value(output, "task_review"))
            manifest_path = Path(output_value(output, "manifest"))
            files_created = sorted(path.name for path in draft_dir.iterdir())
            draft_payload = load_yaml(task_draft_path)
            task_draft_exists = task_draft_path.exists()
            codex_prompt_exists = codex_prompt_path.exists()
            task_review_exists = task_review_path.exists()
            manifest_exists = manifest_path.exists()

        self.assertEqual(exit_code, 0, output)
        self.assertEqual(output_value(output, "status"), "draft_created")
        self.assertEqual(output_value(output, "next_action"), "validate_task_draft")
        self.assertEqual(
            files_created,
            ["MANIFEST.json", "codex_prompt.md", "raw_request.md", "task_draft.yaml", "task_review.md"],
        )
        self.assertTrue(task_draft_exists)
        self.assertTrue(codex_prompt_exists)
        self.assertTrue(task_review_exists)
        self.assertTrue(manifest_exists)
        self.assertEqual(draft_payload["schema_version"], "1.0")
        self.assertFalse(draft_payload["target_task"]["enabled"])
        self.assertEqual(draft_payload["target_task"]["backend"], "codex_cli")
        self.assertEqual(draft_payload["prompt_language"], "ru")

    def test_raw_request_is_copied_exactly(self) -> None:
        with temporary_test_dir() as tmp:
            request_path = tmp / "raw_request.md"
            request_text = "# Draft request\n\nLine one.\nLine two.\n"
            request_path.write_text(request_text, encoding="utf-8")
            output_dir = tmp / ".task_drafts"
            stdout = StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(
                    draft_task_scaffold_main(["--request", str(request_path), "--output-dir", str(output_dir)]),
                    0,
                )
            raw_request_copy = Path(output_value(stdout.getvalue(), "draft_dir")) / "raw_request.md"
            copied_text = raw_request_copy.read_text(encoding="utf-8")

        self.assertEqual(copied_text, request_text)

    def test_task_draft_yaml_parses_and_contains_safe_placeholders(self) -> None:
        with temporary_test_dir() as tmp:
            request_path = tmp / "raw_request.md"
            write_text(
                request_path,
                """
                # Add CLI status docs

                Create documentation that explains the status lifecycle.
                """,
            )
            output_dir = tmp / ".task_drafts"
            stdout = StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(
                    draft_task_scaffold_main(["--request", str(request_path), "--output-dir", str(output_dir)]),
                    0,
                )
            draft_model = TaskDraft.model_validate(load_yaml(Path(output_value(stdout.getvalue(), "task_draft"))))

        self.assertIn("Do not weaken validation, review, apply, or safety gates.", draft_model.non_goals)
        self.assertIn("src/ai_orchestrator/apply.py", draft_model.files_forbidden)
        self.assertIn("python -m unittest discover -s tests", draft_model.commands_to_run)
        self.assertEqual(draft_model.target_task.seed_workspace, ".")
        self.assertEqual(draft_model.optional_review_profiles, ["qa", "architecture"])
        self.assertEqual(draft_model.required_review_profiles, [])

    def test_custom_title_task_id_risk_level_and_prompt_language_are_applied(self) -> None:
        with temporary_test_dir() as tmp:
            request_path = tmp / "raw_request.md"
            write_text(request_path, "Simple request body.\n")
            output_dir = tmp / ".task_drafts"
            stdout = StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(
                    draft_task_scaffold_main(
                        [
                            "--request",
                            str(request_path),
                            "--output-dir",
                            str(output_dir),
                            "--title",
                            "Custom Draft Title",
                            "--task-id",
                            "custom-draft-task",
                            "--risk-level",
                            "critical",
                            "--prompt-language",
                            "en",
                        ]
                    ),
                    0,
                )
            draft_model = TaskDraft.model_validate(load_yaml(Path(output_value(stdout.getvalue(), "task_draft"))))

        self.assertEqual(draft_model.title, "Custom Draft Title")
        self.assertEqual(draft_model.target_task.id, "custom-draft-task")
        self.assertEqual(draft_model.risk_level, "critical")
        self.assertEqual(draft_model.prompt_language, "en")
        self.assertEqual(draft_model.required_review_profiles, ["security", "qa", "architecture", "ops"])

    def test_scaffold_json_output_is_valid(self) -> None:
        with temporary_test_dir() as tmp:
            request_path = tmp / "raw_request.md"
            write_text(request_path, "Draft JSON output request.\n")
            output_dir = tmp / ".task_drafts"
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = draft_task_scaffold_main(
                    ["--request", str(request_path), "--output-dir", str(output_dir), "--format", "json"]
                )
            payload = json.loads(stdout.getvalue())

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "draft_created")
        self.assertEqual(payload["next_action"], "validate_task_draft")
        self.assertTrue(payload["draft_id"].startswith("draft_"))

    def test_codex_prompt_and_task_review_contain_expected_markers(self) -> None:
        with temporary_test_dir() as tmp:
            request_path = tmp / "raw_request.md"
            write_text(request_path, "# Improve task draft\n\nClarify task boundaries.\n")
            output_dir = tmp / ".task_drafts"
            stdout = StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(
                    draft_task_scaffold_main(["--request", str(request_path), "--output-dir", str(output_dir)]),
                    0,
                )
            codex_prompt_text = Path(output_value(stdout.getvalue(), "codex_prompt")).read_text(encoding="utf-8")
            task_review_text = Path(output_value(stdout.getvalue(), "task_review")).read_text(encoding="utf-8")

        self.assertIn("# Prompt For Future Draft Improvement", codex_prompt_text)
        self.assertIn("Do not modify tasks.yaml.", codex_prompt_text)
        self.assertIn("# Task Draft Review Checklist", task_review_text)
        self.assertIn("target_task.enabled remains false until explicit promotion.", task_review_text)

    def test_bom_in_request_file_does_not_leak_into_title_or_objective(self) -> None:
        with temporary_test_dir() as tmp:
            request_path = tmp / "raw_request.md"
            request_path.write_text("# BOM title\n\nBody.\n", encoding="utf-8-sig")
            output_dir = tmp / ".task_drafts"
            stdout = StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(
                    draft_task_scaffold_main(["--request", str(request_path), "--output-dir", str(output_dir)]),
                    0,
                )
            draft_model = TaskDraft.model_validate(load_yaml(Path(output_value(stdout.getvalue(), "task_draft"))))

        self.assertEqual(draft_model.title, "BOM title")
        self.assertEqual(draft_model.objective, "BOM title")

    def test_scaffold_does_not_create_tasks_yaml_or_validator_report(self) -> None:
        with temporary_test_dir() as tmp:
            request_path = tmp / "raw_request.md"
            write_text(request_path, "Draft without promotion.\n")
            output_dir = tmp / ".task_drafts"
            stdout = StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(
                    draft_task_scaffold_main(["--request", str(request_path), "--output-dir", str(output_dir)]),
                    0,
                )
            draft_dir = Path(output_value(stdout.getvalue(), "draft_dir"))
            validator_report_exists = (draft_dir / "task_draft_validator_report.json").exists()
            tasks_yaml_exists = (tmp / "tasks.yaml").exists()

        self.assertFalse(validator_report_exists)
        self.assertFalse(tasks_yaml_exists)

    def test_missing_request_file_fails_clearly(self) -> None:
        with temporary_test_dir() as tmp:
            missing_request = tmp / "missing_request.md"
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = draft_task_scaffold_main(
                    ["--request", str(missing_request), "--output-dir", str(tmp / ".task_drafts")]
                )
            output = stdout.getvalue()

        self.assertEqual(exit_code, 1)
        self.assertIn("status=failed", output)
        self.assertIn("raw request file not found", output)
