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
import yaml

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
from ai_orchestrator.cli import draft_task_scaffold_main, revise_task_draft_main, validate_task_draft_main


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


def revise_web_draft_to_valid(root: Path, draft_id: str) -> None:
    stdout = StringIO()
    with redirect_stdout(stdout):
        exit_code = revise_task_draft_main(
            [
                draft_id,
                "--drafts-dir",
                str(root / ".task_drafts"),
                "--risk-level",
                "low",
                "--clear-files-allowed",
                "--allow-file",
                "docs/web_promote_draft.md",
                "--clear-open-questions",
            ]
        )
    if exit_code != 0:
        raise AssertionError(stdout.getvalue())


def validate_web_draft(root: Path, draft_id: str, *extra_args: str) -> None:
    stdout = StringIO()
    with redirect_stdout(stdout):
        exit_code = validate_task_draft_main([draft_id, "--drafts-dir", str(root / ".task_drafts"), *extra_args])
    if exit_code != 0:
        raise AssertionError(stdout.getvalue())


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


def load_task_from_yaml(tasks_file: Path, task_id: str) -> dict:
    payload = yaml.safe_load(tasks_file.read_text(encoding="utf-8"))
    for task in payload["tasks"]:
        if task["id"] == task_id:
            return task
    raise AssertionError(f"missing task {task_id}")


def task_without_enabled(task: dict) -> dict:
    copy = dict(task)
    copy.pop("enabled", None)
    return copy


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


