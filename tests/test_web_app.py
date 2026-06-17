from __future__ import annotations

from contextlib import redirect_stdout
import importlib.util
from io import StringIO
import json
from pathlib import Path
import shutil
import subprocess
import sys
import textwrap
import time
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_orchestrator.backends.mock import MockBackend
from ai_orchestrator.engine import TaskExecutionEngine
from ai_orchestrator.pipeline import PipelineSelectedTask, PipelineState, PipelineTaskResult
from ai_orchestrator.schemas import TaskSpec
from ai_orchestrator_web.jobs.actions import build_action_command
from ai_orchestrator_web.jobs.models import create_job_record
from ai_orchestrator_web.jobs.runner import run_job_sync
from ai_orchestrator_web.jobs.store import load_job, save_job
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


def create_web_run(root: Path, *, long_review_packet: bool = False) -> tuple[str, Path]:
    runs_dir = root / ".runs"
    task = TaskSpec(
        description="Create deterministic web run fixture",
        acceptance_criteria=["deterministic demo artifact"],
        max_retries=1,
    )
    state = TaskExecutionEngine(MockBackend(), runs_dir).run(task)
    run_dir = runs_dir / state.run_id
    review_packet = "# REVIEW_PACKET\n\n" + ("R" * 4500 if long_review_packet else "Synthetic review packet preview.\n")
    (run_dir / "REVIEW_PACKET.md").write_text(review_packet, encoding="utf-8")
    return state.run_id, run_dir


def create_web_pipeline(root: Path, *, run_id: str, run_dir: Path) -> tuple[str, Path]:
    pipeline_id = "pipeline_web_fixture"
    pipeline_dir = root / ".runs" / "pipelines" / pipeline_id
    pipeline_dir.mkdir(parents=True, exist_ok=True)
    state = PipelineState(
        pipeline_id=pipeline_id,
        tasks_file=str((root / "tasks.yaml").resolve()),
        status="approved",
        selected_tasks=[PipelineSelectedTask(task_id="web-task", title="Web Task", enabled=True)],
        tasks=[
            PipelineTaskResult(
                task_id="web-task",
                title="Web Task",
                status="approved",
                run_id=run_id,
                final_report=str((run_dir / "final_report.md").resolve()),
                review_packet=str((run_dir / "REVIEW_PACKET.md").resolve()),
                state=str((run_dir / "state.json").resolve()),
            )
        ],
    )
    state_path = pipeline_dir / "pipeline_state.json"
    state.save_json(state_path)
    (pipeline_dir / "PIPELINE_REPORT.md").write_text("# Pipeline Report\n\nSynthetic pipeline preview.\n", encoding="utf-8")
    return pipeline_id, pipeline_dir


