from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import json
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_orchestrator.backends.codex_cli import CodexCliBackend
from ai_orchestrator.schemas import PlanStep, TaskSpec

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


class CodexCliBackendTests(unittest.TestCase):
    def test_normalize_plain_command(self):
        backend = CodexCliBackend(codex_cmd="codex")
        self.assertEqual(backend.codex_cmd, ["codex"])

    def test_normalize_compound_command(self):
        backend = CodexCliBackend(codex_cmd="npx --yes @openai/codex")
        self.assertEqual(backend.codex_cmd, ["npx", "--yes", "@openai/codex"])

    def test_normalize_local_path(self):
        backend = CodexCliBackend(codex_cmd="./node_modules/.bin/codex")
        self.assertEqual(backend.codex_cmd, ["./node_modules/.bin/codex"])

    def test_normalize_absolute_windows_path(self):
        backend = CodexCliBackend(
            codex_cmd=r"C:\Users\Slivin.Aleksandr\Documents\ai_orchestrator_mvp\node_modules\.bin\codex.cmd"
        )
        self.assertEqual(
            backend.codex_cmd,
            [r"C:\Users\Slivin.Aleksandr\Documents\ai_orchestrator_mvp\node_modules\.bin\codex.cmd"],
        )

    def test_collect_workspace_text_files_reads_allowed_extensions_only(self):
        with temporary_test_dir() as workspace_dir:
            (workspace_dir / "RESULT.md").write_text("# Title\nORCHESTRATOR_SMOKE_TEST_OK\n", encoding="utf-8")
            (workspace_dir / "nested").mkdir()
            (workspace_dir / "nested" / "config.json").write_text('{"ok": true}\n', encoding="utf-8")
            (workspace_dir / "image.bin").write_bytes(b"\x00\x01\x02")

            collected = CodexCliBackend._collect_workspace_text_files(workspace_dir)

            self.assertCountEqual(
                [(path.relative_to(workspace_dir).as_posix(), content) for path, content in collected],
                [
                    ("RESULT.md", "# Title\nORCHESTRATOR_SMOKE_TEST_OK\n"),
                    ("nested/config.json", '{"ok": true}\n'),
                ],
            )

    def test_execute_step_includes_workspace_files_in_content_and_artifacts(self):
        backend = CodexCliBackend(codex_cmd="codex")
        task = TaskSpec(description="Create RESULT.md", require_structured_report=True)
        step = PlanStep(id="step_1", title="Create file", description="Write RESULT.md")

        captured_run_kwargs = {}
        captured_cmd = []

        def fake_run(cmd, **kwargs):
            captured_cmd.extend(cmd)
            captured_run_kwargs.update(kwargs)
            workspace_dir = Path(cmd[cmd.index("--cd") + 1])
            output_path = Path(cmd[cmd.index("--output-last-message") + 1])
            (workspace_dir / "RESULT.md").write_text(
                "# Orchestrator Codex smoke test\nORCHESTRATOR_SMOKE_TEST_OK\n",
                encoding="utf-8",
            )
            output_path.write_text("Created RESULT.md", encoding="utf-8")

            return subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout="executor stdout",
                stderr="",
            )

        with temporary_test_dir() as artifacts_dir, patch.object(
            CodexCliBackend,
            "_command_exists",
            return_value=True,
        ), patch(
            "ai_orchestrator.backends.codex_cli.subprocess.run",
            side_effect=fake_run,
        ):
            result = backend.execute_step(
                task=task,
                step=step,
                attempt=1,
                previous_feedback=[],
                artifacts_dir=artifacts_dir,
            )

        self.assertEqual(result.status, "completed")
        self.assertIn("Create RESULT.md", captured_run_kwargs["input"])
        self.assertIn("Produce the requested artifact", captured_run_kwargs["input"])
        self.assertIn("EXECUTION_REPORT.json", captured_run_kwargs["input"])
        self.assertTrue(captured_cmd)
        self.assertNotIn("Create RESULT.md", captured_cmd)
        self.assertIn("## workspace files", result.content)
        self.assertIn("### RESULT.md", result.content)
        self.assertIn("ORCHESTRATOR_SMOKE_TEST_OK", result.content)
        self.assertIn(str(artifacts_dir / "workspace" / "RESULT.md"), result.artifact_paths)

    def test_execute_step_copies_seed_workspace_and_writes_baseline_manifest(self):
        backend = CodexCliBackend(codex_cmd="codex")
        step = PlanStep(id="step_1", title="Modify seed", description="Modify seeded project")

        def fake_run(cmd, **kwargs):
            workspace_dir = Path(cmd[cmd.index("--cd") + 1])
            self.assertTrue((workspace_dir / "src" / "existing.py").exists())
            self.assertFalse((workspace_dir / "node_modules" / "ignored.js").exists())
            (workspace_dir / "src" / "existing.py").write_text("VALUE = 2\n", encoding="utf-8")
            (workspace_dir / "EXECUTION_REPORT.json").write_text(
                json.dumps({
                    "schema_version": "1.0",
                    "status": "completed",
                    "summary": "Modified seeded file.",
                    "changed_files": ["src/existing.py", "EXECUTION_REPORT.json"],
                    "commands_run": [
                        {"command": "python -m unittest discover -s tests", "exit_code": 0, "status": "passed", "summary": "ok"}
                    ],
                    "tests": [
                        {"name": "unittest", "command": "python -m unittest discover -s tests", "status": "passed", "total": 0, "passed": 0, "failed": 0, "output": "OK"}
                    ],
                    "risks": [],
                    "assumptions": [],
                    "validation_notes": [],
                }, indent=2),
                encoding="utf-8",
            )
            Path(cmd[cmd.index("--output-last-message") + 1]).write_text("done", encoding="utf-8")
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="ok", stderr="")

        with temporary_test_dir() as tmp, patch.object(
            CodexCliBackend,
            "_command_exists",
            return_value=True,
        ), patch(
            "ai_orchestrator.backends.codex_cli.subprocess.run",
            side_effect=fake_run,
        ):
            seed = tmp / "seed"
            (seed / "src").mkdir(parents=True)
            (seed / "src" / "existing.py").write_text("VALUE = 1\n", encoding="utf-8")
            (seed / "node_modules").mkdir()
            (seed / "node_modules" / "ignored.js").write_text("ignored", encoding="utf-8")
            artifacts_dir = tmp / "artifacts"
            task = TaskSpec(
                description="Modify seeded project",
                require_structured_report=True,
                validate_workspace_manifest=True,
                seed_workspace_path=str(seed),
            )

            result = backend.execute_step(
                task=task,
                step=step,
                attempt=1,
                previous_feedback=[],
                artifacts_dir=artifacts_dir,
            )

            baseline_path = artifacts_dir / "workspace_baseline_manifest.json"
            self.assertEqual(result.status, "completed")
            self.assertTrue(baseline_path.exists())
            baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
            self.assertIn("src/existing.py", baseline["files"])
            self.assertNotIn("node_modules/ignored.js", baseline["files"])
            self.assertIn(str(baseline_path), result.artifact_paths)
            self.assertIn("seed_workspace:", result.content)

    def test_seed_workspace_log_captures_only_report_and_changed_files(self):
        backend = CodexCliBackend(codex_cmd="codex")
        step = PlanStep(id="step_1", title="Modify seed", description="Modify seeded project")

        def fake_run(cmd, **kwargs):
            workspace_dir = Path(cmd[cmd.index("--cd") + 1])
            (workspace_dir / "docs" / "old.md").write_text("unchanged docs\n", encoding="utf-8")
            (workspace_dir / "src" / "existing.py").write_text("VALUE = 2\n", encoding="utf-8")
            (workspace_dir / "EXECUTION_REPORT.json").write_text(
                json.dumps({
                    "schema_version": "1.0",
                    "status": "completed",
                    "summary": "Modified seeded file.",
                    "changed_files": ["src/existing.py", "EXECUTION_REPORT.json"],
                    "commands_run": [
                        {"command": "python -m unittest discover -s tests", "exit_code": 0, "status": "passed", "summary": "ok"}
                    ],
                    "tests": [
                        {"name": "unittest", "command": "python -m unittest discover -s tests", "status": "passed", "total": 0, "passed": 0, "failed": 0, "output": "OK"}
                    ],
                    "risks": [],
                    "assumptions": [],
                    "validation_notes": [],
                }, indent=2),
                encoding="utf-8",
            )
            Path(cmd[cmd.index("--output-last-message") + 1]).write_text("done", encoding="utf-8")
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="ok", stderr="")

        with temporary_test_dir() as tmp, patch.object(
            CodexCliBackend,
            "_command_exists",
            return_value=True,
        ), patch(
            "ai_orchestrator.backends.codex_cli.subprocess.run",
            side_effect=fake_run,
        ):
            seed = tmp / "seed"
            (seed / "src").mkdir(parents=True)
            (seed / "docs").mkdir()
            (seed / "src" / "existing.py").write_text("VALUE = 1\n", encoding="utf-8")
            (seed / "docs" / "old.md").write_text("unchanged docs\n", encoding="utf-8")
            task = TaskSpec(
                description="Modify seeded project",
                require_structured_report=True,
                validate_workspace_manifest=True,
                seed_workspace_path=str(seed),
            )

            result = backend.execute_step(
                task=task,
                step=step,
                attempt=1,
                previous_feedback=[],
                artifacts_dir=tmp / "artifacts",
            )

        self.assertIn("### EXECUTION_REPORT.json", result.content)
        self.assertIn("### src/existing.py", result.content)
        self.assertNotIn("### docs/old.md", result.content)


if __name__ == "__main__":
    unittest.main()
