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
    import_task_draft_improvement_main,
    prepare_task_draft_improvement_main,
    validate_task_draft_main,
)
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


def save_yaml(path: Path, payload: object) -> None:
    import yaml

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(payload, handle, allow_unicode=True, sort_keys=False, default_flow_style=False)


def scaffold_draft(tmp: Path, *, request_text: str = "# Import improved draft\n\nCreate a safe docs task.\n") -> tuple[str, Path]:
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


def prepare_prompt(tmp: Path, draft_id: str) -> Path:
    stdout = StringIO()
    with redirect_stdout(stdout):
        exit_code = prepare_task_draft_improvement_main([draft_id, "--drafts-dir", str(tmp / ".task_drafts")])
    output = stdout.getvalue()
    if exit_code != 0:
        raise AssertionError(output)
    return Path(output_value(output, "prompt"))


def improved_payload(draft_dir: Path, **updates) -> dict[str, object]:
    draft = load_task_draft(draft_dir / "task_draft.yaml")
    payload = draft.model_dump(mode="python")
    payload.update(
        {
            "title": "Improved imported task draft",
            "objective": "Improved objective from external task authoring agent.",
            "risk_level": "low",
            "files_allowed": ["docs/example.md"],
            "open_questions": [],
            "required_review_profiles": [],
            "optional_review_profiles": ["qa"],
        }
    )
    payload["target_task"]["title"] = "Improved imported task draft"
    payload["target_task"]["enabled"] = False
    payload.update(updates)
    return payload


def write_improved_draft(tmp: Path, draft_dir: Path, **updates) -> Path:
    path = tmp / f"improved_{uuid4().hex}.yaml"
    save_yaml(path, improved_payload(draft_dir, **updates))
    return path


def import_improvement(tmp: Path, draft_id: str, improved_path: Path, *extra_args: str) -> tuple[int, str]:
    stdout = StringIO()
    with redirect_stdout(stdout):
        exit_code = import_task_draft_improvement_main(
            [
                draft_id,
                "--drafts-dir",
                str(tmp / ".task_drafts"),
                "--improved-draft",
                str(improved_path),
                *extra_args,
            ]
        )
    return exit_code, stdout.getvalue()


