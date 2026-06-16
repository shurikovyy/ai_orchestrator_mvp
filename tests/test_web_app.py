from __future__ import annotations

from contextlib import redirect_stdout
import importlib.util
from io import StringIO
from pathlib import Path
import shutil
import subprocess
import sys
import textwrap
import unittest
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_orchestrator_web.project_status import (
    get_current_project_status,
    get_git_status_summary,
)
from ai_orchestrator.cli import draft_task_scaffold_main


WEB_DEPS_AVAILABLE = all(
    importlib.util.find_spec(name) is not None for name in ("fastapi", "httpx", "jinja2")
)

TEST_TEMP_ROOT = Path(__file__).resolve().parents[1] / ".tmp_tests"
TEST_TEMP_ROOT.mkdir(exist_ok=True)


class TemporaryProject:
    def __init__(self, base_dir: Path = TEST_TEMP_ROOT) -> None:
        self.base_dir = base_dir

    def __enter__(self) -> Path:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.base_dir / f"web_{uuid4().hex}"
        self.path.mkdir(parents=True, exist_ok=False)
        return self.path

    def __exit__(self, exc_type, exc, tb) -> None:
        shutil.rmtree(self.path, ignore_errors=True)


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")


def output_value(output: str, key: str) -> str:
    prefix = f"{key}="
    for line in output.splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :].strip()
    raise AssertionError(f"missing output line for {key!r} in:\n{output}")


def scaffold_web_draft(root: Path, *, task_id: str = "web-draft-task") -> tuple[str, Path]:
    request_path = root / "raw_request.md"
    write_text(
        request_path,
        """
        # Web draft fixture

        Create a tiny docs-only task draft for web inspection tests.
        """,
    )
    stdout = StringIO()
    with redirect_stdout(stdout):
        exit_code = draft_task_scaffold_main(
            [
                "--request",
                str(request_path),
                "--output-dir",
                str(root / ".task_drafts"),
                "--risk-level",
                "low",
                "--task-id",
                task_id,
            ]
        )
    output = stdout.getvalue()
    if exit_code != 0:
        raise AssertionError(output)
    return output_value(output, "draft_id"), Path(output_value(output, "draft_dir"))


def write_tasks_yaml(root: Path, *, long_prompt: bool = False) -> Path:
    prompt = "A" * 4500 if long_prompt else "Inspect a read-only task from the web dashboard."
    tasks_file = root / "tasks.yaml"
    write_text(
        tasks_file,
        f"""
        project: web-test

        defaults:
          backend: mock
          max_retries: 2
          require_structured_report: false
          rerun_report_test_commands: false
          validate_workspace_manifest: false
          validation_command_timeout: 60
          stream_codex_output: false
          verbose: true

        tasks:
          - id: "web-enabled-task"
            title: "Web Enabled Task"
            enabled: true
            backend: mock
            seed_workspace: "."
            require_structured_report: true
            rerun_report_test_commands: true
            validate_workspace_manifest: true
            validation_command_timeout: 45
            stream_codex_output: true
            prompt: |
              {prompt}
            criteria:
              - "report.status=completed"
              - "tests.status=passed"
            plan_steps:
              - id: "inspect"
                title: "Inspect"
                description: |
                  Inspect task state.
                criteria:
                  - "inspection summary"

          - id: "web-disabled-task"
            title: "Web Disabled Task"
            enabled: false
            backend: mock
            prompt: |
              This disabled task must stay visibly disabled.
            criteria:
              - "disabled marker"
        """,
    )
    return tasks_file


class ProjectStatusTests(unittest.TestCase):
    def test_project_status_reports_local_artifact_flags(self) -> None:
        with TemporaryProject() as root:
            (root / "tasks.yaml").write_text("tasks: []\n", encoding="utf-8")
            (root / ".task_drafts").mkdir()
            (root / ".runs").mkdir()

            status = get_current_project_status(root)

            self.assertEqual(status.project_root, root.resolve())
            self.assertTrue(status.tasks_yaml_exists)
            self.assertTrue(status.task_drafts_exists)
            self.assertTrue(status.runs_exists)

    def test_git_status_handles_non_git_directory(self) -> None:
        with TemporaryProject() as root:
            with patch("ai_orchestrator_web.project_status.subprocess.run") as run:
                run.return_value = subprocess.CompletedProcess(
                    ["git", "status", "--short"],
                    128,
                    "",
                    "fatal: not a git repository",
                )
                summary = get_git_status_summary(root)

        self.assertEqual(summary.status, "not_git_repo")