def create_job_without_starting_subprocess(
    *,
    project_root: Path,
    action: str,
    params: dict[str, str] | None = None,
    result_refs: dict[str, str] | None = None,
):
    command = build_action_command(action, project_root.resolve(), params=params)
    job = create_job_record(
        action=action,
        project_root=project_root.resolve(),
        command=command,
        result_refs=result_refs,
    )
    save_job(project_root.resolve(), job)
    return job


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

    def test_draft_detail_shows_promote_disabled_button_for_promotable_draft(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            draft_id, _draft_dir = scaffold_web_draft(root, task_id="web-promotable-draft")
            revise_web_draft_to_valid(root, draft_id)
            validate_web_draft(root, draft_id)
            client = TestClient(create_app(root))

            response = client.get(f"/drafts/{draft_id}")

            self.assertEqual(response.status_code, 200)
            self.assertIn("promote_task_draft", response.text)
            self.assertIn("Promote disabled", response.text)
            self.assertIn(f'action="/drafts/{draft_id}/promote"', response.text)
            self.assertIn("enabled=false", response.text)

    def test_promote_draft_post_creates_disabled_promotion_job(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            draft_id, _draft_dir = scaffold_web_draft(root, task_id="web-promoted-disabled-task")
            revise_web_draft_to_valid(root, draft_id)
            validate_web_draft(root, draft_id)
            client = TestClient(create_app(root))

            response = client.post(f"/drafts/{draft_id}/promote", follow_redirects=False)

            self.assertEqual(response.status_code, 303)
            location = response.headers["location"]
            self.assertTrue(location.startswith("/jobs/job_"))
            job_id = location.rsplit("/", 1)[-1]
            job = load_job(root, job_id)
            self.assertEqual(job.action, "promote_task_draft_disabled")
            self.assertEqual(job.result_refs, {"draft_id": draft_id, "tasks_url": "/tasks"})
            self.assertIsInstance(job.command, list)
            self.assertIn("promote-task-draft", job.command)
            self.assertIn(draft_id, job.command)
            self.assertIn("--drafts-dir", job.command)
            self.assertIn("--tasks-file", job.command)
            self.assertNotIn("--enable", job.command)
            self.assertNotIn("--replace", job.command)

            self.assertEqual(wait_for_job_status(root, job_id), "succeeded")
            finished = load_job(root, job_id)
            self.assertEqual(finished.exit_code, 0)
            self.assertFalse((root / ".runs").exists())
            tasks_file = root / "tasks.yaml"
            self.assertTrue(tasks_file.exists())
            payload = yaml.safe_load(tasks_file.read_text(encoding="utf-8"))
            promoted_task = next(task for task in payload["tasks"] if task["id"] == "web-promoted-disabled-task")
            self.assertIs(promoted_task["enabled"], False)

            tasks_response = client.get("/tasks")
            job_response = client.get(location)

            self.assertEqual(tasks_response.status_code, 200)
            self.assertIn("web-promoted-disabled-task", tasks_response.text)
            self.assertIn("false", tasks_response.text)
            self.assertEqual(job_response.status_code, 200)
            self.assertIn(f'href="/drafts/{draft_id}"', job_response.text)
            self.assertIn('href="/tasks"', job_response.text)

    def test_promote_missing_draft_returns_not_found(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            client = TestClient(create_app(root))

            response = client.post("/drafts/missing-draft/promote")

            self.assertEqual(response.status_code, 404)

    def test_promote_path_traversal_draft_id_returns_not_found(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            client = TestClient(create_app(root))

            response = client.post("/drafts/bad%5Cname/promote")

            self.assertIn(response.status_code, {400, 404})

    def test_promote_not_ready_draft_returns_bad_request_without_job(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            draft_id, _draft_dir = scaffold_web_draft(root, task_id="web-not-ready-promote")
            client = TestClient(create_app(root))

            response = client.post(f"/drafts/{draft_id}/promote")

            self.assertEqual(response.status_code, 400)
            self.assertFalse((root / ".web" / "jobs").exists())
            self.assertFalse((root / "tasks.yaml").exists())

    def test_one_active_job_rule_blocks_promote_job(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            draft_id, _draft_dir = scaffold_web_draft(root, task_id="web-active-promote-draft")
            revise_web_draft_to_valid(root, draft_id)
            validate_web_draft(root, draft_id)
            command = build_action_command("web_health_cli", root)
            job = create_job_record(action="web_health_cli", project_root=root, command=command)
            job.status = "running"
            save_job(root, job)
            client = TestClient(create_app(root))

            response = client.post(f"/drafts/{draft_id}/promote")

            self.assertEqual(response.status_code, 409)
            self.assertFalse((root / "tasks.yaml").exists())

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
            self.assertIn("Doctor dry-run", response.text)
            self.assertIn('action="/tasks/web-enabled-task/doctor-dry-run"', response.text)
            self.assertIn("Pipeline dry-run", response.text)
            self.assertIn('action="/tasks/web-enabled-task/pipeline-dry-run"', response.text)
            self.assertIn("Execution Eligibility", response.text)
            self.assertIn("Disable task", response.text)
            self.assertIn('action="/tasks/web-enabled-task/disable"', response.text)
            self.assertNotIn('action="/tasks/web-enabled-task/enable"', response.text)

    def test_task_detail_shows_real_run_readiness_missing_codex_config(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root, patch.dict(
            "os.environ", {"CODEX_CMD": "", "AI_ORCHESTRATOR_CODEX_CMD": ""}
        ):
            write_tasks_yaml(root)
            client = TestClient(create_app(root))

            response = client.get("/tasks/web-enabled-task")

            self.assertEqual(response.status_code, 200)
            self.assertIn("Real-run Readiness", response.text)
            self.assertIn("codex_cmd=not_configured", response.text)
            self.assertIn("Set CODEX_CMD or AI_ORCHESTRATOR_CODEX_CMD", response.text)
            self.assertNotIn('action="/tasks/web-enabled-task/doctor-real-run"', response.text)
            self.assertIn("Real Execution", response.text)
            self.assertIn("Codex command is not configured", response.text)
            self.assertNotIn('action="/tasks/web-enabled-task/run-pipeline"', response.text)

    def test_task_detail_shows_real_run_readiness_button_when_codex_configured(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root, patch.dict("os.environ", {"CODEX_CMD": sys.executable}):
            write_tasks_yaml(root)
            client = TestClient(create_app(root))

            response = client.get("/tasks/web-enabled-task")

            self.assertEqual(response.status_code, 200)
            self.assertIn("Real-run Readiness", response.text)
            self.assertIn("codex_cmd=configured", response.text)
            self.assertIn("Doctor real-run", response.text)
            self.assertIn('action="/tasks/web-enabled-task/doctor-real-run"', response.text)
            self.assertIn("Real Execution", response.text)
            self.assertIn("confirm_real_pipeline", response.text)
            self.assertIn("I understand this will run Codex in an isolated workspace.", response.text)
            self.assertIn("Run real pipeline", response.text)
            self.assertIn('action="/tasks/web-enabled-task/run-pipeline"', response.text)

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
            self.assertIn("Task is disabled. Doctor dry-run is expected to report", response.text)
            self.assertIn('action="/tasks/web-disabled-task/doctor-dry-run"', response.text)
            self.assertIn("Task is disabled. Pipeline dry-run is expected to report skip_disabled", response.text)
            self.assertIn('action="/tasks/web-disabled-task/pipeline-dry-run"', response.text)
            self.assertIn("Execution Eligibility", response.text)
            self.assertIn("Enable task", response.text)
            self.assertIn('action="/tasks/web-disabled-task/enable"', response.text)
            self.assertNotIn('action="/tasks/web-disabled-task/disable"', response.text)
            self.assertIn("Task is disabled. Doctor real-run is expected to report", response.text)
            self.assertIn("Task is disabled. Enable it before real pipeline execution.", response.text)
            self.assertNotIn('action="/tasks/web-disabled-task/run-pipeline"', response.text)

    def test_enable_task_sets_enabled_true_without_execution_artifacts(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            tasks_file = write_tasks_yaml(root)
            before = load_task_from_yaml(tasks_file, "web-disabled-task")
            client = TestClient(create_app(root))

            response = client.post("/tasks/web-disabled-task/enable", follow_redirects=False)

            self.assertEqual(response.status_code, 303)
            self.assertEqual(response.headers["location"], "/tasks/web-disabled-task")
            after = load_task_from_yaml(tasks_file, "web-disabled-task")
            self.assertFalse(before["enabled"])
            self.assertTrue(after["enabled"])
            self.assertEqual(task_without_enabled(after), task_without_enabled(before))
            self.assertFalse((root / ".runs").exists())
            self.assertFalse((root / ".task_drafts").exists())
            self.assertFalse((root / ".web" / "jobs").exists())

            detail = client.get("/tasks/web-disabled-task")
            task_list = client.get("/tasks")

            self.assertEqual(detail.status_code, 200)
            self.assertIn("enabled=true", detail.text)
            self.assertIn("Disable task", detail.text)
            self.assertEqual(task_list.status_code, 200)
            self.assertIn("web-disabled-task", task_list.text)

    def test_disable_task_sets_enabled_false_without_execution_artifacts(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            tasks_file = write_tasks_yaml(root)
            before = load_task_from_yaml(tasks_file, "web-enabled-task")
            client = TestClient(create_app(root))

            response = client.post("/tasks/web-enabled-task/disable", follow_redirects=False)

            self.assertEqual(response.status_code, 303)
            self.assertEqual(response.headers["location"], "/tasks/web-enabled-task")
            after = load_task_from_yaml(tasks_file, "web-enabled-task")
            self.assertTrue(before["enabled"])
            self.assertFalse(after["enabled"])
            self.assertEqual(task_without_enabled(after), task_without_enabled(before))
            self.assertFalse((root / ".runs").exists())
            self.assertFalse((root / ".task_drafts").exists())
            self.assertFalse((root / ".web" / "jobs").exists())

            detail = client.get("/tasks/web-enabled-task")

            self.assertEqual(detail.status_code, 200)
            self.assertIn("enabled=false", detail.text)
            self.assertIn("Enable task", detail.text)

    def test_enable_disable_unknown_task_returns_not_found(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            write_tasks_yaml(root)
            client = TestClient(create_app(root))

            enable_response = client.post("/tasks/missing-task/enable")
            disable_response = client.post("/tasks/missing-task/disable")

            self.assertEqual(enable_response.status_code, 404)
            self.assertEqual(disable_response.status_code, 404)

    def test_enable_disable_path_traversal_task_id_returns_not_found(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            write_tasks_yaml(root)
            client = TestClient(create_app(root))

            enable_response = client.post("/tasks/bad%5Cname/enable")
            disable_response = client.post("/tasks/bad%5Cname/disable")

            self.assertIn(enable_response.status_code, {400, 404})
            self.assertIn(disable_response.status_code, {400, 404})

    def test_doctor_dry_run_post_creates_diagnostic_job(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            tasks_file = write_tasks_yaml(root)
            before_tasks = tasks_file.read_text(encoding="utf-8")
            client = TestClient(create_app(root))

            response = client.post("/tasks/web-enabled-task/doctor-dry-run", follow_redirects=False)

            self.assertEqual(response.status_code, 303)
            location = response.headers["location"]
            self.assertTrue(location.startswith("/jobs/job_"))
            job_id = location.rsplit("/", 1)[-1]
            job = load_job(root, job_id)
            self.assertEqual(job.action, "doctor_dry_run")
            self.assertEqual(
                job.result_refs,
                {
                    "task_id": "web-enabled-task",
                    "task_url": "/tasks/web-enabled-task",
                    "tasks_url": "/tasks",
                },
            )
            self.assertIsInstance(job.command, list)
            self.assertIn("doctor", job.command)
            self.assertIn("--tasks-file", job.command)
            self.assertIn(str(tasks_file.resolve()), job.command)
            self.assertIn("--task-id", job.command)
            self.assertIn("web-enabled-task", job.command)
            self.assertIn("--intent", job.command)
            self.assertIn("dry-run", job.command)
            self.assertNotIn("--codex-cmd", job.command)

            final_status = wait_for_job_status(root, job_id)
            self.assertIn(final_status, {"succeeded", "failed"})
            finished = load_job(root, job_id)
            self.assertEqual(finished.status, final_status)
            self.assertTrue(Path(finished.stdout_path).exists())
            self.assertTrue(Path(finished.stderr_path).exists())
            self.assertFalse((root / ".runs").exists())
            self.assertFalse((root / ".task_drafts").exists())
            self.assertEqual(tasks_file.read_text(encoding="utf-8"), before_tasks)

            detail = client.get(location)

            self.assertEqual(detail.status_code, 200)
            self.assertIn('href="/tasks/web-enabled-task"', detail.text)
            self.assertIn('href="/tasks"', detail.text)

    def test_doctor_dry_run_missing_task_returns_not_found(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            write_tasks_yaml(root)
            client = TestClient(create_app(root))

            response = client.post("/tasks/missing-task/doctor-dry-run")

            self.assertEqual(response.status_code, 404)

    def test_doctor_dry_run_missing_tasks_yaml_returns_not_found(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            client = TestClient(create_app(root))

            response = client.post("/tasks/web-enabled-task/doctor-dry-run")

            self.assertEqual(response.status_code, 404)

    def test_doctor_dry_run_path_traversal_task_id_returns_not_found(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            write_tasks_yaml(root)
            client = TestClient(create_app(root))

            response = client.post("/tasks/bad%5Cname/doctor-dry-run")

            self.assertIn(response.status_code, {400, 404})

    def test_one_active_job_rule_blocks_doctor_dry_run_job(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            tasks_file = write_tasks_yaml(root)
            before_tasks = tasks_file.read_text(encoding="utf-8")
            command = build_action_command("web_health_cli", root)
            job = create_job_record(action="web_health_cli", project_root=root, command=command)
            job.status = "running"
            save_job(root, job)
            client = TestClient(create_app(root))

            response = client.post("/tasks/web-enabled-task/doctor-dry-run")

            self.assertEqual(response.status_code, 409)
            self.assertEqual(tasks_file.read_text(encoding="utf-8"), before_tasks)
            self.assertFalse((root / ".runs").exists())

    def test_pipeline_dry_run_post_creates_planning_job(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            tasks_file = write_tasks_yaml(root)
            before_tasks = tasks_file.read_text(encoding="utf-8")
            client = TestClient(create_app(root))

            response = client.post("/tasks/web-enabled-task/pipeline-dry-run", follow_redirects=False)

            self.assertEqual(response.status_code, 303)
            location = response.headers["location"]
            self.assertTrue(location.startswith("/jobs/job_"))
            job_id = location.rsplit("/", 1)[-1]
            job = load_job(root, job_id)
            self.assertEqual(job.action, "pipeline_dry_run")
            self.assertEqual(
                job.result_refs,
                {
                    "task_id": "web-enabled-task",
                    "task_url": "/tasks/web-enabled-task",
                    "tasks_url": "/tasks",
                },
            )
            self.assertIsInstance(job.command, list)
            self.assertIn("run-pipeline", job.command)
            self.assertIn("--tasks-file", job.command)
            self.assertIn(str(tasks_file.resolve()), job.command)
            self.assertIn("--only", job.command)
            self.assertIn("web-enabled-task", job.command)
            self.assertIn("--dry-run", job.command)
            self.assertNotIn("--codex-cmd", job.command)
            self.assertNotIn("--verbose", job.command)
            self.assertNotIn("--stream-codex-output", job.command)

            final_status = wait_for_job_status(root, job_id)
            self.assertIn(final_status, {"succeeded", "failed"})
            finished = load_job(root, job_id)
            self.assertEqual(finished.status, final_status)
            self.assertTrue(Path(finished.stdout_path).exists())
            self.assertTrue(Path(finished.stderr_path).exists())
            self.assertFalse((root / ".runs").exists())
            self.assertFalse((root / ".task_drafts").exists())
            self.assertEqual(tasks_file.read_text(encoding="utf-8"), before_tasks)

            detail = client.get(location)

            self.assertEqual(detail.status_code, 200)
            self.assertIn('href="/tasks/web-enabled-task"', detail.text)
            self.assertIn('href="/tasks"', detail.text)

    def test_pipeline_dry_run_missing_task_returns_not_found(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            write_tasks_yaml(root)
            client = TestClient(create_app(root))

            response = client.post("/tasks/missing-task/pipeline-dry-run")

            self.assertEqual(response.status_code, 404)

    def test_pipeline_dry_run_missing_tasks_yaml_returns_not_found(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            client = TestClient(create_app(root))

            response = client.post("/tasks/web-enabled-task/pipeline-dry-run")

            self.assertEqual(response.status_code, 404)

    def test_pipeline_dry_run_path_traversal_task_id_returns_not_found(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            write_tasks_yaml(root)
            client = TestClient(create_app(root))

            response = client.post("/tasks/bad%5Cname/pipeline-dry-run")

            self.assertIn(response.status_code, {400, 404})

    def test_one_active_job_rule_blocks_pipeline_dry_run_job(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            tasks_file = write_tasks_yaml(root)
            before_tasks = tasks_file.read_text(encoding="utf-8")
            command = build_action_command("web_health_cli", root)
            job = create_job_record(action="web_health_cli", project_root=root, command=command)
            job.status = "running"
            save_job(root, job)
            client = TestClient(create_app(root))

            response = client.post("/tasks/web-enabled-task/pipeline-dry-run")

            self.assertEqual(response.status_code, 409)
            self.assertEqual(tasks_file.read_text(encoding="utf-8"), before_tasks)
            self.assertFalse((root / ".runs").exists())

    def test_doctor_real_run_without_codex_env_returns_bad_request(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root, patch.dict(
            "os.environ", {"CODEX_CMD": "", "AI_ORCHESTRATOR_CODEX_CMD": ""}
        ):
            tasks_file = write_tasks_yaml(root)
            before_tasks = tasks_file.read_text(encoding="utf-8")
            client = TestClient(create_app(root))

            response = client.post("/tasks/web-enabled-task/doctor-real-run")

            self.assertEqual(response.status_code, 400)
            self.assertIn("Set CODEX_CMD or AI_ORCHESTRATOR_CODEX_CMD", response.text)
            self.assertFalse((root / ".web" / "jobs").exists())
            self.assertFalse((root / ".runs").exists())
            self.assertEqual(tasks_file.read_text(encoding="utf-8"), before_tasks)

    def test_doctor_real_run_post_creates_readiness_job(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root, patch.dict("os.environ", {"CODEX_CMD": sys.executable}):
            tasks_file = write_tasks_yaml(root)
            before_tasks = tasks_file.read_text(encoding="utf-8")
            client = TestClient(create_app(root))

            response = client.post("/tasks/web-enabled-task/doctor-real-run", follow_redirects=False)

            self.assertEqual(response.status_code, 303)
            location = response.headers["location"]
            self.assertTrue(location.startswith("/jobs/job_"))
            job_id = location.rsplit("/", 1)[-1]
            job = load_job(root, job_id)
            self.assertEqual(job.action, "doctor_real_run")
            self.assertEqual(
                job.result_refs,
                {
                    "task_id": "web-enabled-task",
                    "task_url": "/tasks/web-enabled-task",
                    "tasks_url": "/tasks",
                },
            )
            self.assertIsInstance(job.command, list)
            self.assertIn("doctor", job.command)
            self.assertIn("--tasks-file", job.command)
            self.assertIn(str(tasks_file.resolve()), job.command)
            self.assertIn("--task-id", job.command)
            self.assertIn("web-enabled-task", job.command)
            self.assertIn("--intent", job.command)
            self.assertIn("real-run", job.command)
            self.assertIn("--codex-cmd", job.command)
            self.assertIn(sys.executable, job.command)
            self.assertNotIn("run-pipeline", job.command)

            final_status = wait_for_job_status(root, job_id)
            self.assertIn(final_status, {"succeeded", "failed"})
            finished = load_job(root, job_id)
            self.assertEqual(finished.status, final_status)
            self.assertTrue(Path(finished.stdout_path).exists())
            self.assertTrue(Path(finished.stderr_path).exists())
            self.assertFalse((root / ".runs").exists())
            self.assertFalse((root / ".task_drafts").exists())
            self.assertEqual(tasks_file.read_text(encoding="utf-8"), before_tasks)

            detail = client.get(location)

            self.assertEqual(detail.status_code, 200)
            self.assertIn('href="/tasks/web-enabled-task"', detail.text)
            self.assertIn('href="/tasks"', detail.text)

    def test_doctor_real_run_missing_task_returns_not_found(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root, patch.dict("os.environ", {"CODEX_CMD": sys.executable}):
            write_tasks_yaml(root)
            client = TestClient(create_app(root))

            response = client.post("/tasks/missing-task/doctor-real-run")

            self.assertEqual(response.status_code, 404)

    def test_doctor_real_run_path_traversal_task_id_returns_not_found(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root, patch.dict("os.environ", {"CODEX_CMD": sys.executable}):
            write_tasks_yaml(root)
            client = TestClient(create_app(root))

            response = client.post("/tasks/bad%5Cname/doctor-real-run")

            self.assertIn(response.status_code, {400, 404})

    def test_one_active_job_rule_blocks_doctor_real_run_job(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root, patch.dict("os.environ", {"CODEX_CMD": sys.executable}):
            tasks_file = write_tasks_yaml(root)
            before_tasks = tasks_file.read_text(encoding="utf-8")
            command = build_action_command("web_health_cli", root)
            job = create_job_record(action="web_health_cli", project_root=root, command=command)
            job.status = "running"
            save_job(root, job)
            client = TestClient(create_app(root))

            response = client.post("/tasks/web-enabled-task/doctor-real-run")

            self.assertEqual(response.status_code, 409)
            self.assertEqual(tasks_file.read_text(encoding="utf-8"), before_tasks)
            self.assertFalse((root / ".runs").exists())

    def test_run_pipeline_missing_codex_env_returns_bad_request_without_job(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root, patch.dict(
            "os.environ", {"CODEX_CMD": "", "AI_ORCHESTRATOR_CODEX_CMD": ""}
        ):
            tasks_file = write_tasks_yaml(root)
            before_tasks = tasks_file.read_text(encoding="utf-8")
            client = TestClient(create_app(root))

            response = client.post(
                "/tasks/web-enabled-task/run-pipeline",
                data={"confirm_real_pipeline": "yes"},
            )

            self.assertEqual(response.status_code, 400)
            self.assertIn("Set CODEX_CMD or AI_ORCHESTRATOR_CODEX_CMD", response.text)
            self.assertFalse((root / ".web" / "jobs").exists())
            self.assertFalse((root / ".runs").exists())
            self.assertEqual(tasks_file.read_text(encoding="utf-8"), before_tasks)

    def test_run_pipeline_missing_confirmation_returns_bad_request_without_job(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root, patch.dict("os.environ", {"CODEX_CMD": sys.executable}):
            tasks_file = write_tasks_yaml(root)
            before_tasks = tasks_file.read_text(encoding="utf-8")
            client = TestClient(create_app(root))

            response = client.post("/tasks/web-enabled-task/run-pipeline")

            self.assertEqual(response.status_code, 400)
            self.assertIn("Explicit real pipeline confirmation is required", response.text)
            self.assertFalse((root / ".web" / "jobs").exists())
            self.assertFalse((root / ".runs").exists())
            self.assertEqual(tasks_file.read_text(encoding="utf-8"), before_tasks)

    def test_run_pipeline_disabled_task_returns_bad_request_without_job(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root, patch.dict("os.environ", {"CODEX_CMD": sys.executable}):
            tasks_file = write_tasks_yaml(root)
            before_tasks = tasks_file.read_text(encoding="utf-8")
            client = TestClient(create_app(root))

            response = client.post(
                "/tasks/web-disabled-task/run-pipeline",
                data={"confirm_real_pipeline": "yes"},
            )

            self.assertEqual(response.status_code, 400)
            self.assertIn("Task is disabled", response.text)
            self.assertFalse((root / ".web" / "jobs").exists())
            self.assertFalse((root / ".runs").exists())
            self.assertEqual(tasks_file.read_text(encoding="utf-8"), before_tasks)

    def test_run_pipeline_post_creates_real_pipeline_job_without_starting_pipeline_in_test(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root, patch.dict("os.environ", {"CODEX_CMD": sys.executable}):
            tasks_file = write_tasks_yaml(root)
            before_tasks = tasks_file.read_text(encoding="utf-8")
            client = TestClient(create_app(root))

            with patch(
                "ai_orchestrator_web.routes.tasks.start_background_job",
                side_effect=create_job_without_starting_subprocess,
            ) as start_job:
                response = client.post(
                    "/tasks/web-enabled-task/run-pipeline",
                    data={"confirm_real_pipeline": "yes"},
                    follow_redirects=False,
                )

            self.assertEqual(response.status_code, 303)
            location = response.headers["location"]
            self.assertTrue(location.startswith("/jobs/job_"))
            start_job.assert_called_once()
            job_id = location.rsplit("/", 1)[-1]
            job = load_job(root, job_id)
            self.assertEqual(job.action, "run_pipeline_real")
            self.assertEqual(
                job.result_refs,
                {
                    "task_id": "web-enabled-task",
                    "task_url": "/tasks/web-enabled-task",
                    "tasks_url": "/tasks",
                    "pipelines_url": "/pipelines",
                    "runs_url": "/runs",
                },
            )
            self.assertIsInstance(job.command, list)
            self.assertIn("run-pipeline", job.command)
            self.assertIn("--tasks-file", job.command)
            self.assertIn(str(tasks_file.resolve()), job.command)
            self.assertIn("--only", job.command)
            self.assertIn("web-enabled-task", job.command)
            self.assertIn("--codex-cmd", job.command)
            self.assertIn(sys.executable, job.command)
            self.assertIn("--verbose", job.command)
            self.assertIn("--stream-codex-output", job.command)
            self.assertNotIn("--dry-run", job.command)
            self.assertFalse((root / ".runs").exists())
            self.assertFalse((root / ".task_drafts").exists())
            self.assertEqual(tasks_file.read_text(encoding="utf-8"), before_tasks)

            detail = client.get(location)

            self.assertEqual(detail.status_code, 200)
            self.assertIn('href="/tasks/web-enabled-task"', detail.text)
            self.assertIn('href="/tasks"', detail.text)
            self.assertIn('href="/pipelines"', detail.text)
            self.assertIn('href="/runs"', detail.text)

    def test_run_pipeline_missing_task_returns_not_found(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root, patch.dict("os.environ", {"CODEX_CMD": sys.executable}):
            write_tasks_yaml(root)
            client = TestClient(create_app(root))

            response = client.post(
                "/tasks/missing-task/run-pipeline",
                data={"confirm_real_pipeline": "yes"},
            )

            self.assertEqual(response.status_code, 404)

    def test_run_pipeline_path_traversal_task_id_returns_not_found(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root, patch.dict("os.environ", {"CODEX_CMD": sys.executable}):
            write_tasks_yaml(root)
            client = TestClient(create_app(root))

            response = client.post(
                "/tasks/bad%5Cname/run-pipeline",
                data={"confirm_real_pipeline": "yes"},
            )

            self.assertIn(response.status_code, {400, 404})

    def test_one_active_job_rule_blocks_run_pipeline_job(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root, patch.dict("os.environ", {"CODEX_CMD": sys.executable}):
            tasks_file = write_tasks_yaml(root)
            before_tasks = tasks_file.read_text(encoding="utf-8")
            command = build_action_command("web_health_cli", root)
            job = create_job_record(action="web_health_cli", project_root=root, command=command)
            job.status = "running"
            save_job(root, job)
            client = TestClient(create_app(root))

            response = client.post(
                "/tasks/web-enabled-task/run-pipeline",
                data={"confirm_real_pipeline": "yes"},
            )

            self.assertEqual(response.status_code, 409)
            self.assertEqual(tasks_file.read_text(encoding="utf-8"), before_tasks)
            self.assertFalse((root / ".runs").exists())

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
            self.assertIn("Post-run Analysis", response.text)
            self.assertIn("Recommended next action: classify_run", response.text)
            self.assertIn("Classify run", response.text)
            self.assertIn(f'action="/runs/{run_id}/classify"', response.text)
            self.assertIn("Classify this run before running review checks.", response.text)
            self.assertIn("Run review checks", response.text)
            self.assertIn(f'action="/runs/{run_id}/review-checks"', response.text)

    def test_classify_run_post_creates_analysis_job_without_starting_cli_in_test(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, run_dir = create_web_run(root)
            before_state = (run_dir / "state.json").read_text(encoding="utf-8")
            client = TestClient(create_app(root))

            with patch(
                "ai_orchestrator_web.routes.runs.start_background_job",
                side_effect=create_job_without_starting_subprocess,
            ) as start_job:
                response = client.post(f"/runs/{run_id}/classify", follow_redirects=False)

            self.assertEqual(response.status_code, 303)
            location = response.headers["location"]
            self.assertTrue(location.startswith("/jobs/job_"))
            start_job.assert_called_once()
            job_id = location.rsplit("/", 1)[-1]
            job = load_job(root, job_id)
            self.assertEqual(job.action, "classify_run")
            self.assertEqual(
                job.result_refs,
                {
                    "run_id": run_id,
                    "run_url": f"/runs/{run_id}",
                    "runs_url": "/runs",
                    "pipelines_url": "/pipelines",
                },
            )
            self.assertIsInstance(job.command, list)
            self.assertIn("classify-run", job.command)
            self.assertIn(run_id, job.command)
            self.assertIn("--runs-dir", job.command)
            self.assertIn(str((root / ".runs").resolve()), job.command)
            self.assertNotIn("--codex-cmd", job.command)
            self.assertNotIn("run-pipeline", job.command)
            self.assertNotIn("apply-run", job.command)
            self.assertNotIn("accept-run", job.command)
            self.assertFalse((root / ".task_drafts").exists())
            self.assertFalse((root / "tasks.yaml").exists())
            self.assertEqual((run_dir / "state.json").read_text(encoding="utf-8"), before_state)

            detail = client.get(location)

            self.assertEqual(detail.status_code, 200)
            self.assertIn(f'href="/runs/{run_id}"', detail.text)
            self.assertIn('href="/runs"', detail.text)
            self.assertIn('href="/pipelines"', detail.text)

    def test_run_review_checks_post_creates_analysis_job_without_starting_cli_in_test(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, run_dir = create_web_run(root)
            before_state = (run_dir / "state.json").read_text(encoding="utf-8")
            client = TestClient(create_app(root))

            with patch(
                "ai_orchestrator_web.routes.runs.start_background_job",
                side_effect=create_job_without_starting_subprocess,
            ) as start_job:
                response = client.post(f"/runs/{run_id}/review-checks", follow_redirects=False)

            self.assertEqual(response.status_code, 303)
            location = response.headers["location"]
            self.assertTrue(location.startswith("/jobs/job_"))
            start_job.assert_called_once()
            job_id = location.rsplit("/", 1)[-1]
            job = load_job(root, job_id)
            self.assertEqual(job.action, "run_review_checks")
            self.assertEqual(
                job.result_refs,
                {
                    "run_id": run_id,
                    "run_url": f"/runs/{run_id}",
                    "runs_url": "/runs",
                    "pipelines_url": "/pipelines",
                },
            )
            self.assertIsInstance(job.command, list)
            self.assertIn("run-review-checks", job.command)
            self.assertIn(run_id, job.command)
            self.assertIn("--runs-dir", job.command)
            self.assertIn(str((root / ".runs").resolve()), job.command)
            self.assertNotIn("--codex-cmd", job.command)
            self.assertNotIn("run-pipeline", job.command)
            self.assertNotIn("apply-run", job.command)
            self.assertNotIn("accept-run", job.command)
            self.assertFalse((root / ".task_drafts").exists())
            self.assertFalse((root / "tasks.yaml").exists())
            self.assertEqual((run_dir / "state.json").read_text(encoding="utf-8"), before_state)

            detail = client.get(location)

            self.assertEqual(detail.status_code, 200)
            self.assertIn(f'href="/runs/{run_id}"', detail.text)
            self.assertIn('href="/runs"', detail.text)
            self.assertIn('href="/pipelines"', detail.text)

    def test_classify_run_unknown_run_returns_not_found(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            client = TestClient(create_app(root))

            response = client.post("/runs/missing-run/classify")

            self.assertEqual(response.status_code, 404)

    def test_run_review_checks_unknown_run_returns_not_found(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            client = TestClient(create_app(root))

            response = client.post("/runs/missing-run/review-checks")

            self.assertEqual(response.status_code, 404)

    def test_classify_run_path_traversal_run_id_returns_not_found(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            client = TestClient(create_app(root))

            response = client.post("/runs/bad%5Cname/classify")

            self.assertIn(response.status_code, {400, 404})

    def test_run_review_checks_path_traversal_run_id_returns_not_found(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            client = TestClient(create_app(root))

            response = client.post("/runs/bad%5Cname/review-checks")

            self.assertIn(response.status_code, {400, 404})

    def test_one_active_job_rule_blocks_classify_run_job(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, run_dir = create_web_run(root)
            before_state = (run_dir / "state.json").read_text(encoding="utf-8")
            command = build_action_command("web_health_cli", root)
            job = create_job_record(action="web_health_cli", project_root=root, command=command)
            job.status = "running"
            save_job(root, job)
            client = TestClient(create_app(root))

            response = client.post(f"/runs/{run_id}/classify")

            self.assertEqual(response.status_code, 409)
            self.assertEqual((run_dir / "state.json").read_text(encoding="utf-8"), before_state)

    def test_one_active_job_rule_blocks_run_review_checks_job(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, run_dir = create_web_run(root)
            before_state = (run_dir / "state.json").read_text(encoding="utf-8")
            command = build_action_command("web_health_cli", root)
            job = create_job_record(action="web_health_cli", project_root=root, command=command)
            job.status = "running"
            save_job(root, job)
            client = TestClient(create_app(root))

            response = client.post(f"/runs/{run_id}/review-checks")

            self.assertEqual(response.status_code, 409)
            self.assertEqual((run_dir / "state.json").read_text(encoding="utf-8"), before_state)

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
            self.assertNotIn("No run-pipeline", response.text)
            self.assertNotIn("No real pipeline execution", response.text)
            self.assertNotIn("Codex actions are exposed", response.text)
            self.assertIn("generic form exposes only safe allowlisted actions", response.text)
            self.assertIn("Pipeline dry-run is planning-only", response.text)
            self.assertIn("--dry-run", response.text)
            self.assertIn("Real pipeline execution is available only from task detail", response.text)
            self.assertIn("requires an enabled task", response.text)
            self.assertIn("explicit confirmation", response.text)
            self.assertNotIn("validate_task_draft", response.text)
            self.assertNotIn("promote_task_draft_disabled", response.text)
            self.assertNotIn("doctor_dry_run", response.text)
            self.assertNotIn("doctor_real_run", response.text)
            self.assertNotIn("pipeline_dry_run", response.text)
            self.assertNotIn("run_pipeline_real", response.text)
            self.assertNotIn("classify_run", response.text)
            self.assertNotIn("run_review_checks", response.text)
            self.assertNotIn("enable_task", response.text)
            self.assertNotIn("disable_task", response.text)

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

    def test_promote_task_draft_disabled_action_builds_safe_argv(self) -> None:
        with TemporaryProject() as root:
            draft_dir = root / ".task_drafts" / "safe-draft"
            draft_dir.mkdir(parents=True)

            command = build_action_command(
                "promote_task_draft_disabled",
                root,
                params={"draft_id": "safe-draft"},
            )

            self.assertIsInstance(command, list)
            self.assertIn("promote-task-draft", command)
            self.assertIn("safe-draft", command)
            self.assertIn("--drafts-dir", command)
            self.assertIn(str((root / ".task_drafts").resolve()), command)
            self.assertIn("--tasks-file", command)
            self.assertIn(str((root / "tasks.yaml").resolve()), command)
            self.assertNotIn("--enable", command)
            self.assertNotIn("--replace", command)

    def test_promote_task_draft_disabled_action_rejects_missing_draft(self) -> None:
        with TemporaryProject() as root:
            with self.assertRaises(ValueError):
                build_action_command(
                    "promote_task_draft_disabled",
                    root,
                    params={"draft_id": "missing-draft"},
                )

    def test_promote_task_draft_disabled_action_rejects_unsafe_draft_id(self) -> None:
        with TemporaryProject() as root:
            with self.assertRaises(ValueError):
                build_action_command(
                    "promote_task_draft_disabled",
                    root,
                    params={"draft_id": "..\\bad"},
                )

    def test_doctor_dry_run_action_builds_safe_argv(self) -> None:
        with TemporaryProject() as root:
            tasks_file = write_tasks_yaml(root)

            command = build_action_command(
                "doctor_dry_run",
                root,
                params={"task_id": "web-enabled-task"},
            )

            self.assertIsInstance(command, list)
            self.assertIn("doctor", command)
            self.assertIn("--tasks-file", command)
            self.assertIn(str(tasks_file.resolve()), command)
            self.assertIn("--task-id", command)
            self.assertIn("web-enabled-task", command)
            self.assertIn("--intent", command)
            self.assertIn("dry-run", command)
            self.assertNotIn("--codex-cmd", command)

    def test_doctor_dry_run_action_rejects_missing_task(self) -> None:
        with TemporaryProject() as root:
            write_tasks_yaml(root)

            with self.assertRaises(ValueError):
                build_action_command(
                    "doctor_dry_run",
                    root,
                    params={"task_id": "missing-task"},
                )

    def test_doctor_dry_run_action_rejects_missing_tasks_yaml(self) -> None:
        with TemporaryProject() as root:
            with self.assertRaises(ValueError):
                build_action_command(
                    "doctor_dry_run",
                    root,
                    params={"task_id": "web-enabled-task"},
                )

    def test_doctor_dry_run_action_rejects_unsafe_task_id(self) -> None:
        with TemporaryProject() as root:
            write_tasks_yaml(root)

            with self.assertRaises(ValueError):
                build_action_command(
                    "doctor_dry_run",
                    root,
                    params={"task_id": "..\\bad"},
                )

    def test_doctor_real_run_action_builds_safe_argv(self) -> None:
        with TemporaryProject() as root:
            tasks_file = write_tasks_yaml(root)

            command = build_action_command(
                "doctor_real_run",
                root,
                params={"task_id": "web-enabled-task", "codex_cmd": sys.executable},
            )

            self.assertIsInstance(command, list)
            self.assertIn("doctor", command)
            self.assertIn("--tasks-file", command)
            self.assertIn(str(tasks_file.resolve()), command)
            self.assertIn("--task-id", command)
            self.assertIn("web-enabled-task", command)
            self.assertIn("--intent", command)
            self.assertIn("real-run", command)
            self.assertIn("--codex-cmd", command)
            self.assertIn(sys.executable, command)
            self.assertNotIn("run-pipeline", command)

    def test_doctor_real_run_action_rejects_missing_codex_cmd(self) -> None:
        with TemporaryProject() as root:
            write_tasks_yaml(root)

            with self.assertRaises(ValueError):
                build_action_command(
                    "doctor_real_run",
                    root,
                    params={"task_id": "web-enabled-task"},
                )

    def test_doctor_real_run_action_rejects_missing_task(self) -> None:
        with TemporaryProject() as root:
            write_tasks_yaml(root)

            with self.assertRaises(ValueError):
                build_action_command(
                    "doctor_real_run",
                    root,
                    params={"task_id": "missing-task", "codex_cmd": sys.executable},
                )

    def test_doctor_real_run_action_rejects_unsafe_task_id(self) -> None:
        with TemporaryProject() as root:
            write_tasks_yaml(root)

            with self.assertRaises(ValueError):
                build_action_command(
                    "doctor_real_run",
                    root,
                    params={"task_id": "..\\bad", "codex_cmd": sys.executable},
                )

    def test_pipeline_dry_run_action_builds_safe_argv(self) -> None:
        with TemporaryProject() as root:
            tasks_file = write_tasks_yaml(root)

            command = build_action_command(
                "pipeline_dry_run",
                root,
                params={"task_id": "web-enabled-task"},
            )

            self.assertIsInstance(command, list)
            self.assertIn("run-pipeline", command)
            self.assertIn("--tasks-file", command)
            self.assertIn(str(tasks_file.resolve()), command)
            self.assertIn("--only", command)
            self.assertIn("web-enabled-task", command)
            self.assertIn("--dry-run", command)
            self.assertNotIn("--codex-cmd", command)
            self.assertNotIn("--verbose", command)
            self.assertNotIn("--stream-codex-output", command)

    def test_pipeline_dry_run_action_rejects_missing_task(self) -> None:
        with TemporaryProject() as root:
            write_tasks_yaml(root)

            with self.assertRaises(ValueError):
                build_action_command(
                    "pipeline_dry_run",
                    root,
                    params={"task_id": "missing-task"},
                )

    def test_pipeline_dry_run_action_rejects_missing_tasks_yaml(self) -> None:
        with TemporaryProject() as root:
            with self.assertRaises(ValueError):
                build_action_command(
                    "pipeline_dry_run",
                    root,
                    params={"task_id": "web-enabled-task"},
                )

    def test_pipeline_dry_run_action_rejects_unsafe_task_id(self) -> None:
        with TemporaryProject() as root:
            write_tasks_yaml(root)

            with self.assertRaises(ValueError):
                build_action_command(
                    "pipeline_dry_run",
                    root,
                    params={"task_id": "..\\bad"},
                )

    def test_run_pipeline_real_action_builds_safe_argv(self) -> None:
        with TemporaryProject() as root:
            tasks_file = write_tasks_yaml(root)

            command = build_action_command(
                "run_pipeline_real",
                root,
                params={"task_id": "web-enabled-task", "codex_cmd": sys.executable},
            )

            self.assertIsInstance(command, list)
            self.assertIn("run-pipeline", command)
            self.assertIn("--tasks-file", command)
            self.assertIn(str(tasks_file.resolve()), command)
            self.assertIn("--only", command)
            self.assertIn("web-enabled-task", command)
            self.assertIn("--codex-cmd", command)
            self.assertIn(sys.executable, command)
            self.assertIn("--verbose", command)
            self.assertIn("--stream-codex-output", command)
            self.assertNotIn("--dry-run", command)

    def test_run_pipeline_real_action_rejects_missing_codex_cmd(self) -> None:
        with TemporaryProject() as root:
            write_tasks_yaml(root)

            with self.assertRaises(ValueError):
                build_action_command(
                    "run_pipeline_real",
                    root,
                    params={"task_id": "web-enabled-task"},
                )

    def test_run_pipeline_real_action_rejects_missing_task(self) -> None:
        with TemporaryProject() as root:
            write_tasks_yaml(root)

            with self.assertRaises(ValueError):
                build_action_command(
                    "run_pipeline_real",
                    root,
                    params={"task_id": "missing-task", "codex_cmd": sys.executable},
                )

    def test_run_pipeline_real_action_rejects_unsafe_task_id(self) -> None:
        with TemporaryProject() as root:
            write_tasks_yaml(root)

            with self.assertRaises(ValueError):
                build_action_command(
                    "run_pipeline_real",
                    root,
                    params={"task_id": "..\\bad", "codex_cmd": sys.executable},
                )

    def test_classify_run_action_builds_safe_argv(self) -> None:
        with TemporaryProject() as root:
            run_id, _run_dir = create_web_run(root)

            command = build_action_command(
                "classify_run",
                root,
                params={"run_id": run_id},
            )

            self.assertIsInstance(command, list)
            self.assertIn("classify-run", command)
            self.assertIn(run_id, command)
            self.assertIn("--runs-dir", command)
            self.assertIn(str((root / ".runs").resolve()), command)
            self.assertNotIn("--codex-cmd", command)
            self.assertNotIn("run-pipeline", command)
            self.assertNotIn("apply-run", command)
            self.assertNotIn("accept-run", command)

    def test_classify_run_action_rejects_missing_run(self) -> None:
        with TemporaryProject() as root:
            with self.assertRaises(ValueError):
                build_action_command(
                    "classify_run",
                    root,
                    params={"run_id": "missing-run"},
                )

    def test_classify_run_action_rejects_unsafe_run_id(self) -> None:
        with TemporaryProject() as root:
            create_web_run(root)

            with self.assertRaises(ValueError):
                build_action_command(
                    "classify_run",
                    root,
                    params={"run_id": "..\\bad"},
                )

    def test_run_review_checks_action_builds_safe_argv(self) -> None:
        with TemporaryProject() as root:
            run_id, _run_dir = create_web_run(root)

            command = build_action_command(
                "run_review_checks",
                root,
                params={"run_id": run_id},
            )

            self.assertIsInstance(command, list)
            self.assertIn("run-review-checks", command)
            self.assertIn(run_id, command)
            self.assertIn("--runs-dir", command)
            self.assertIn(str((root / ".runs").resolve()), command)
            self.assertNotIn("--codex-cmd", command)
            self.assertNotIn("run-pipeline", command)
            self.assertNotIn("apply-run", command)
            self.assertNotIn("accept-run", command)

    def test_run_review_checks_action_rejects_missing_run(self) -> None:
        with TemporaryProject() as root:
            with self.assertRaises(ValueError):
                build_action_command(
                    "run_review_checks",
                    root,
                    params={"run_id": "missing-run"},
                )

    def test_run_review_checks_action_rejects_unsafe_run_id(self) -> None:
        with TemporaryProject() as root:
            create_web_run(root)

            with self.assertRaises(ValueError):
                build_action_command(
                    "run_review_checks",
                    root,
                    params={"run_id": "..\\bad"},
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
