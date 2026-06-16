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

from ai_orchestrator.cli import (
    draft_task_scaffold_main,
    list_task_drafts_main,
    promote_task_draft_main,
    revise_task_draft_main,
    show_task_draft_main,
    validate_task_draft_main,
)
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


def output_line_for_draft(output: str, draft_id: str) -> str:
    prefix = f"draft_id={draft_id} "
    for line in output.splitlines():
        if line.startswith(prefix):
            return line
    raise AssertionError(f"missing output row for draft {draft_id!r} in:\n{output}")


def load_yaml(path: Path) -> object:
    import yaml

    return yaml.safe_load(path.read_text(encoding="utf-8"))


def scaffold_draft(
    tmp: Path,
    *,
    request_text: str = "# Show draft\n\nDocument task draft inspection.\n",
    risk_level: str = "low",
    task_id: str = "show-task-draft-test",
) -> tuple[str, Path]:
    request_path = tmp / "raw_request.md"
    write_text(request_path, request_text)
    output_dir = tmp / ".task_drafts"
    stdout = StringIO()
    with redirect_stdout(stdout):
        exit_code = draft_task_scaffold_main(
            [
                "--request",
                str(request_path),
                "--output-dir",
                str(output_dir),
                "--risk-level",
                risk_level,
                "--task-id",
                task_id,
            ]
        )
    output = stdout.getvalue()
    if exit_code != 0:
        raise AssertionError(output)
    return output_value(output, "draft_id"), Path(output_value(output, "draft_dir"))


def revise_draft_to_valid(tmp: Path, draft_id: str) -> None:
    stdout = StringIO()
    with redirect_stdout(stdout):
        exit_code = revise_task_draft_main(
            [
                draft_id,
                "--drafts-dir",
                str(tmp / ".task_drafts"),
                "--risk-level",
                "low",
                "--clear-files-allowed",
                "--allow-file",
                "docs/show_task_draft.md",
                "--clear-open-questions",
            ]
        )
    if exit_code != 0:
        raise AssertionError(stdout.getvalue())


def validate_draft(tmp: Path, draft_id: str, *extra_args: str) -> str:
    stdout = StringIO()
    with redirect_stdout(stdout):
        exit_code = validate_task_draft_main([draft_id, "--drafts-dir", str(tmp / ".task_drafts"), *extra_args])
    output = stdout.getvalue()
    if exit_code != 0:
        raise AssertionError(output)
    return output


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


