from __future__ import annotations

import importlib.util
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_orchestrator_web.project_status import (
    get_current_project_status,
    get_git_status_summary,
)


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


if __name__ == "__main__":
    unittest.main()