def wait_for_job_status(root: Path, job_id: str, *, timeout_seconds: float = 20.0) -> str:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        job = load_job(root, job_id)
        if job.status not in {"queued", "running"}:
            return job.status
        time.sleep(0.05)
    return load_job(root, job_id).status


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

    def test_dashboard_links_to_runs_and_pipelines(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            client = TestClient(create_app(root))

            response = client.get("/")

            self.assertEqual(response.status_code, 200)
            self.assertIn('href="/runs"', response.text)
            self.assertIn('href="/pipelines"', response.text)

    def test_dashboard_links_to_jobs(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            client = TestClient(create_app(root))

            response = client.get("/")

            self.assertEqual(response.status_code, 200)
            self.assertIn('href="/jobs"', response.text)

    def test_dashboard_links_to_new_task_request(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            client = TestClient(create_app(root))

            response = client.get("/")

            self.assertEqual(response.status_code, 200)
            self.assertIn('href="/drafts/new"', response.text)

    def test_home_navigation_exists_on_key_pages(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            draft_id, _draft_dir = scaffold_web_draft(root, task_id="web-nav-draft")
            write_tasks_yaml(root)
            command = build_action_command("web_health_cli", root)
            job = create_job_record(action="web_health_cli", project_root=root, command=command)
            save_job(root, job)
            client = TestClient(create_app(root))

            paths = [
                "/drafts",
                f"/drafts/{draft_id}",
                "/drafts/new",
                "/tasks",
                f"/jobs/{job.job_id}",
            ]
            for path in paths:
                with self.subTest(path=path):
                    response = client.get(path)
                    self.assertEqual(response.status_code, 200)
                    self.assertIn('href="/"', response.text)
                    self.assertIn("Home", response.text)

    def test_drafts_missing_dir_shows_empty_state(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            client = TestClient(create_app(root))

            response = client.get("/drafts")

            self.assertEqual(response.status_code, 200)
            self.assertIn("No task drafts found", response.text)

    def test_drafts_page_links_to_new_task_request(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            client = TestClient(create_app(root))

            response = client.get("/drafts")

            self.assertEqual(response.status_code, 200)
            self.assertIn('href="/drafts/new"', response.text)

    def test_new_draft_form_returns_200(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            client = TestClient(create_app(root))

            response = client.get("/drafts/new")

            self.assertEqual(response.status_code, 200)
            self.assertIn("New Task Request", response.text)
            self.assertIn('name="title"', response.text)
            self.assertIn('name="task_id"', response.text)
            self.assertIn('name="risk_level"', response.text)
            self.assertIn('name="prompt_language"', response.text)
            self.assertIn('name="raw_request"', response.text)
            self.assertIn("does not run Codex", response.text)

    def test_create_draft_empty_raw_request_returns_validation_error(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            client = TestClient(create_app(root))

            response = client.post("/drafts/create", data={"raw_request": "   "})

            self.assertEqual(response.status_code, 400)
            self.assertIn("Raw request is required", response.text)
            self.assertFalse((root / ".task_drafts").exists())

    def test_create_draft_rejects_task_id_path_traversal(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            client = TestClient(create_app(root))

            response = client.post(
                "/drafts/create",
                data={
                    "raw_request": "Create a safe docs-only draft.",
                    "task_id": "..\\bad",
                },
            )

            self.assertEqual(response.status_code, 400)
            self.assertIn("Task ID may contain only", response.text)
            self.assertFalse((root / ".task_drafts").exists())

    def test_create_draft_title_length_validation(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            client = TestClient(create_app(root))

            response = client.post(
                "/drafts/create",
                data={
                    "raw_request": "Create a safe docs-only draft.",
                    "title": "T" * 201,
                },
            )

            self.assertEqual(response.status_code, 400)
            self.assertIn("Title must be", response.text)
            self.assertFalse((root / ".task_drafts").exists())

    def test_create_draft_valid_request_creates_scaffold_job(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            client = TestClient(create_app(root))

            response = client.post(
                "/drafts/create",
                data={
                    "raw_request": "Create a short docs-only task draft from the web form.",
                    "title": "Web Form Draft",
                    "task_id": "0.1.51-web-form-draft",
                    "risk_level": "low",
                    "prompt_language": "ru",
                },
                follow_redirects=False,
            )

            self.assertEqual(response.status_code, 303)
            location = response.headers["location"]
            self.assertTrue(location.startswith("/jobs/job_"))
            job_id = location.rsplit("/", 1)[-1]
            job = load_job(root, job_id)
            self.assertEqual(job.action, "draft_task_scaffold")
            self.assertIsInstance(job.command, list)
            self.assertIn("draft-task-scaffold", job.command)
            self.assertIn("--request", job.command)
            request_path = Path(job.command[job.command.index("--request") + 1])
            self.assertEqual(request_path.parent, (root / ".task_drafts" / "raw_requests").resolve())
            self.assertTrue(request_path.name.startswith("web_request_"))
            self.assertTrue(request_path.name.endswith(".md"))
            self.assertNotIn("0.1.51-web-form-draft", request_path.name)
            self.assertEqual(request_path.read_text(encoding="utf-8"), "Create a short docs-only task draft from the web form.")

            self.assertEqual(wait_for_job_status(root, job_id), "succeeded")
            finished = load_job(root, job_id)
            self.assertEqual(finished.exit_code, 0)
            self.assertFalse((root / "tasks.yaml").exists())
            self.assertFalse((root / ".runs").exists())

            drafts_response = client.get("/drafts")
            detail_response = client.get(location)

            self.assertEqual(drafts_response.status_code, 200)
            self.assertIn("0.1.51-web-form-draft", drafts_response.text)
            self.assertEqual(detail_response.status_code, 200)
            self.assertIn("draft_task_scaffold", detail_response.text)

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
            self.assertIn("Validate draft", response.text)
            self.assertIn(f'action="/drafts/{draft_id}/validate"', response.text)
            self.assertIn("raw_request.md", response.text)
            self.assertIn("task_draft.yaml", response.text)

    def test_validate_draft_post_creates_validation_job(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            draft_id, draft_dir = scaffold_web_draft(root, task_id="web-validate-draft")
            client = TestClient(create_app(root))

            response = client.post(f"/drafts/{draft_id}/validate", follow_redirects=False)

            self.assertEqual(response.status_code, 303)
            location = response.headers["location"]
            self.assertTrue(location.startswith("/jobs/job_"))
            job_id = location.rsplit("/", 1)[-1]
            job = load_job(root, job_id)
            self.assertEqual(job.action, "validate_task_draft")
            self.assertEqual(job.result_refs, {"draft_id": draft_id})
            self.assertIsInstance(job.command, list)
            self.assertIn("validate-task-draft", job.command)
            self.assertIn(draft_id, job.command)
            self.assertIn("--drafts-dir", job.command)
            self.assertIn("--force", job.command)

            self.assertEqual(wait_for_job_status(root, job_id), "succeeded")
            finished = load_job(root, job_id)
            self.assertEqual(finished.exit_code, 0)
            self.assertTrue((draft_dir / "task_draft_validator_report.json").exists())
            self.assertTrue((draft_dir / "task_draft_validator_report.md").exists())
            self.assertFalse((root / "tasks.yaml").exists())
            self.assertFalse((root / ".runs").exists())

            detail_response = client.get(f"/drafts/{draft_id}")
            job_response = client.get(location)

            self.assertEqual(detail_response.status_code, 200)
            self.assertIn("validation_status", detail_response.text)
            self.assertIn("valid_for_promotion", detail_response.text)
            self.assertEqual(job_response.status_code, 200)
            self.assertIn(f'href="/drafts/{draft_id}"', job_response.text)
            self.assertIn("Home", job_response.text)

    def test_validate_unknown_draft_returns_not_found(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            client = TestClient(create_app(root))

            response = client.post("/drafts/missing-draft/validate")

            self.assertEqual(response.status_code, 404)

    def test_validate_path_traversal_draft_id_returns_not_found(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            client = TestClient(create_app(root))

            response = client.post("/drafts/bad%5Cname/validate")

            self.assertIn(response.status_code, {400, 404})

    def test_one_active_job_rule_blocks_validate_job(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            draft_id, _draft_dir = scaffold_web_draft(root, task_id="web-active-job-draft")
            command = build_action_command("web_health_cli", root)
            job = create_job_record(action="web_health_cli", project_root=root, command=command)
            job.status = "running"
            save_job(root, job)
            client = TestClient(create_app(root))

            response = client.post(f"/drafts/{draft_id}/validate")

            self.assertEqual(response.status_code, 409)

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

    def test_runs_missing_dir_shows_empty_state(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            client = TestClient(create_app(root))

            response = client.get("/runs")

            self.assertEqual(response.status_code, 200)
            self.assertIn("No runs found", response.text)

    def test_runs_lists_run_dirs_with_state_json(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, _run_dir = create_web_run(root)
            client = TestClient(create_app(root))

            response = client.get("/runs")

            self.assertEqual(response.status_code, 200)
            self.assertIn(run_id, response.text)
            self.assertIn("validator_status", response.text)
            self.assertIn("next_action", response.text)
            self.assertIn("classify_run", response.text)

    def test_run_detail_shows_lifecycle_status_and_artifact_preview(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, _run_dir = create_web_run(root, long_review_packet=True)
            client = TestClient(create_app(root))

            response = client.get(f"/runs/{run_id}")

            self.assertEqual(response.status_code, 200)
            self.assertIn(run_id, response.text)
            self.assertIn("validator_status", response.text)
            self.assertIn("next_action", response.text)
            self.assertIn("classify_run", response.text)
            self.assertIn("REVIEW_PACKET.md", response.text)
            self.assertIn("Content clipped to the first 4000 characters.", response.text)

    def test_unknown_run_id_returns_not_found(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            client = TestClient(create_app(root))

            response = client.get("/runs/missing-run")

            self.assertEqual(response.status_code, 404)

    def test_path_traversal_run_id_returns_not_found(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            client = TestClient(create_app(root))

            response = client.get("/runs/bad%5Cname")

            self.assertIn(response.status_code, {400, 404})

    def test_pipelines_missing_dir_shows_empty_state(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            client = TestClient(create_app(root))

            response = client.get("/pipelines")

            self.assertEqual(response.status_code, 200)
            self.assertIn("No pipelines found", response.text)

    def test_pipelines_lists_pipeline_dirs_with_state_json(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, run_dir = create_web_run(root)
            pipeline_id, _pipeline_dir = create_web_pipeline(root, run_id=run_id, run_dir=run_dir)
            client = TestClient(create_app(root))

            response = client.get("/pipelines")

            self.assertEqual(response.status_code, 200)
            self.assertIn(pipeline_id, response.text)
            self.assertIn("tasks_total", response.text)
            self.assertIn("classify_runs", response.text)

    def test_pipeline_detail_shows_task_run_link(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, run_dir = create_web_run(root)
            pipeline_id, _pipeline_dir = create_web_pipeline(root, run_id=run_id, run_dir=run_dir)
            client = TestClient(create_app(root))

            response = client.get(f"/pipelines/{pipeline_id}")

            self.assertEqual(response.status_code, 200)
            self.assertIn(pipeline_id, response.text)
            self.assertIn("pipeline_status", response.text)
            self.assertIn("tasks_total", response.text)
            self.assertIn(f'href="/runs/{run_id}"', response.text)
            self.assertIn("Pipeline Report", response.text)

    def test_unknown_pipeline_id_returns_not_found(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            client = TestClient(create_app(root))

            response = client.get("/pipelines/missing-pipeline")

            self.assertEqual(response.status_code, 404)

    def test_path_traversal_pipeline_id_returns_not_found(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            client = TestClient(create_app(root))

            response = client.get("/pipelines/bad%5Cname")

            self.assertIn(response.status_code, {400, 404})

    def test_run_and_pipeline_pages_are_read_only_for_state_and_reports(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, run_dir = create_web_run(root)
            pipeline_id, pipeline_dir = create_web_pipeline(root, run_id=run_id, run_dir=run_dir)
            tracked_paths = [
                run_dir / "state.json",
                run_dir / "REVIEW_PACKET.md",
                pipeline_dir / "pipeline_state.json",
                pipeline_dir / "PIPELINE_REPORT.md",
            ]
            before = {path: path.read_text(encoding="utf-8") for path in tracked_paths}
            client = TestClient(create_app(root))

            responses = [
                client.get("/runs"),
                client.get(f"/runs/{run_id}"),
                client.get("/pipelines"),
                client.get(f"/pipelines/{pipeline_id}"),
            ]

            self.assertTrue(all(response.status_code == 200 for response in responses))
            after = {path: path.read_text(encoding="utf-8") for path in tracked_paths}
            self.assertEqual(after, before)

    def test_jobs_missing_dir_shows_empty_state(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            client = TestClient(create_app(root))

            response = client.get("/jobs")

            self.assertEqual(response.status_code, 200)
            self.assertIn("No jobs found", response.text)
            self.assertIn("web_health_cli", response.text)
            self.assertNotIn("validate_task_draft", response.text)

    def test_start_allowed_job_creates_metadata_and_logs(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            client = TestClient(create_app(root))

            response = client.post("/jobs/start", data={"action": "web_health_cli"}, follow_redirects=False)

            self.assertEqual(response.status_code, 303)
            location = response.headers["location"]
            self.assertTrue(location.startswith("/jobs/job_"))
            job_id = location.rsplit("/", 1)[-1]
            self.assertEqual(wait_for_job_status(root, job_id), "succeeded")
            job = load_job(root, job_id)
            detail = client.get(location)

            self.assertEqual(detail.status_code, 200)
            self.assertEqual(job.action, "web_health_cli")
            self.assertEqual(job.status, "succeeded")
            self.assertEqual(job.exit_code, 0)
            self.assertTrue(Path(job.stdout_path).exists())
            self.assertTrue(Path(job.stderr_path).exists())
            self.assertIn("usage:", Path(job.stdout_path).read_text(encoding="utf-8"))
            self.assertIn("web_health_cli", detail.text)

    def test_unsupported_job_action_returns_bad_request(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            client = TestClient(create_app(root))

            response = client.post("/jobs/start", data={"action": "run-pipeline"})

            self.assertEqual(response.status_code, 400)

    def test_job_id_path_traversal_returns_not_found(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            client = TestClient(create_app(root))

            response = client.get("/jobs/bad%5Cname")

            self.assertIn(response.status_code, {400, 404})

    def test_one_active_job_rule_refuses_second_job(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            command = build_action_command("web_health_cli", root)
            job = create_job_record(action="web_health_cli", project_root=root, command=command)
            job.status = "running"
            save_job(root, job)
            client = TestClient(create_app(root))

            response = client.post("/jobs/start", data={"action": "web_health_cli"})

            self.assertEqual(response.status_code, 409)

    def test_job_runner_uses_argv_list_and_shell_false(self) -> None:
        with TemporaryProject() as root:
            command = build_action_command("web_health_cli", root)
            job = create_job_record(action="web_health_cli", project_root=root, command=command)
            process = Mock()
            process.wait.return_value = 0
            with patch("ai_orchestrator_web.jobs.runner.subprocess.Popen", return_value=process) as popen:
                finished = run_job_sync(job, root)

            args, kwargs = popen.call_args
            self.assertIsInstance(args[0], list)
            self.assertEqual(args[0], command)
            self.assertIs(kwargs["shell"], False)
            self.assertEqual(finished.status, "succeeded")

    def test_draft_scaffold_action_builds_safe_argv(self) -> None:
        with TemporaryProject() as root:
            request_path = root / ".task_drafts" / "raw_requests" / "web_request_fixture.md"
            write_text(request_path, "Create a safe draft.")

            command = build_action_command(
                "draft_task_scaffold",
                root,
                params={
                    "request_path": str(request_path),
                    "title": "Safe Draft",
                    "task_id": "safe-draft",
                    "risk_level": "unknown",
                    "prompt_language": "ru",
                },
            )

            self.assertIsInstance(command, list)
            self.assertIn("draft-task-scaffold", command)
            self.assertIn("--request", command)
            self.assertIn(str(request_path.resolve()), command)
            self.assertIn("--task-id", command)
            self.assertIn("safe-draft", command)

    def test_draft_scaffold_action_rejects_request_path_outside_raw_requests(self) -> None:
        with TemporaryProject() as root:
            request_path = root / "unsafe_request.md"
            write_text(request_path, "Create a safe draft.")

            with self.assertRaises(ValueError):
                build_action_command(
                    "draft_task_scaffold",
                    root,
                    params={"request_path": str(request_path)},
                )

    def test_validate_task_draft_action_builds_safe_argv(self) -> None:
        with TemporaryProject() as root:
            draft_dir = root / ".task_drafts" / "safe-draft"
            draft_dir.mkdir(parents=True)

            command = build_action_command(
                "validate_task_draft",
                root,
                params={"draft_id": "safe-draft"},
            )

            self.assertIsInstance(command, list)
            self.assertIn("validate-task-draft", command)
            self.assertIn("safe-draft", command)
            self.assertIn("--drafts-dir", command)
            self.assertIn(str((root / ".task_drafts").resolve()), command)
            self.assertIn("--force", command)

    def test_validate_task_draft_action_rejects_missing_draft(self) -> None:
        with TemporaryProject() as root:
            with self.assertRaises(ValueError):
                build_action_command(
                    "validate_task_draft",
                    root,
                    params={"draft_id": "missing-draft"},
                )

    def test_validate_task_draft_action_rejects_unsafe_draft_id(self) -> None:
        with TemporaryProject() as root:
            with self.assertRaises(ValueError):
                build_action_command(
                    "validate_task_draft",
                    root,
                    params={"draft_id": "..\\bad"},
                )

    def test_job_json_contains_command_as_list(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            client = TestClient(create_app(root))

            response = client.post("/jobs/start", data={"action": "list_task_drafts"}, follow_redirects=False)

            self.assertEqual(response.status_code, 303)
            job_id = response.headers["location"].rsplit("/", 1)[-1]
            wait_for_job_status(root, job_id)
            payload = json.loads((root / ".web" / "jobs" / f"{job_id}.json").read_text(encoding="utf-8"))
            self.assertIsInstance(payload["command"], list)
            self.assertIn("list-task-drafts", payload["command"])

    def test_web_runtime_directory_is_ignored(self) -> None:
        gitignore = Path(__file__).resolve().parents[1] / ".gitignore"

        self.assertIn(".web/", gitignore.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
