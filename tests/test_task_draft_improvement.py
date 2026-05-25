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
    prepare_task_draft_improvement_main,
    validate_task_draft_main,
)
from ai_orchestrator.task_drafts import load_task_draft_manifest

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


def scaffold_draft(tmp: Path, *, request_text: str = "# Improve task draft\n\nCreate a safer task draft.\n") -> tuple[str, Path]:
    request_path = tmp / "raw_request.md"
    write_text(request_path, request_text)
    drafts_dir = tmp / ".task_drafts"
    stdout = StringIO()
    with redirect_stdout(stdout):
        exit_code = draft_task_scaffold_main(["--request", str(request_path), "--output-dir", str(drafts_dir)])
    output = stdout.getvalue()
    if exit_code != 0:
        raise AssertionError(output)
    return output_value(output, "draft_id"), Path(output_value(output, "draft_dir"))


def validate_draft(tmp: Path, draft_id: str) -> None:
    stdout = StringIO()
    with redirect_stdout(stdout):
        exit_code = validate_task_draft_main([draft_id, "--drafts-dir", str(tmp / ".task_drafts")])
    if exit_code != 0:
        raise AssertionError(stdout.getvalue())


def prepare_improvement(tmp: Path, draft_id: str, *extra_args: str) -> tuple[int, str]:
    stdout = StringIO()
    with redirect_stdout(stdout):
        exit_code = prepare_task_draft_improvement_main(
            [draft_id, "--drafts-dir", str(tmp / ".task_drafts"), *extra_args]
        )
    return exit_code, stdout.getvalue()


class TaskDraftImprovementCliTests(unittest.TestCase):
    def test_prepare_task_draft_improvement_creates_prompt_with_all_inputs(self) -> None:
        with temporary_test_dir() as tmp:
            raw_request = "# Improve operator task\n\nMake the operator task draft safer and narrower.\n"
            draft_id, draft_dir = scaffold_draft(tmp, request_text=raw_request)
            validate_draft(tmp, draft_id)
            exit_code, output = prepare_improvement(tmp, draft_id)
            prompt_path = Path(output_value(output, "prompt"))
            prompt_exists = prompt_path.exists()
            prompt_text = prompt_path.read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0, output)
        self.assertTrue(prompt_exists)
        self.assertIn("# Prompt для улучшения task draft", prompt_text)
        self.assertIn("## Роль", prompt_text)
        self.assertIn("## Запреты", prompt_text)
        self.assertIn("Make the operator task draft safer and narrower.", prompt_text)
        self.assertIn("### Current task_draft.yaml", prompt_text)
        self.assertIn("schema_version:", prompt_text)
        self.assertIn("draft_id:", prompt_text)
        self.assertIn("### Current codex_prompt.md", prompt_text)
        self.assertIn("### Current task_review.md", prompt_text)
        self.assertIn("### Validator report", prompt_text)
        self.assertIn("validation_status", prompt_text)
        self.assertIn("Findings:", prompt_text)
        self.assertIn("warnings блокируют promotion", prompt_text)
        self.assertIn("`task_draft.yaml` целиком в YAML block", prompt_text)
        self.assertIn("Не запускать Codex executor.", prompt_text)
        self.assertEqual(output_value(output, "status"), "task_draft_improvement_prompt_prepared")
        self.assertEqual(output_value(output, "next_action"), "run_external_task_authoring_agent_or_revise_task_draft")

    def test_prepare_task_draft_improvement_updates_manifest(self) -> None:
        with temporary_test_dir() as tmp:
            draft_id, draft_dir = scaffold_draft(tmp)
            exit_code, output = prepare_improvement(tmp, draft_id)
            manifest = load_task_draft_manifest(draft_dir / "MANIFEST.json")

        self.assertEqual(exit_code, 0, output)
        self.assertEqual(manifest.improvement_prompt_status, "prepared")
        self.assertEqual(Path(manifest.task_draft_improvement_prompt), Path(output_value(output, "prompt")))
        self.assertIsNotNone(manifest.task_draft_improvement_prompt_created_at)

    def test_prepare_task_draft_improvement_refuses_existing_output_without_force(self) -> None:
        with temporary_test_dir() as tmp:
            draft_id, _draft_dir = scaffold_draft(tmp)
            first_exit, first_output = prepare_improvement(tmp, draft_id)
            second_exit, second_output = prepare_improvement(tmp, draft_id)

        self.assertEqual(first_exit, 0, first_output)
        self.assertNotEqual(second_exit, 0)
        self.assertIn("status=failed", second_output)
        self.assertIn("already exists", second_output)

    def test_prepare_task_draft_improvement_force_overwrites_existing_output(self) -> None:
        with temporary_test_dir() as tmp:
            draft_id, _draft_dir = scaffold_draft(tmp)
            exit_code, output = prepare_improvement(tmp, draft_id)
            prompt_path = Path(output_value(output, "prompt"))
            prompt_path.write_text("old marker", encoding="utf-8")
            force_exit, force_output = prepare_improvement(tmp, draft_id, "--force")
            prompt_text = prompt_path.read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0, output)
        self.assertEqual(force_exit, 0, force_output)
        self.assertNotIn("old marker", prompt_text)
        self.assertIn("# Prompt для улучшения task draft", prompt_text)

    def test_prepare_task_draft_improvement_works_without_validator_report(self) -> None:
        with temporary_test_dir() as tmp:
            draft_id, _draft_dir = scaffold_draft(tmp)
            exit_code, output = prepare_improvement(tmp, draft_id)
            prompt_text = Path(output_value(output, "prompt")).read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0, output)
        self.assertIn("Validator report отсутствует.", prompt_text)

    def test_prepare_task_draft_improvement_does_not_modify_existing_draft_artifacts(self) -> None:
        with temporary_test_dir() as tmp:
            draft_id, draft_dir = scaffold_draft(tmp)
            task_draft_path = draft_dir / "task_draft.yaml"
            codex_prompt_path = draft_dir / "codex_prompt.md"
            task_review_path = draft_dir / "task_review.md"
            task_draft_before = task_draft_path.read_text(encoding="utf-8")
            codex_prompt_before = codex_prompt_path.read_text(encoding="utf-8")
            task_review_before = task_review_path.read_text(encoding="utf-8")
            exit_code, output = prepare_improvement(tmp, draft_id)
            task_draft_after = task_draft_path.read_text(encoding="utf-8")
            codex_prompt_after = codex_prompt_path.read_text(encoding="utf-8")
            task_review_after = task_review_path.read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0, output)
        self.assertEqual(task_draft_before, task_draft_after)
        self.assertEqual(codex_prompt_before, codex_prompt_after)
        self.assertEqual(task_review_before, task_review_after)
        self.assertFalse((tmp / "tasks.yaml").exists())
        self.assertFalse((tmp / ".runs").exists())

    def test_prepare_task_draft_improvement_json_output_is_valid(self) -> None:
        with temporary_test_dir() as tmp:
            draft_id, _draft_dir = scaffold_draft(tmp)
            exit_code, output = prepare_improvement(tmp, draft_id, "--format", "json")
            payload = json.loads(output)
            prompt_exists = Path(payload["prompt"]).exists()

        self.assertEqual(exit_code, 0, output)
        self.assertEqual(payload["draft_id"], draft_id)
        self.assertEqual(payload["status"], "task_draft_improvement_prompt_prepared")
        self.assertTrue(prompt_exists)
        self.assertEqual(payload["next_action"], "run_external_task_authoring_agent_or_revise_task_draft")


if __name__ == "__main__":
    unittest.main()