class ShowTaskDraftCliTests(unittest.TestCase):
    def test_missing_draft_id_fails_clearly(self) -> None:
        with temporary_test_dir() as tmp:
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = show_task_draft_main(["missing-draft", "--drafts-dir", str(tmp / ".task_drafts")])
            output = stdout.getvalue()

        self.assertEqual(exit_code, 1, output)
        self.assertEqual(output_value(output, "draft_id"), "missing-draft")
        self.assertEqual(output_value(output, "status"), "failed")
        self.assertIn("task draft not found: missing-draft", output)

    def test_scaffolded_unvalidated_draft_points_to_validation(self) -> None:
        with temporary_test_dir() as tmp:
            draft_id, _draft_dir = scaffold_draft(tmp)
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = show_task_draft_main([draft_id, "--drafts-dir", str(tmp / ".task_drafts")])
            output = stdout.getvalue()

        self.assertEqual(exit_code, 0, output)
        self.assertEqual(output_value(output, "draft_id"), draft_id)
        self.assertEqual(output_value(output, "validation_status"), "missing")
        self.assertEqual(output_value(output, "valid_for_promotion"), "unknown")
        self.assertEqual(output_value(output, "target_enabled"), "false")
        self.assertEqual(output_value(output, "next_action"), "validate_task_draft")

    def test_stale_validation_after_revision_points_to_validation(self) -> None:
        with temporary_test_dir() as tmp:
            draft_id, _draft_dir = scaffold_draft(tmp)
            validate_draft(tmp, draft_id)
            stdout = StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(
                    revise_task_draft_main(
                        [draft_id, "--drafts-dir", str(tmp / ".task_drafts"), "--add-assumption", "Needs another pass."]
                    ),
                    0,
                )
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = show_task_draft_main([draft_id, "--drafts-dir", str(tmp / ".task_drafts")])
            output = stdout.getvalue()

        self.assertEqual(exit_code, 0, output)
        self.assertEqual(output_value(output, "validation_status"), "stale")
        self.assertEqual(output_value(output, "valid_for_promotion"), "false")
        self.assertEqual(output_value(output, "next_action"), "validate_task_draft")

    def test_needs_revision_draft_points_to_revision(self) -> None:
        with temporary_test_dir() as tmp:
            draft_id, _draft_dir = scaffold_draft(tmp)
            validate_draft(tmp, draft_id)
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = show_task_draft_main([draft_id, "--drafts-dir", str(tmp / ".task_drafts")])
            output = stdout.getvalue()

        self.assertEqual(exit_code, 0, output)
        self.assertEqual(output_value(output, "validation_status"), "needs_revision")
        self.assertEqual(output_value(output, "valid_for_promotion"), "false")
        self.assertEqual(output_value(output, "next_action"), "revise_task_draft")

    def test_valid_draft_points_to_promotion(self) -> None:
        with temporary_test_dir() as tmp:
            draft_id, _draft_dir = scaffold_draft(tmp)
            revise_draft_to_valid(tmp, draft_id)
            validate_draft(tmp, draft_id)
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = show_task_draft_main([draft_id, "--drafts-dir", str(tmp / ".task_drafts")])
            output = stdout.getvalue()

        self.assertEqual(exit_code, 0, output)
        self.assertEqual(output_value(output, "validation_status"), "valid")
        self.assertEqual(output_value(output, "valid_for_promotion"), "true")
        self.assertEqual(output_value(output, "open_questions"), "0")
        self.assertEqual(output_value(output, "files_allowed"), "1")
        self.assertEqual(output_value(output, "next_action"), "promote_task_draft")

    def test_promoted_draft_points_to_promoted_task_inspection(self) -> None:
        with temporary_test_dir() as tmp:
            draft_id, _draft_dir = scaffold_draft(tmp)
            revise_draft_to_valid(tmp, draft_id)
            validate_draft(tmp, draft_id)
            stdout = StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(
                    promote_task_draft_main(
                        [draft_id, "--drafts-dir", str(tmp / ".task_drafts"), "--tasks-file", str(tmp / "tasks.yaml")]
                    ),
                    0,
                )
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = show_task_draft_main([draft_id, "--drafts-dir", str(tmp / ".task_drafts")])
            output = stdout.getvalue()

        self.assertEqual(exit_code, 0, output)
        self.assertEqual(output_value(output, "validation_status"), "valid")
        self.assertEqual(output_value(output, "next_action"), "inspect_promoted_task")

    def test_show_task_draft_is_read_only_for_core_artifacts(self) -> None:
        with temporary_test_dir() as tmp:
            draft_id, draft_dir = scaffold_draft(tmp)
            task_draft_path = draft_dir / "task_draft.yaml"
            manifest_path = draft_dir / "MANIFEST.json"
            before_task_draft = task_draft_path.read_text(encoding="utf-8")
            before_manifest = manifest_path.read_text(encoding="utf-8")
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = show_task_draft_main([draft_id, "--drafts-dir", str(tmp / ".task_drafts")])
            output = stdout.getvalue()
            after_task_draft = task_draft_path.read_text(encoding="utf-8")
            after_manifest = manifest_path.read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0, output)
        self.assertEqual(after_task_draft, before_task_draft)
        self.assertEqual(after_manifest, before_manifest)

    def test_show_task_draft_json_output_is_parseable(self) -> None:
        with temporary_test_dir() as tmp:
            draft_id, _draft_dir = scaffold_draft(tmp)
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = show_task_draft_main(
                    [draft_id, "--drafts-dir", str(tmp / ".task_drafts"), "--format", "json"]
                )
            payload = json.loads(stdout.getvalue())

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["draft_id"], draft_id)
        self.assertEqual(payload["validation_status"], "missing")
        self.assertEqual(payload["valid_for_promotion"], None)
        self.assertEqual(payload["next_action"], "validate_task_draft")
        self.assertIn("paths", payload)
        self.assertIn("target_task", payload)

    def test_show_task_draft_show_paths_includes_resolved_paths(self) -> None:
        with temporary_test_dir() as tmp:
            draft_id, draft_dir = scaffold_draft(tmp)
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = show_task_draft_main(
                    [draft_id, "--drafts-dir", str(tmp / ".task_drafts"), "--show-paths"]
                )
            output = stdout.getvalue()

        self.assertEqual(exit_code, 0, output)
        self.assertEqual(output_value(output, "draft_dir"), str(draft_dir.resolve()))
        self.assertEqual(output_value(output, "task_draft"), str((draft_dir / "task_draft.yaml").resolve()))