class TaskDraftImprovementImportCliTests(unittest.TestCase):
    def test_import_task_draft_improvement_replaces_draft_and_regenerates_artifacts(self) -> None:
        with temporary_test_dir() as tmp:
            draft_id, draft_dir = scaffold_draft(tmp)
            improved_path = write_improved_draft(tmp, draft_dir)
            notes_path = tmp / "notes.md"
            write_text(notes_path, "# Notes\n\nImproved scope and risk.")
            exit_code, output = import_improvement(tmp, draft_id, improved_path, "--notes", str(notes_path))
            imported = load_task_draft(draft_dir / "task_draft.yaml")
            backup_path = Path(output_value(output, "backup"))
            backup_exists = backup_path.exists()
            codex_prompt = (draft_dir / "codex_prompt.md").read_text(encoding="utf-8")
            task_review = (draft_dir / "task_review.md").read_text(encoding="utf-8")
            manifest = load_task_draft_manifest(draft_dir / "MANIFEST.json")
            notes_text = (draft_dir / "TASK_DRAFT_IMPROVEMENT_NOTES.md").read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0, output)
        self.assertEqual(output_value(output, "status"), "task_draft_improvement_imported")
        self.assertEqual(imported.objective, "Improved objective from external task authoring agent.")
        self.assertEqual(imported.files_allowed, ["docs/example.md"])
        self.assertTrue(backup_exists)
        self.assertIn("Improved objective from external task authoring agent.", codex_prompt)
        self.assertIn("Improved imported task draft", task_review)
        self.assertEqual(manifest.validation_status, "stale")
        self.assertFalse(manifest.valid_for_promotion)
        self.assertEqual(manifest.validation_stale_reason, "task draft imported from external improvement")
        self.assertEqual(manifest.revision_count, 1)
        self.assertEqual(Path(manifest.imported_improvement_source), improved_path.resolve())
        self.assertEqual(Path(manifest.improvement_notes), (draft_dir / "TASK_DRAFT_IMPROVEMENT_NOTES.md").resolve())
        self.assertIn("Improved scope and risk.", notes_text)

    def test_import_task_draft_improvement_is_atomic_for_invalid_inputs(self) -> None:
        invalid_cases = [
            {"draft_id": "wrong_draft_id"},
            {"target_task": {"id": "bad", "title": "Bad", "enabled": True}},
            {"non_goals": []},
            {"files_forbidden": []},
            {"commands_to_run": ["git reset --hard"]},
            {"files_allowed": ["../escape.py"]},
        ]
        for updates in invalid_cases:
            with self.subTest(updates=updates), temporary_test_dir() as tmp:
                draft_id, draft_dir = scaffold_draft(tmp)
                original_draft = (draft_dir / "task_draft.yaml").read_text(encoding="utf-8")
                original_prompt = (draft_dir / "codex_prompt.md").read_text(encoding="utf-8")
                original_review = (draft_dir / "task_review.md").read_text(encoding="utf-8")
                original_manifest = (draft_dir / "MANIFEST.json").read_text(encoding="utf-8")
                improved_path = write_improved_draft(tmp, draft_dir, **updates)
                exit_code, output = import_improvement(tmp, draft_id, improved_path)

                self.assertNotEqual(exit_code, 0, output)
                self.assertIn("status=failed", output)
                self.assertEqual((draft_dir / "task_draft.yaml").read_text(encoding="utf-8"), original_draft)
                self.assertEqual((draft_dir / "codex_prompt.md").read_text(encoding="utf-8"), original_prompt)
                self.assertEqual((draft_dir / "task_review.md").read_text(encoding="utf-8"), original_review)
                self.assertEqual((draft_dir / "MANIFEST.json").read_text(encoding="utf-8"), original_manifest)
                self.assertFalse(any(draft_dir.glob("task_draft.before_improvement*.yaml")))

    def test_import_task_draft_improvement_empty_notes_fail(self) -> None:
        with temporary_test_dir() as tmp:
            draft_id, draft_dir = scaffold_draft(tmp)
            improved_path = write_improved_draft(tmp, draft_dir)
            notes_path = tmp / "empty_notes.md"
            notes_path.write_text("   ", encoding="utf-8")
            exit_code, output = import_improvement(tmp, draft_id, improved_path, "--notes", str(notes_path))

        self.assertNotEqual(exit_code, 0)
        self.assertIn("notes file is empty", output)

    def test_import_task_draft_improvement_notes_overwrite_requires_force(self) -> None:
        with temporary_test_dir() as tmp:
            draft_id, draft_dir = scaffold_draft(tmp)
            notes_path = tmp / "notes.md"
            write_text(notes_path, "# Notes\n\nFirst notes.")
            first_improved = write_improved_draft(tmp, draft_dir)
            first_exit, first_output = import_improvement(tmp, draft_id, first_improved, "--notes", str(notes_path))
            second_improved = write_improved_draft(tmp, draft_dir, objective="Second imported objective.")
            second_exit, second_output = import_improvement(tmp, draft_id, second_improved, "--notes", str(notes_path))
            force_exit, force_output = import_improvement(
                tmp,
                draft_id,
                second_improved,
                "--notes",
                str(notes_path),
                "--force",
            )

        self.assertEqual(first_exit, 0, first_output)
        self.assertNotEqual(second_exit, 0)
        self.assertIn("notes already exist", second_output)
        self.assertEqual(force_exit, 0, force_output)

    def test_import_task_draft_improvement_preserves_raw_request_and_prompt_packet(self) -> None:
        with temporary_test_dir() as tmp:
            draft_id, draft_dir = scaffold_draft(tmp)
            prompt_path = prepare_prompt(tmp, draft_id)
            raw_before = (draft_dir / "raw_request.md").read_text(encoding="utf-8")
            prompt_before = prompt_path.read_text(encoding="utf-8")
            improved_path = write_improved_draft(tmp, draft_dir)
            exit_code, output = import_improvement(tmp, draft_id, improved_path)
            raw_after = (draft_dir / "raw_request.md").read_text(encoding="utf-8")
            prompt_after = prompt_path.read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0, output)
        self.assertEqual(raw_before, raw_after)
        self.assertEqual(prompt_before, prompt_after)
        self.assertFalse((tmp / "tasks.yaml").exists())
        self.assertFalse((tmp / ".runs").exists())

    def test_import_task_draft_improvement_json_output_is_valid(self) -> None:
        with temporary_test_dir() as tmp:
            draft_id, draft_dir = scaffold_draft(tmp)
            improved_path = write_improved_draft(tmp, draft_dir)
            exit_code, output = import_improvement(tmp, draft_id, improved_path, "--format", "json")
            payload = json.loads(output)
            backup_exists = Path(payload["backup"]).exists()

        self.assertEqual(exit_code, 0, output)
        self.assertEqual(payload["draft_id"], draft_id)
        self.assertEqual(payload["status"], "task_draft_improvement_imported")
        self.assertEqual(payload["validation_status"], "stale")
        self.assertEqual(payload["next_action"], "validate_task_draft")
        self.assertTrue(backup_exists)

    def test_validate_task_draft_can_run_after_import_and_sees_updated_draft(self) -> None:
        with temporary_test_dir() as tmp:
            draft_id, draft_dir = scaffold_draft(tmp)
            improved_path = write_improved_draft(tmp, draft_dir)
            import_exit, import_output = import_improvement(tmp, draft_id, improved_path)
            stdout = StringIO()
            with redirect_stdout(stdout):
                validate_exit = validate_task_draft_main([draft_id, "--drafts-dir", str(tmp / ".task_drafts")])
            validate_output = stdout.getvalue()
            imported = load_task_draft(draft_dir / "task_draft.yaml")

        self.assertEqual(import_exit, 0, import_output)
        self.assertEqual(validate_exit, 0, validate_output)
        self.assertEqual(imported.objective, "Improved objective from external task authoring agent.")
        self.assertIn("status=validated", validate_output)


if __name__ == "__main__":
    unittest.main()
