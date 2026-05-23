from __future__ import annotations

from contextlib import contextmanager, redirect_stdout
from io import StringIO
from pathlib import Path
import json
import shutil
import sys
import textwrap
import unittest
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_orchestrator import task_draft_promotion as promotion_module
from ai_orchestrator.cli import (
    draft_task_scaffold_main,
    promote_task_draft_main,
    revise_task_draft_main,
    validate_task_draft_main,
)
from ai_orchestrator.task_drafts import load_task_draft, load_task_draft_manifest
from ai_orchestrator.task_queue import load_task_queue_config

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
    task_id: str | None = None,
    title: str | None = None,
) -> tuple[str, Path]:
    request_path = tmp / "raw_request.md"
    write_text(request_path, request_text)
    drafts_dir = tmp / ".task_drafts"
    args = ["--request", str(request_path), "--output-dir", str(drafts_dir), "--risk-level", risk_level]
    if task_id is not None:
        args.extend(["--task-id", task_id])
    if title is not None:
        args.extend(["--title", title])
    stdout = StringIO()
    with redirect_stdout(stdout):
        exit_code = draft_task_scaffold_main(args)
    output = stdout.getvalue()
    if exit_code != 0:
        raise AssertionError(output)
    return output_value(output, "draft_id"), Path(output_value(output, "draft_dir"))


def revise_to_valid(tmp: Path, draft_id: str) -> None:
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
                "docs/example.md",
                "--clear-open-questions",
            ]
        )
    if exit_code != 0:
        raise AssertionError(stdout.getvalue())


def validate_draft(tmp: Path, draft_id: str) -> str:
    stdout = StringIO()
    with redirect_stdout(stdout):
        exit_code = validate_task_draft_main([draft_id, "--drafts-dir", str(tmp / ".task_drafts")])
    output = stdout.getvalue()
    if exit_code != 0:
        raise AssertionError(output)
    return output


def promote_draft(tmp: Path, draft_id: str, *extra_args: str) -> tuple[int, str]:
    stdout = StringIO()
    with redirect_stdout(stdout):
        exit_code = promote_task_draft_main(
            [draft_id, "--drafts-dir", str(tmp / ".task_drafts"), "--tasks-file", str(tmp / "tasks.yaml"), *extra_args]
        )
    return exit_code, stdout.getvalue()