class ListTaskDraftsCliTests(unittest.TestCase):
    def test_missing_drafts_dir_returns_empty_text_and_json(self) -> None:
        with temporary_test_dir() as tmp:
            drafts_dir = tmp / ".task_drafts"
            stdout = StringIO()
            with redirect_stdout(stdout):
                text_exit_code = list_task_drafts_main(["--drafts-dir", str(drafts_dir)])
            text_output = stdout.getvalue()
            stdout = StringIO()
            with redirect_stdout(stdout):
                json_exit_code = list_task_drafts_main(["--drafts-dir", str(drafts_dir), "--format", "json"])
            payload = json.loads(stdout.getvalue())

        self.assertEqual(text_exit_code, 0, text_output)
        self.assertIn(f"No task drafts found in {drafts_dir}", text_output)
        self.assertEqual(json_exit_code, 0)
        self.assertEqual(payload["count"], 0)
        self.assertEqual(payload["drafts"], [])

    def test_list_includes_unvalidated_stale_and_valid_drafts(self) -> None:
        with temporary_test_dir() as tmp:
            missing_id, _missing_dir = scaffold_draft(tmp, task_id="missing-draft-task")
            stale_id, _stale_dir = scaffold_draft(tmp, task_id="stale-draft-task")
            validate_draft(tmp, stale_id)
            stdout = StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(
                    revise_task_draft_main(
                        [stale_id, "--drafts-dir", str(tmp / ".task_drafts"), "--add-assumption", "Needs revalidation."]
                    ),
                    0,
                )
            valid_id, _valid_dir = scaffold_draft(tmp, task_id="valid-draft-task")
            revise_draft_to_valid(tmp, valid_id)
            validate_draft(tmp, valid_id)
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = list_task_drafts_main(["--drafts-dir", str(tmp / ".task_drafts")])
            output = stdout.getvalue()

        self.assertEqual(exit_code, 0, output)
        self.assertIn("validation_status=missing", output_line_for_draft(output, missing_id))
        self.assertIn("next_action=validate_task_draft", output_line_for_draft(output, missing_id))
        self.assertIn("validation_status=stale", output_line_for_draft(output, stale_id))
        self.assertIn("next_action=validate_task_draft", output_line_for_draft(output, stale_id))
        self.assertIn("validation_status=valid", output_line_for_draft(output, valid_id))
        self.assertIn("valid_for_promotion=true", output_line_for_draft(output, valid_id))
        self.assertIn("next_action=promote_task_draft", output_line_for_draft(output, valid_id))

    def test_list_filters_by_status_and_next_action(self) -> None:
        with temporary_test_dir() as tmp:
            missing_id, _missing_dir = scaffold_draft(tmp, task_id="missing-filter-task")
            valid_id, _valid_dir = scaffold_draft(tmp, task_id="valid-filter-task")
            revise_draft_to_valid(tmp, valid_id)
            validate_draft(tmp, valid_id)
            stdout = StringIO()
            with redirect_stdout(stdout):
                status_exit_code = list_task_drafts_main(["--drafts-dir", str(tmp / ".task_drafts"), "--status", "valid"])
            status_output = stdout.getvalue()
            stdout = StringIO()
            with redirect_stdout(stdout):
                action_exit_code = list_task_drafts_main(
                    ["--drafts-dir", str(tmp / ".task_drafts"), "--next-action", "validate_task_draft"]
                )
            action_output = stdout.getvalue()

        self.assertEqual(status_exit_code, 0, status_output)
        self.assertIn(f"draft_id={valid_id}", status_output)
        self.assertNotIn(f"draft_id={missing_id}", status_output)
        self.assertEqual(action_exit_code, 0, action_output)
        self.assertIn(f"draft_id={missing_id}", action_output)
        self.assertNotIn(f"draft_id={valid_id}", action_output)

    def test_list_json_output_is_parseable_and_compact(self) -> None:
        with temporary_test_dir() as tmp:
            draft_id, _draft_dir = scaffold_draft(tmp, task_id="json-list-task")
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = list_task_drafts_main(["--drafts-dir", str(tmp / ".task_drafts"), "--format", "json"])
            payload = json.loads(stdout.getvalue())

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["drafts"][0]["draft_id"], draft_id)
        self.assertEqual(payload["drafts"][0]["validation_status"], "missing")
        self.assertEqual(payload["drafts"][0]["next_action"], "validate_task_draft")
        self.assertNotIn("codex_prompt", payload["drafts"][0])

    def test_list_show_paths_includes_draft_dir(self) -> None:
        with temporary_test_dir() as tmp:
            draft_id, draft_dir = scaffold_draft(tmp, task_id="paths-list-task")
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = list_task_drafts_main(["--drafts-dir", str(tmp / ".task_drafts"), "--show-paths"])
            output = stdout.getvalue()

        self.assertEqual(exit_code, 0, output)
        row = output_line_for_draft(output, draft_id)
        self.assertIn(f"draft_dir={draft_dir.resolve()}", row)

    def test_list_task_drafts_is_read_only_for_core_artifacts(self) -> None:
        with temporary_test_dir() as tmp:
            _draft_id, draft_dir = scaffold_draft(tmp, task_id="readonly-list-task")
            task_draft_path = draft_dir / "task_draft.yaml"
            manifest_path = draft_dir / "MANIFEST.json"
            before_task_draft = task_draft_path.read_text(encoding="utf-8")
            before_manifest = manifest_path.read_text(encoding="utf-8")
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = list_task_drafts_main(["--drafts-dir", str(tmp / ".task_drafts")])
            output = stdout.getvalue()
            after_task_draft = task_draft_path.read_text(encoding="utf-8")
            after_manifest = manifest_path.read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0, output)
        self.assertEqual(after_task_draft, before_task_draft)
        self.assertEqual(after_manifest, before_manifest)

    def test_list_ignores_raw_requests_and_other_helper_dirs(self) -> None:
        with temporary_test_dir() as tmp:
            drafts_dir = tmp / ".task_drafts"
            write_text(drafts_dir / "raw_requests" / "request.md", "Raw request only.\n")
            (drafts_dir / "scratch").mkdir(parents=True)
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = list_task_drafts_main(["--drafts-dir", str(drafts_dir)])
            output = stdout.getvalue()

        self.assertEqual(exit_code, 0, output)
        self.assertIn(f"No task drafts found in {drafts_dir}", output)


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