@unittest.skipUnless(WEB_DEPS_AVAILABLE, "web dependencies are not installed")
class WebAppRouteTests(unittest.TestCase):
    def test_health_returns_ok(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            client = TestClient(create_app(root))

            response = client.get("/health")

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {"status": "ok", "app": "ai_orchestrator_web"})

    def test_dashboard_contains_core_project_status(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            client = TestClient(create_app(root))

            response = client.get("/")

            self.assertEqual(response.status_code, 200)
            self.assertIn("AI Orchestrator Web MVP", response.text)
            self.assertIn("Project root", response.text)
            self.assertIn("ai_orchestrator version", response.text)
            self.assertIn("Git status", response.text)

    def test_dashboard_links_to_drafts(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            client = TestClient(create_app(root))

            response = client.get("/")

            self.assertEqual(response.status_code, 200)
            self.assertIn('href="/drafts"', response.text)

    def test_dashboard_links_to_tasks(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            client = TestClient(create_app(root))

            response = client.get("/")

            self.assertEqual(response.status_code, 200)
            self.assertIn('href="/tasks"', response.text)

    def test_drafts_missing_dir_shows_empty_state(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            client = TestClient(create_app(root))

            response = client.get("/drafts")

            self.assertEqual(response.status_code, 200)
            self.assertIn("No task drafts found", response.text)

    def test_drafts_lists_scaffolded_draft(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            draft_id, _draft_dir = scaffold_web_draft(root, task_id="web-list-draft")
            client = TestClient(create_app(root))

            response = client.get("/drafts")

            self.assertEqual(response.status_code, 200)
            self.assertIn(draft_id, response.text)
            self.assertIn("web-list-draft", response.text)
            self.assertIn("validation_status", response.text)
            self.assertIn("missing", response.text)
            self.assertIn("validate_task_draft", response.text)

    def test_draft_detail_shows_summary_and_artifact_previews(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            draft_id, _draft_dir = scaffold_web_draft(root, task_id="web-detail-draft")
            client = TestClient(create_app(root))

            response = client.get(f"/drafts/{draft_id}")

            self.assertEqual(response.status_code, 200)
            self.assertIn(draft_id, response.text)
            self.assertIn("web-detail-draft", response.text)
            self.assertIn("validation_status", response.text)
            self.assertIn("next_action", response.text)
            self.assertIn("validate_task_draft", response.text)
            self.assertIn("raw_request.md", response.text)
            self.assertIn("task_draft.yaml", response.text)

    def test_invalid_draft_id_returns_not_found(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            client = TestClient(create_app(root))

            response = client.get("/drafts/bad%5Cname")

            self.assertIn(response.status_code, {400, 404})

    def test_draft_pages_are_read_only_for_core_artifacts(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            draft_id, draft_dir = scaffold_web_draft(root, task_id="web-readonly-draft")
            task_draft_path = draft_dir / "task_draft.yaml"
            manifest_path = draft_dir / "MANIFEST.json"
            before_task_draft = task_draft_path.read_text(encoding="utf-8")
            before_manifest = manifest_path.read_text(encoding="utf-8")
            client = TestClient(create_app(root))

            list_response = client.get("/drafts")
            detail_response = client.get(f"/drafts/{draft_id}")

            self.assertEqual(list_response.status_code, 200)
            self.assertEqual(detail_response.status_code, 200)
            self.assertEqual(task_draft_path.read_text(encoding="utf-8"), before_task_draft)
            self.assertEqual(manifest_path.read_text(encoding="utf-8"), before_manifest)

    def test_tasks_missing_yaml_shows_friendly_message(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            client = TestClient(create_app(root))

            response = client.get("/tasks")

            self.assertEqual(response.status_code, 200)
            self.assertIn("tasks.yaml not found", response.text)
            self.assertIn("Copy tasks.yaml.example to tasks.yaml", response.text)

    def test_tasks_lists_tasks_from_tasks_yaml(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            write_tasks_yaml(root)
            client = TestClient(create_app(root))

            response = client.get("/tasks")

            self.assertEqual(response.status_code, 200)
            self.assertIn("web-enabled-task", response.text)
            self.assertIn("Web Enabled Task", response.text)
            self.assertIn("web-disabled-task", response.text)
            self.assertIn("criteria", response.text)
            self.assertIn("plan_steps", response.text)
            self.assertIn("true", response.text)
            self.assertIn("false", response.text)

    def test_task_detail_shows_task_contract(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            write_tasks_yaml(root)
            client = TestClient(create_app(root))

            response = client.get("/tasks/web-enabled-task")

            self.assertEqual(response.status_code, 200)
            self.assertIn("web-enabled-task", response.text)
            self.assertIn("Web Enabled Task", response.text)
            self.assertIn("enabled", response.text)
            self.assertIn("backend", response.text)
            self.assertIn("Criteria", response.text)
            self.assertIn("report.status=completed", response.text)
            self.assertIn("require_structured_report", response.text)
            self.assertIn("enabled_check_with_doctor", response.text)

    def test_disabled_task_is_visibly_marked_disabled(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            write_tasks_yaml(root)
            client = TestClient(create_app(root))

            response = client.get("/tasks/web-disabled-task")

            self.assertEqual(response.status_code, 200)
            self.assertIn("enabled=false", response.text)
            self.assertIn("disabled_requires_explicit_enable", response.text)

    def test_task_prompt_preview_is_clipped_when_long(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            write_tasks_yaml(root, long_prompt=True)
            client = TestClient(create_app(root))

            response = client.get("/tasks/web-enabled-task")

            self.assertEqual(response.status_code, 200)
            self.assertIn("Prompt preview clipped to the first 4000 characters.", response.text)

    def test_unknown_task_returns_not_found(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            write_tasks_yaml(root)
            client = TestClient(create_app(root))

            response = client.get("/tasks/missing-task")

            self.assertEqual(response.status_code, 404)

    def test_path_traversal_task_id_returns_not_found(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            write_tasks_yaml(root)
            client = TestClient(create_app(root))

            response = client.get("/tasks/bad%5Cname")

            self.assertIn(response.status_code, {400, 404})

    def test_task_pages_are_read_only_for_tasks_yaml(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            tasks_file = write_tasks_yaml(root)
            before = tasks_file.read_text(encoding="utf-8")
            client = TestClient(create_app(root))

            list_response = client.get("/tasks")
            detail_response = client.get("/tasks/web-enabled-task")

            self.assertEqual(list_response.status_code, 200)
            self.assertEqual(detail_response.status_code, 200)
            self.assertEqual(tasks_file.read_text(encoding="utf-8"), before)


if __name__ == "__main__":
    unittest.main()
