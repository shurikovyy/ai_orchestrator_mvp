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
from ai_orchestrator.task_drafts import TaskDraftManifest, load_task_draft, load_task_draft_manifest
from ai_orchestrator.task_draft_validation import TaskDraftValidationReport

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


def save_yaml(path: Path, payload: object) -> None:
    import yaml

    with path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(payload, handle, allow_unicode=True, sort_keys=False, default_flow_style=False)


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
    draft_id = output_value(output, "draft_id")
    draft_dir = Path(output_value(output, "draft_dir"))
    return draft_id, draft_dir


class TaskDraftValidationCliTests(unittest.TestCase):
    def test_validate_task_draft_creates_json_and_markdown_reports(self) -> None:
        with temporary_test_dir() as tmp:
            draft_id, draft_dir = scaffold_draft(
                tmp,
                request_text="# Add docs draft\n\nCreate documentation for the operator workflow.\n",
            )
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = validate_task_draft_main([draft_id, "--drafts-dir", str(tmp / ".task_drafts")])
            output = stdout.getvalue()
            report_json_path = Path(output_value(output, "validator_report"))
            report_md_path = Path(output_value(output, "validator_report_markdown"))
            report_json_exists = report_json_path.exists()
            report_md_exists = report_md_path.exists()

        self.assertEqual(exit_code, 0, output)
        self.assertTrue(report_json_exists)
        self.assertTrue(report_md_exists)
        self.assertEqual(output_value(output, "status"), "validated")

    def test_default_like_scaffold_with_open_questions_is_needs_revision(self) -> None:
        with temporary_test_dir() as tmp:
            draft_id, _draft_dir = scaffold_draft(
                tmp,
                request_text="# Add docs draft\n\nCreate documentation for the operator workflow.\n",
                risk_level="medium",
            )
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = validate_task_draft_main([draft_id, "--drafts-dir", str(tmp / ".task_drafts")])
            output = stdout.getvalue()
            report = TaskDraftValidationReport.model_validate_json(
                Path(output_value(output, "validator_report")).read_text(encoding="utf-8")
            )

        self.assertEqual(exit_code, 0, output)
        self.assertEqual(report.validation_status, "needs_revision")
        self.assertFalse(report.valid_for_promotion)
        self.assertTrue(any(f.field == "open_questions" for f in report.findings))

    def test_cleared_open_questions_and_safe_scope_can_be_valid(self) -> None:
        with temporary_test_dir() as tmp:
            draft_id, draft_dir = scaffold_draft(
                tmp,
                request_text="# Add docs draft\n\nCreate documentation for the operator workflow.\n",
                risk_level="medium",
            )
            task_draft_path = draft_dir / "task_draft.yaml"
            payload = load_yaml(task_draft_path)
            payload["open_questions"] = []
            payload["files_allowed"] = ["docs"]
            save_yaml(task_draft_path, payload)
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = validate_task_draft_main([draft_id, "--drafts-dir", str(tmp / ".task_drafts")])
            output = stdout.getvalue()
            report = TaskDraftValidationReport.model_validate_json(
                Path(output_value(output, "validator_report")).read_text(encoding="utf-8")
            )

        self.assertEqual(exit_code, 0, output)
        self.assertEqual(report.validation_status, "valid")
        self.assertTrue(report.valid_for_promotion)

    def test_missing_artifact_creates_invalid_report(self) -> None:
        with temporary_test_dir() as tmp:
            draft_id, draft_dir = scaffold_draft(
                tmp,
                request_text="# Add docs draft\n\nCreate documentation for the operator workflow.\n",
            )
            (draft_dir / "codex_prompt.md").unlink()
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = validate_task_draft_main([draft_id, "--drafts-dir", str(tmp / ".task_drafts")])
            output = stdout.getvalue()
            report = TaskDraftValidationReport.model_validate_json(
                Path(output_value(output, "validator_report")).read_text(encoding="utf-8")
            )

        self.assertEqual(exit_code, 0, output)
        self.assertEqual(report.validation_status, "invalid")
        self.assertTrue(any("required draft artifact missing: codex_prompt.md" in f.message for f in report.findings))

    def test_target_task_enabled_true_is_error(self) -> None:
        with temporary_test_dir() as tmp:
            draft_id, draft_dir = scaffold_draft(tmp, request_text="# Docs\n\nDocument flow.\n")
            payload = load_yaml(draft_dir / "task_draft.yaml")
            payload["open_questions"] = []
            payload["target_task"]["enabled"] = True
            save_yaml(draft_dir / "task_draft.yaml", payload)
            stdout = StringIO()
            with redirect_stdout(stdout):
                validate_task_draft_main([draft_id, "--drafts-dir", str(tmp / ".task_drafts")])
            report = TaskDraftValidationReport.model_validate_json(
                Path(output_value(stdout.getvalue(), "validator_report")).read_text(encoding="utf-8")
            )

        self.assertTrue(any(f.field == "target_task.enabled" for f in report.findings))
        self.assertEqual(report.validation_status, "invalid")

    def test_missing_non_goals_is_error(self) -> None:
        with temporary_test_dir() as tmp:
            draft_id, draft_dir = scaffold_draft(tmp, request_text="# Docs\n\nDocument flow.\n")
            payload = load_yaml(draft_dir / "task_draft.yaml")
            payload["open_questions"] = []
            payload["non_goals"] = []
            save_yaml(draft_dir / "task_draft.yaml", payload)
            stdout = StringIO()
            with redirect_stdout(stdout):
                validate_task_draft_main([draft_id, "--drafts-dir", str(tmp / ".task_drafts")])
            report = TaskDraftValidationReport.model_validate_json(
                Path(output_value(stdout.getvalue(), "validator_report")).read_text(encoding="utf-8")
            )

        self.assertTrue(any("non_goals" in (f.field or "") for f in report.findings))
        self.assertEqual(report.validation_status, "invalid")

    def test_missing_files_forbidden_is_error(self) -> None:
        with temporary_test_dir() as tmp:
            draft_id, draft_dir = scaffold_draft(tmp, request_text="# Docs\n\nDocument flow.\n")
            payload = load_yaml(draft_dir / "task_draft.yaml")
            payload["open_questions"] = []
            payload["files_forbidden"] = []
            save_yaml(draft_dir / "task_draft.yaml", payload)
            stdout = StringIO()
            with redirect_stdout(stdout):
                validate_task_draft_main([draft_id, "--drafts-dir", str(tmp / ".task_drafts")])
            report = TaskDraftValidationReport.model_validate_json(
                Path(output_value(stdout.getvalue(), "validator_report")).read_text(encoding="utf-8")
            )

        self.assertTrue(any("files_forbidden" in (f.field or "") for f in report.findings))
        self.assertEqual(report.validation_status, "invalid")

    def test_empty_files_allowed_is_warning_and_blocks_promotion(self) -> None:
        with temporary_test_dir() as tmp:
            draft_id, draft_dir = scaffold_draft(tmp, request_text="General request without scope hint.\n", risk_level="medium")
            payload = load_yaml(draft_dir / "task_draft.yaml")
            payload["open_questions"] = []
            save_yaml(draft_dir / "task_draft.yaml", payload)
            stdout = StringIO()
            with redirect_stdout(stdout):
                validate_task_draft_main([draft_id, "--drafts-dir", str(tmp / ".task_drafts")])
            report = TaskDraftValidationReport.model_validate_json(
                Path(output_value(stdout.getvalue(), "validator_report")).read_text(encoding="utf-8")
            )

        self.assertTrue(any(f.field == "files_allowed" and f.severity == "warning" for f in report.findings))
        self.assertFalse(report.valid_for_promotion)

    def test_dangerous_command_is_error(self) -> None:
        with temporary_test_dir() as tmp:
            draft_id, draft_dir = scaffold_draft(tmp, request_text="# Docs\n\nDocument flow.\n")
            payload = load_yaml(draft_dir / "task_draft.yaml")
            payload["commands_to_run"] = ["git push origin main"]
            payload["open_questions"] = []
            save_yaml(draft_dir / "task_draft.yaml", payload)
            stdout = StringIO()
            with redirect_stdout(stdout):
                validate_task_draft_main([draft_id, "--drafts-dir", str(tmp / ".task_drafts")])
            report = TaskDraftValidationReport.model_validate_json(
                Path(output_value(stdout.getvalue(), "validator_report")).read_text(encoding="utf-8")
            )

        self.assertTrue(any("dangerous command snippet" in f.message for f in report.findings))
        self.assertEqual(report.validation_status, "invalid")

    def test_missing_unittest_command_is_warning(self) -> None:
        with temporary_test_dir() as tmp:
            draft_id, draft_dir = scaffold_draft(tmp, request_text="# Docs\n\nDocument flow.\n", risk_level="medium")
            payload = load_yaml(draft_dir / "task_draft.yaml")
            payload["open_questions"] = []
            payload["commands_to_run"] = ["python -m pytest tests/test_specific.py"]
            save_yaml(draft_dir / "task_draft.yaml", payload)
            stdout = StringIO()
            with redirect_stdout(stdout):
                validate_task_draft_main([draft_id, "--drafts-dir", str(tmp / ".task_drafts")])
            report = TaskDraftValidationReport.model_validate_json(
                Path(output_value(stdout.getvalue(), "validator_report")).read_text(encoding="utf-8")
            )

        self.assertTrue(any("commands_to_run should include `python -m unittest discover -s tests`" in f.message for f in report.findings))
        self.assertEqual(report.validation_status, "needs_revision")

    def test_unknown_required_reviewer_profile_is_error(self) -> None:
        with temporary_test_dir() as tmp:
            draft_id, draft_dir = scaffold_draft(tmp, request_text="# Docs\n\nDocument flow.\n")
            payload = load_yaml(draft_dir / "task_draft.yaml")
            payload["required_review_profiles"] = ["made_up_profile"]
            payload["open_questions"] = []
            save_yaml(draft_dir / "task_draft.yaml", payload)
            stdout = StringIO()
            with redirect_stdout(stdout):
                validate_task_draft_main([draft_id, "--drafts-dir", str(tmp / ".task_drafts")])
            report = TaskDraftValidationReport.model_validate_json(
                Path(output_value(stdout.getvalue(), "validator_report")).read_text(encoding="utf-8")
            )

        self.assertTrue(any("unknown review profile" in f.message for f in report.findings))
        self.assertEqual(report.validation_status, "invalid")

    def test_high_risk_without_required_reviewers_is_warning(self) -> None:
        with temporary_test_dir() as tmp:
            draft_id, draft_dir = scaffold_draft(tmp, request_text="# Docs\n\nDocument flow.\n", risk_level="high")
            payload = load_yaml(draft_dir / "task_draft.yaml")
            payload["open_questions"] = []
            payload["required_review_profiles"] = []
            save_yaml(draft_dir / "task_draft.yaml", payload)
            stdout = StringIO()
            with redirect_stdout(stdout):
                validate_task_draft_main([draft_id, "--drafts-dir", str(tmp / ".task_drafts")])
            report = TaskDraftValidationReport.model_validate_json(
                Path(output_value(stdout.getvalue(), "validator_report")).read_text(encoding="utf-8")
            )

        self.assertTrue(any("high/critical risk draft has no required_review_profiles" in f.message for f in report.findings))
        self.assertEqual(report.validation_status, "needs_revision")

    def test_unsafe_file_path_is_error(self) -> None:
        with temporary_test_dir() as tmp:
            draft_id, draft_dir = scaffold_draft(tmp, request_text="# Docs\n\nDocument flow.\n")
            payload = load_yaml(draft_dir / "task_draft.yaml")
            payload["files_allowed"] = ["../escape.py"]
            payload["open_questions"] = []
            save_yaml(draft_dir / "task_draft.yaml", payload)
            stdout = StringIO()
            with redirect_stdout(stdout):
                validate_task_draft_main([draft_id, "--drafts-dir", str(tmp / ".task_drafts")])
            report = TaskDraftValidationReport.model_validate_json(
                Path(output_value(stdout.getvalue(), "validator_report")).read_text(encoding="utf-8")
            )

        self.assertTrue(any("task_draft.yaml validation error for files_allowed" in f.message for f in report.findings))
        self.assertEqual(report.validation_status, "invalid")

    def test_codex_prompt_missing_execution_report_instruction_is_error(self) -> None:
        with temporary_test_dir() as tmp:
            draft_id, draft_dir = scaffold_draft(tmp, request_text="# Docs\n\nDocument flow.\n")
            prompt_path = draft_dir / "codex_prompt.md"
            prompt_text = prompt_path.read_text(encoding="utf-8").replace("EXECUTION_REPORT.json", "EXECUTION_REPORT_PLACEHOLDER")
            prompt_path.write_text(prompt_text, encoding="utf-8")
            stdout = StringIO()
            with redirect_stdout(stdout):
                validate_task_draft_main([draft_id, "--drafts-dir", str(tmp / ".task_drafts")])
            report = TaskDraftValidationReport.model_validate_json(
                Path(output_value(stdout.getvalue(), "validator_report")).read_text(encoding="utf-8")
            )

        self.assertTrue(any("must instruct the future executor to create EXECUTION_REPORT.json" in f.message for f in report.findings))
        self.assertEqual(report.validation_status, "invalid")

    def test_manifest_is_updated(self) -> None:
        with temporary_test_dir() as tmp:
            draft_id, draft_dir = scaffold_draft(tmp, request_text="# Docs\n\nDocument flow.\n")
            stdout = StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(validate_task_draft_main([draft_id, "--drafts-dir", str(tmp / ".task_drafts")]), 0)
            manifest = load_task_draft_manifest(draft_dir / "MANIFEST.json")

        self.assertEqual(manifest.validation_status, "needs_revision")
        self.assertFalse(manifest.valid_for_promotion)
        self.assertIsNotNone(manifest.validated_at)
        self.assertTrue(manifest.validator_report)
        self.assertTrue(manifest.validator_report_md)

    def test_validate_task_draft_clears_stale_reason_after_revision(self) -> None:
        with temporary_test_dir() as tmp:
            draft_id, draft_dir = scaffold_draft(tmp, request_text="# Docs\n\nDocument flow.\n")
            with redirect_stdout(StringIO()):
                self.assertEqual(validate_task_draft_main([draft_id, "--drafts-dir", str(tmp / ".task_drafts")]), 0)
            with redirect_stdout(StringIO()):
                self.assertEqual(
                    revise_task_draft_main(
                        [
                            draft_id,
                            "--drafts-dir",
                            str(tmp / ".task_drafts"),
                            "--risk-level",
                            "low",
                            "--clear-files-allowed",
                            "--allow-file",
                            "docs/example.md",
                            "--clear-open-questions",
                        ]
                    ),
                    0,
                )
            stale_manifest = load_task_draft_manifest(draft_dir / "MANIFEST.json")
            with redirect_stdout(StringIO()):
                self.assertEqual(
                    validate_task_draft_main([draft_id, "--drafts-dir", str(tmp / ".task_drafts"), "--force"]),
                    0,
                )
            current_manifest = load_task_draft_manifest(draft_dir / "MANIFEST.json")

        self.assertEqual(stale_manifest.validation_status, "stale")
        self.assertFalse(stale_manifest.valid_for_promotion)
        self.assertEqual(stale_manifest.validation_stale_reason, "task draft revised after last validation")
        self.assertIn(current_manifest.validation_status, {"valid", "needs_revision", "invalid"})
        self.assertIsNone(current_manifest.validation_stale_reason)
        self.assertIsNotNone(current_manifest.validated_at)

    def test_force_required_to_overwrite_existing_validator_report(self) -> None:
        with temporary_test_dir() as tmp:
            draft_id, _draft_dir = scaffold_draft(tmp, request_text="# Docs\n\nDocument flow.\n")
            with redirect_stdout(StringIO()):
                self.assertEqual(validate_task_draft_main([draft_id, "--drafts-dir", str(tmp / ".task_drafts")]), 0)
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = validate_task_draft_main([draft_id, "--drafts-dir", str(tmp / ".task_drafts")])
            output = stdout.getvalue()

        self.assertEqual(exit_code, 1)
        self.assertIn("task draft validator report already exists", output)

    def test_validate_task_draft_force_overwrites_existing_report(self) -> None:
        with temporary_test_dir() as tmp:
            draft_id, draft_dir = scaffold_draft(tmp, request_text="# Docs\n\nDocument flow.\n")
            with redirect_stdout(StringIO()):
                self.assertEqual(validate_task_draft_main([draft_id, "--drafts-dir", str(tmp / ".task_drafts")]), 0)
            report_path = draft_dir / "task_draft_validator_report.json"
            report_path.write_text("{}", encoding="utf-8")
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = validate_task_draft_main([draft_id, "--drafts-dir", str(tmp / ".task_drafts"), "--force"])
            report = TaskDraftValidationReport.model_validate_json(report_path.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0, stdout.getvalue())
        self.assertEqual(report.draft_id, draft_id)

    def test_validate_task_draft_json_output_is_valid(self) -> None:
        with temporary_test_dir() as tmp:
            draft_id, _draft_dir = scaffold_draft(tmp, request_text="# Docs\n\nDocument flow.\n")
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = validate_task_draft_main(
                    [draft_id, "--drafts-dir", str(tmp / ".task_drafts"), "--format", "json"]
                )
            payload = json.loads(stdout.getvalue())

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "validated")
        self.assertEqual(payload["validation_status"], "needs_revision")

    def test_validate_task_draft_does_not_modify_tasks_yaml_or_create_runs(self) -> None:
        with temporary_test_dir() as tmp:
            draft_id, _draft_dir = scaffold_draft(tmp, request_text="# Docs\n\nDocument flow.\n")
            tasks_yaml = tmp / "tasks.yaml"
            tasks_yaml.write_text("project: local\n", encoding="utf-8")
            original_tasks_yaml = tasks_yaml.read_text(encoding="utf-8")
            with redirect_stdout(StringIO()):
                self.assertEqual(validate_task_draft_main([draft_id, "--drafts-dir", str(tmp / ".task_drafts")]), 0)
            tasks_yaml_after = tasks_yaml.read_text(encoding="utf-8")
            runs_exists = (tmp / ".runs").exists()

        self.assertEqual(original_tasks_yaml, tasks_yaml_after)
        self.assertFalse(runs_exists)