class TaskDraftPromotionCliTests(unittest.TestCase):
    def test_promote_task_draft_creates_tasks_yaml_if_missing(self) -> None:
        with temporary_test_dir() as tmp:
            draft_id, _draft_dir = scaffold_draft(tmp, request_text="# Docs\n\nDocument docs flow.\n")
            revise_to_valid(tmp, draft_id)
            validate_draft(tmp, draft_id)
            exit_code, output = promote_draft(tmp, draft_id)
            tasks_file = tmp / "tasks.yaml"
            tasks_file_exists = tasks_file.exists()
            config = load_task_queue_config(tasks_file)

        self.assertEqual(exit_code, 0, output)
        self.assertTrue(tasks_file_exists)
        self.assertEqual(config.project, "ai_orchestrator_mvp")
        self.assertEqual(output_value(output, "status"), "promoted")

    def test_promoted_task_enabled_false_by_default(self) -> None:
        with temporary_test_dir() as tmp:
            draft_id, draft_dir = scaffold_draft(tmp, request_text="# Docs\n\nDocument docs flow.\n")
            revise_to_valid(tmp, draft_id)
            validate_draft(tmp, draft_id)
            exit_code, output = promote_draft(tmp, draft_id)
            task_id = load_task_draft(draft_dir / "task_draft.yaml").target_task.id
            task = next(task for task in load_task_queue_config(tmp / "tasks.yaml").tasks if task.id == task_id)

        self.assertEqual(exit_code, 0, output)
        self.assertFalse(task.enabled)
        self.assertEqual(output_value(output, "enabled"), "false")

    def test_enable_sets_enabled_true(self) -> None:
        with temporary_test_dir() as tmp:
            draft_id, draft_dir = scaffold_draft(tmp, request_text="# Docs\n\nDocument docs flow.\n")
            revise_to_valid(tmp, draft_id)
            validate_draft(tmp, draft_id)
            exit_code, output = promote_draft(tmp, draft_id, "--enable")
            task_id = load_task_draft(draft_dir / "task_draft.yaml").target_task.id
            task = next(task for task in load_task_queue_config(tmp / "tasks.yaml").tasks if task.id == task_id)

        self.assertEqual(exit_code, 0, output)
        self.assertTrue(task.enabled)
        self.assertEqual(output_value(output, "enabled"), "true")

    def test_promoted_task_maps_backend_seed_workspace_and_structured_flags_correctly(self) -> None:
        with temporary_test_dir() as tmp:
            draft_id, _draft_dir = scaffold_draft(tmp, request_text="# Docs\n\nDocument docs flow.\n", task_id="docs-flow")
            stdout = StringIO()
            with redirect_stdout(stdout):
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
                            "--task-id",
                            "docs-flow",
                            "--commit-message",
                            "docs: promote draft",
                        ]
                    ),
                    0,
                )
            validate_draft(tmp, draft_id)
            exit_code, _output = promote_draft(tmp, draft_id)
            task = next(task for task in load_task_queue_config(tmp / "tasks.yaml").tasks if task.id == "docs-flow")

        self.assertEqual(exit_code, 0)
        self.assertEqual(task.backend, "codex_cli")
        self.assertEqual(task.seed_workspace, ".")
        self.assertTrue(task.require_structured_report)
        self.assertTrue(task.rerun_report_test_commands)
        self.assertTrue(task.validate_workspace_manifest)
        self.assertEqual(task.validation_command_timeout, 60)
        self.assertTrue(task.stream_codex_output)
        self.assertTrue(task.verbose)
        self.assertEqual(task.commit_message, "docs: promote draft")

    def test_promoted_task_prompt_includes_required_sections(self) -> None:
        with temporary_test_dir() as tmp:
            draft_id, _draft_dir = scaffold_draft(tmp, request_text="# Docs\n\nDocument docs flow.\n")
            revise_to_valid(tmp, draft_id)
            validate_draft(tmp, draft_id)
            self.assertEqual(promote_draft(tmp, draft_id)[0], 0)
            task = load_task_queue_config(tmp / "tasks.yaml").tasks[0]

        self.assertIn("Non-goals:", task.prompt)
        self.assertIn("Files allowed:", task.prompt)
        self.assertIn("Files forbidden:", task.prompt)
        self.assertIn("Invariants:", task.prompt)
        self.assertIn("Tests required:", task.prompt)
        self.assertIn("Commands to run:", task.prompt)
        self.assertIn("Rollback notes:", task.prompt)
        self.assertIn("Create EXECUTION_REPORT.json", task.prompt)
        self.assertIn("Do not create EXECUTION_REPORT.md.", task.prompt)
        self.assertIn("Do not commit changes.", task.prompt)
        self.assertIn("Do not modify files_forbidden.", task.prompt)
        self.assertIn("Keep changed_files within files_allowed.", task.prompt)

    def test_promoted_criteria_match_draft_acceptance_criteria(self) -> None:
        with temporary_test_dir() as tmp:
            draft_id, draft_dir = scaffold_draft(tmp, request_text="# Docs\n\nDocument docs flow.\n")
            revise_to_valid(tmp, draft_id)
            validate_draft(tmp, draft_id)
            self.assertEqual(promote_draft(tmp, draft_id)[0], 0)
            draft = load_task_draft(draft_dir / "task_draft.yaml")
            task = load_task_queue_config(tmp / "tasks.yaml").tasks[0]

        self.assertEqual(task.criteria, draft.acceptance_criteria)

    def test_promotion_fails_if_validator_report_missing(self) -> None:
        with temporary_test_dir() as tmp:
            draft_id, draft_dir = scaffold_draft(tmp, request_text="# Docs\n\nDocument docs flow.\n")
            revise_to_valid(tmp, draft_id)
            report_output = validate_draft(tmp, draft_id)
            Path(output_value(report_output, "validator_report")).unlink()
            exit_code, output = promote_draft(tmp, draft_id)

        self.assertEqual(exit_code, 1)
        self.assertIn("required draft artifact missing: task_draft_validator_report.json", output)

    def test_promotion_fails_if_validation_status_needs_revision(self) -> None:
        with temporary_test_dir() as tmp:
            draft_id, _draft_dir = scaffold_draft(tmp, request_text="# Docs\n\nDocument docs flow.\n")
            validate_draft(tmp, draft_id)
            exit_code, output = promote_draft(tmp, draft_id)

        self.assertEqual(exit_code, 1)
        self.assertIn("validation_status is not valid: needs_revision", output)

    def test_promotion_fails_if_valid_for_promotion_false(self) -> None:
        with temporary_test_dir() as tmp:
            draft_id, draft_dir = scaffold_draft(tmp, request_text="# Docs\n\nDocument docs flow.\n")
            revise_to_valid(tmp, draft_id)
            report_output = validate_draft(tmp, draft_id)
            report_path = Path(output_value(report_output, "validator_report"))
            payload = json.loads(report_path.read_text(encoding="utf-8"))
            payload["valid_for_promotion"] = False
            report_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
            exit_code, output = promote_draft(tmp, draft_id)

        self.assertEqual(exit_code, 1)
        self.assertIn("validator report does not allow promotion", output)

    def test_promotion_fails_if_manifest_validation_status_stale(self) -> None:
        with temporary_test_dir() as tmp:
            draft_id, draft_dir = scaffold_draft(tmp, request_text="# Docs\n\nDocument docs flow.\n")
            revise_to_valid(tmp, draft_id)
            validate_draft(tmp, draft_id)
            stdout = StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(
                    revise_task_draft_main(
                        [draft_id, "--drafts-dir", str(tmp / ".task_drafts"), "--add-assumption", "Needs revalidation."]
                    ),
                    0,
                )
            exit_code, output = promote_draft(tmp, draft_id)
            manifest = load_task_draft_manifest(draft_dir / "MANIFEST.json")

        self.assertEqual(exit_code, 1)
        self.assertEqual(manifest.validation_status, "stale")
        self.assertIn("manifest validation_status is not valid: stale", output)

    def test_promotion_fails_if_task_id_exists_without_replace(self) -> None:
        with temporary_test_dir() as tmp:
            draft_id, draft_dir = scaffold_draft(tmp, request_text="# Docs\n\nDocument docs flow.\n", task_id="docs-flow")
            revise_to_valid(tmp, draft_id)
            validate_draft(tmp, draft_id)
            write_text(
                tmp / "tasks.yaml",
                """
                project: existing
                defaults:
                  backend: mock
                tasks:
                  - id: "docs-flow"
                    title: "Existing"
                    prompt: "Existing prompt"
                    enabled: false
                    criteria:
                      - "tests.status=passed"
                """,
            )
            exit_code, output = promote_draft(tmp, draft_id)
            current_text = (tmp / "tasks.yaml").read_text(encoding="utf-8")

        self.assertEqual(exit_code, 1)
        self.assertIn("task id already exists in tasks.yaml: docs-flow; pass --replace to overwrite it", output)
        self.assertIn('id: "docs-flow"', current_text)

    def test_replace_replaces_existing_task_and_preserves_other_tasks(self) -> None:
        with temporary_test_dir() as tmp:
            draft_id, draft_dir = scaffold_draft(tmp, request_text="# Docs\n\nDocument docs flow.\n", task_id="docs-flow")
            revise_to_valid(tmp, draft_id)
            validate_draft(tmp, draft_id)
            write_text(
                tmp / "tasks.yaml",
                """
                project: existing
                defaults:
                  backend: mock
                tasks:
                  - id: "keep-me"
                    title: "Keep me"
                    prompt: "Keep prompt"
                    enabled: false
                    criteria:
                      - "tests.status=passed"
                  - id: "docs-flow"
                    title: "Existing"
                    prompt: "Old prompt"
                    enabled: false
                    criteria:
                      - "tests.status=passed"
                """,
            )
            exit_code, output = promote_draft(tmp, draft_id, "--replace")
            config = load_task_queue_config(tmp / "tasks.yaml")
            ids = [task.id for task in config.tasks]
            replaced = next(task for task in config.tasks if task.id == "docs-flow")
            draft = load_task_draft(draft_dir / "task_draft.yaml")

        self.assertEqual(exit_code, 0, output)
        self.assertEqual(output_value(output, "mode"), "replace")
        self.assertEqual(ids, ["keep-me", "docs-flow"])
        self.assertIn(draft.objective, replaced.prompt)

    def test_existing_tasks_yaml_project_and_defaults_preserved(self) -> None:
        with temporary_test_dir() as tmp:
            draft_id, _draft_dir = scaffold_draft(tmp, request_text="# Docs\n\nDocument docs flow.\n", task_id="docs-flow")
            revise_to_valid(tmp, draft_id)
            validate_draft(tmp, draft_id)
            write_text(
                tmp / "tasks.yaml",
                """
                project: custom_project
                defaults:
                  backend: codex_cli
                  max_retries: 5
                  require_structured_report: true
                  rerun_report_test_commands: true
                  validate_workspace_manifest: true
                  validation_command_timeout: 120
                  stream_codex_output: true
                  verbose: false
                tasks:
                  - id: "existing"
                    title: "Existing"
                    prompt: "Existing prompt"
                    enabled: false
                    criteria:
                      - "tests.status=passed"
                """,
            )
            self.assertEqual(promote_draft(tmp, draft_id)[0], 0)
            raw_text = (tmp / "tasks.yaml").read_text(encoding="utf-8")
            config = load_task_queue_config(tmp / "tasks.yaml")

        self.assertIn("project: custom_project", raw_text)
        self.assertEqual(config.project, "custom_project")
        self.assertEqual(config.defaults.backend, "codex_cli")
        self.assertEqual(config.defaults.max_retries, 5)

    def test_written_tasks_yaml_reloads_via_load_task_queue_config(self) -> None:
        with temporary_test_dir() as tmp:
            draft_id, _draft_dir = scaffold_draft(tmp, request_text="# Docs\n\nDocument docs flow.\n")
            revise_to_valid(tmp, draft_id)
            validate_draft(tmp, draft_id)
            self.assertEqual(promote_draft(tmp, draft_id)[0], 0)
            config = load_task_queue_config(tmp / "tasks.yaml")

        self.assertEqual(len(config.tasks), 1)

    def test_reload_failure_restores_previous_tasks_yaml_content(self) -> None:
        with temporary_test_dir() as tmp:
            draft_id, _draft_dir = scaffold_draft(tmp, request_text="# Docs\n\nDocument docs flow.\n", task_id="docs-flow")
            revise_to_valid(tmp, draft_id)
            validate_draft(tmp, draft_id)
            write_text(
                tmp / "tasks.yaml",
                """
                project: existing
                defaults:
                  backend: mock
                tasks:
                  - id: "existing"
                    title: "Existing"
                    prompt: "Existing prompt"
                    enabled: false
                    criteria:
                      - "tests.status=passed"
                """,
            )
            original_text = (tmp / "tasks.yaml").read_text(encoding="utf-8")
            original_loader = promotion_module.load_task_queue_config
            call_count = {"count": 0}

            def flaky_loader(path):
                call_count["count"] += 1
                if call_count["count"] == 1:
                    return original_loader(path)
                raise ValueError("simulated reload failure")

            with patch("ai_orchestrator.task_draft_promotion.load_task_queue_config", side_effect=flaky_loader):
                exit_code, output = promote_draft(tmp, draft_id)
            current_text = (tmp / "tasks.yaml").read_text(encoding="utf-8")

        self.assertEqual(exit_code, 1)
        self.assertIn("simulated reload failure", output)
        self.assertEqual(current_text, original_text)

    def test_promotion_does_not_create_runs(self) -> None:
        with temporary_test_dir() as tmp:
            draft_id, _draft_dir = scaffold_draft(tmp, request_text="# Docs\n\nDocument docs flow.\n")
            revise_to_valid(tmp, draft_id)
            validate_draft(tmp, draft_id)
            exit_code, _output = promote_draft(tmp, draft_id)
            runs_exists = (tmp / ".runs").exists()

        self.assertEqual(exit_code, 0)
        self.assertFalse(runs_exists)

    def test_promote_task_draft_json_output_is_valid(self) -> None:
        with temporary_test_dir() as tmp:
            draft_id, _draft_dir = scaffold_draft(tmp, request_text="# Docs\n\nDocument docs flow.\n")
            revise_to_valid(tmp, draft_id)
            validate_draft(tmp, draft_id)
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = promote_task_draft_main(
                    [draft_id, "--drafts-dir", str(tmp / ".task_drafts"), "--tasks-file", str(tmp / "tasks.yaml"), "--format", "json"]
                )
            payload = json.loads(stdout.getvalue())

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "promoted")
        self.assertFalse(payload["enabled"])

    def test_manifest_is_updated_with_promotion_metadata(self) -> None:
        with temporary_test_dir() as tmp:
            draft_id, draft_dir = scaffold_draft(tmp, request_text="# Docs\n\nDocument docs flow.\n", task_id="docs-flow")
            revise_to_valid(tmp, draft_id)
            validate_draft(tmp, draft_id)
            self.assertEqual(promote_draft(tmp, draft_id)[0], 0)
            manifest = load_task_draft_manifest(draft_dir / "MANIFEST.json")

        self.assertIsNotNone(manifest.promoted_at)
        self.assertEqual(manifest.promoted_task_id, "docs-flow")
        self.assertFalse(manifest.promoted_enabled)
        self.assertEqual(manifest.promotion_status, "promoted")
        self.assertTrue((manifest.promoted_tasks_file or "").endswith("tasks.yaml"))
