from __future__ import annotations

from contextlib import redirect_stdout
import hashlib
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


def review_findings_payload(run_id: str) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "run_id": run_id,
        "summary": "QA reviewer found one blocking issue.",
        "overall_decision": "needs_rework",
        "source_profile": "qa",
        "source_kind": "reviewer_profile",
        "findings": [
            {
                "id": "QA001",
                "reviewer": "qa",
                "category": "qa",
                "severity": "major",
                "title": "Missing regression test for changed behavior",
                "evidence": "The changed route behavior is not covered by a regression test.",
                "required_action": "Add a regression test for the changed route behavior.",
                "file": "tests/test_web_app.py",
                "line": 123,
                "status": "open",
            }
        ],
    }


def review_findings_json(run_id: str) -> str:
    return json.dumps(review_findings_payload(run_id), indent=2)


def write_review_findings_fixture(run_dir: Path, run_id: str) -> Path:
    findings_path = run_dir / "REVIEW_FINDINGS.json"
    findings_path.write_text(review_findings_json(run_id), encoding="utf-8")
    return findings_path


def review_arbitration_payload(
    run_id: str,
    *,
    source_sha256: str | None = None,
    finding_id: str = "QA001",
) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "run_id": run_id,
        "source_findings_path": "REVIEW_FINDINGS.json",
        "source_findings_sha256": source_sha256,
        "source_findings_updated_at": "2026-06-20T00:00:00+00:00",
        "arbiter": "human",
        "summary": "QA blocking finding upheld; rework is required before approval.",
        "overall_decision": "needs_rework",
        "arbitrated_findings": [
            {
                "finding_id": finding_id,
                "source_reviewer": "qa",
                "original_severity": "major",
                "final_severity": "major",
                "original_blocking": True,
                "final_blocking": True,
                "status": "upheld",
                "reason": "The missing regression test is a valid blocker.",
                "final_required_action": "Add a regression test that covers the changed behavior.",
                "human_escalation_required": False,
                "deterministic_hard_gate": False,
            }
        ],
    }


def review_arbitration_json(run_id: str) -> str:
    return json.dumps(review_arbitration_payload(run_id), indent=2)


def write_review_arbitration_fixture(
    run_dir: Path,
    run_id: str,
    *,
    source_sha256: str | None = None,
    finding_id: str = "QA001",
) -> Path:
    arbitration_path = run_dir / "REVIEW_ARBITRATION.json"
    arbitration_path.write_text(
        json.dumps(
            review_arbitration_payload(run_id, source_sha256=source_sha256, finding_id=finding_id),
            indent=2,
        ),
        encoding="utf-8",
    )
    return arbitration_path


def write_review_decision_fixture(run_dir: Path, run_id: str, *, decision: str = "approved") -> Path:
    decision_path = run_dir / "REVIEW_DECISION.json"
    decision_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "run_id": run_id,
                "decision": decision,
                "decided_at": "2026-06-20T00:00:00+00:00",
                "feedback_path": None,
                "feedback_excerpt": "",
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    state_path = run_dir / "state.json"
    state_payload = json.loads(state_path.read_text(encoding="utf-8"))
    state_payload["human_review_decision"] = decision
    state_payload["human_review_decision_path"] = str(decision_path.resolve())
    state_path.write_text(json.dumps(state_payload, indent=2), encoding="utf-8")
    return decision_path


def write_apply_report_fixture(run_dir: Path, run_id: str) -> tuple[Path, Path]:
    json_path = run_dir / "APPLY_REPORT.json"
    markdown_path = run_dir / "APPLY_REPORT.md"
    json_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "run_id": run_id,
                "status": "applied",
                "applied_at": "2026-06-20T00:00:00+00:00",
                "target_workspace": str((run_dir.parent.parent / "target_repo").resolve()),
                "review_gate": "human_approved",
                "applied_files": ["docs/applied.md"],
                "deleted_files": ["docs/deleted.md"],
                "skipped_files": ["EXECUTION_REPORT.json"],
                "commit_created": False,
                "git_add_performed": False,
                "next_step": "Inspect git diff, run tests, then commit manually.",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(
        "# Apply Report\n\nStatus: `applied`\n\nTarget workspace: `target_repo`\n",
        encoding="utf-8",
    )
    return json_path, markdown_path


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_reviewer_prompt_fixture(run_dir: Path, run_id: str, profiles: tuple[str, ...]) -> Path:
    prompts_dir = run_dir / "reviewer_prompts"
    prompts_dir.mkdir(parents=True, exist_ok=True)
    prompt_entries = []
    for profile in profiles:
        prompt_path = prompts_dir / f"{profile}_review_prompt.md"
        prompt_path.write_text(f"# {profile} prompt\n\nPrompt content for {profile}.\n", encoding="utf-8")
        prompt_entries.append({"profile": profile, "path": str(prompt_path.resolve())})
    (prompts_dir / "MANIFEST.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "run_id": run_id,
                "profiles": list(profiles),
                "prompts": prompt_entries,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return prompts_dir


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
            self.assertIn("Prepare review usually comes after classification and review checks.", response.text)
            self.assertIn("Prepare review", response.text)
            self.assertIn("reviewer prompt packets", response.text)
            self.assertIn(f'action="/runs/{run_id}/prepare-review"', response.text)

    def test_run_detail_links_to_reviewer_prompt_viewer(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, _run_dir = create_web_run(root)
            client = TestClient(create_app(root))

            response = client.get(f"/runs/{run_id}")

            self.assertEqual(response.status_code, 200)
            self.assertIn("View reviewer prompts", response.text)
            self.assertIn(f'href="/runs/{run_id}/reviewer-prompts"', response.text)

    def test_reviewer_prompts_index_empty_state_is_read_only(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, run_dir = create_web_run(root)
            before_state = (run_dir / "state.json").read_text(encoding="utf-8")
            client = TestClient(create_app(root))

            response = client.get(f"/runs/{run_id}/reviewer-prompts")

            self.assertEqual(response.status_code, 200)
            self.assertIn("No reviewer prompt packets found for this run. Run Prepare review first.", response.text)
            self.assertIn("does not run reviewer agents", response.text)
            self.assertFalse((root / ".web" / "jobs").exists())
            self.assertEqual((run_dir / "state.json").read_text(encoding="utf-8"), before_state)
            for blocked in ("record-findings", "review-run", "apply-run", "accept-run", "run-pipeline"):
                self.assertNotIn(blocked, response.text)

    def test_reviewer_prompts_index_reads_manifest_and_lists_actual_files(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, run_dir = create_web_run(root)
            write_reviewer_prompt_fixture(run_dir, run_id, ("security", "architecture", "qa"))
            client = TestClient(create_app(root))

            response = client.get(f"/runs/{run_id}/reviewer-prompts")

            self.assertEqual(response.status_code, 200)
            self.assertIn("schema_version", response.text)
            self.assertIn("1.0", response.text)
            self.assertIn("security_review_prompt.md", response.text)
            self.assertIn("architecture_review_prompt.md", response.text)
            self.assertIn("qa_review_prompt.md", response.text)
            self.assertNotIn("security" + ".md", response.text)
            self.assertNotIn("architecture" + ".md", response.text)
            self.assertNotIn("qa" + ".md", response.text)
            self.assertIn(f'href="/runs/{run_id}/reviewer-prompts/security"', response.text)
            self.assertIn(f'href="/runs/{run_id}/reviewer-prompts/architecture"', response.text)
            self.assertIn(f'href="/runs/{run_id}/reviewer-prompts/qa"', response.text)

    def test_reviewer_prompt_detail_opens_manifest_backed_file(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, run_dir = create_web_run(root)
            write_reviewer_prompt_fixture(run_dir, run_id, ("security",))
            client = TestClient(create_app(root))

            response = client.get(f"/runs/{run_id}/reviewer-prompts/security")

            self.assertEqual(response.status_code, 200)
            self.assertIn("security_review_prompt.md", response.text)
            self.assertIn("Prompt content for security.", response.text)
            self.assertIn("External reviewers or humans can copy this prompt.", response.text)
            for blocked in ("record-findings", "review-run", "apply-run", "accept-run", "run-pipeline"):
                self.assertNotIn(blocked, response.text)

    def test_reviewer_prompt_fallback_works_without_manifest(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, run_dir = create_web_run(root)
            prompts_dir = run_dir / "reviewer_prompts"
            prompts_dir.mkdir(parents=True)
            (prompts_dir / "qa_review_prompt.md").write_text("# QA fallback\n\nFallback prompt.\n", encoding="utf-8")
            client = TestClient(create_app(root))

            index = client.get(f"/runs/{run_id}/reviewer-prompts")
            detail = client.get(f"/runs/{run_id}/reviewer-prompts/qa")

            self.assertEqual(index.status_code, 200)
            self.assertIn("qa_review_prompt.md", index.text)
            self.assertIn("fallback", index.text)
            self.assertEqual(detail.status_code, 200)
            self.assertIn("Fallback prompt.", detail.text)
            self.assertIn("source</dt><dd>fallback", detail.text)

    def test_reviewer_prompt_incomplete_manifest_adds_fallback_files(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, run_dir = create_web_run(root)
            prompts_dir = write_reviewer_prompt_fixture(run_dir, run_id, ("security",))
            (prompts_dir / "qa_review_prompt.md").write_text("# QA fallback\n\nExtra prompt.\n", encoding="utf-8")
            client = TestClient(create_app(root))

            response = client.get(f"/runs/{run_id}/reviewer-prompts")

            self.assertEqual(response.status_code, 200)
            self.assertIn("security_review_prompt.md", response.text)
            self.assertIn("qa_review_prompt.md", response.text)
            self.assertIn(f'href="/runs/{run_id}/reviewer-prompts/qa"', response.text)

    def test_reviewer_prompt_routes_reject_unsafe_run_id(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            client = TestClient(create_app(root))

            index = client.get("/runs/bad%5Cname/reviewer-prompts")
            detail = client.get("/runs/bad%5Cname/reviewer-prompts/qa")

            self.assertIn(index.status_code, {400, 404})
            self.assertIn(detail.status_code, {400, 404})

    def test_reviewer_prompt_detail_rejects_unsafe_profile(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, run_dir = create_web_run(root)
            write_reviewer_prompt_fixture(run_dir, run_id, ("qa",))
            client = TestClient(create_app(root))

            path_traversal = client.get(f"/runs/{run_id}/reviewer-prompts/..%2Fsecret")
            bad_name = client.get(f"/runs/{run_id}/reviewer-prompts/bad%5Cname")

            self.assertIn(path_traversal.status_code, {400, 404})
            self.assertIn(bad_name.status_code, {400, 404})

    def test_reviewer_prompt_unknown_profile_returns_not_found(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, run_dir = create_web_run(root)
            write_reviewer_prompt_fixture(run_dir, run_id, ("qa",))
            client = TestClient(create_app(root))

            response = client.get(f"/runs/{run_id}/reviewer-prompts/security")

            self.assertEqual(response.status_code, 404)

    def test_reviewer_prompt_manifest_path_outside_prompts_dir_is_not_read(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, run_dir = create_web_run(root)
            prompts_dir = run_dir / "reviewer_prompts"
            prompts_dir.mkdir(parents=True)
            outside_file = root / "outside_secret.md"
            outside_file.write_text("outside file secret should not render", encoding="utf-8")
            (prompts_dir / "MANIFEST.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "run_id": run_id,
                        "profiles": ["security"],
                        "prompts": [{"profile": "security", "path": str(outside_file.resolve())}],
                    }
                ),
                encoding="utf-8",
            )
            client = TestClient(create_app(root))

            index = client.get(f"/runs/{run_id}/reviewer-prompts")
            detail = client.get(f"/runs/{run_id}/reviewer-prompts/security")

            self.assertEqual(index.status_code, 200)
            self.assertIn("ignored unsafe manifest path for profile: security", index.text)
            self.assertNotIn("outside file secret should not render", index.text)
            self.assertNotIn(f'href="/runs/{run_id}/reviewer-prompts/security"', index.text)
            self.assertEqual(detail.status_code, 404)
            self.assertNotIn("outside file secret should not render", detail.text)

    def test_reviewer_prompt_manifest_contained_non_prompt_file_is_not_read(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, run_dir = create_web_run(root)
            prompts_dir = run_dir / "reviewer_prompts"
            prompts_dir.mkdir(parents=True)
            non_prompt = prompts_dir / "not_a_prompt.txt"
            non_prompt.write_text("contained non-prompt secret should not render", encoding="utf-8")
            (prompts_dir / "MANIFEST.json").write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "run_id": run_id,
                        "profiles": ["security"],
                        "prompts": [{"profile": "security", "path": str(non_prompt.resolve())}],
                    }
                ),
                encoding="utf-8",
            )
            client = TestClient(create_app(root))

            index = client.get(f"/runs/{run_id}/reviewer-prompts")
            detail = client.get(f"/runs/{run_id}/reviewer-prompts/security")

            self.assertEqual(index.status_code, 200)
            self.assertIn("ignored non-prompt manifest path for profile: security", index.text)
            self.assertIn("not_a_prompt.txt", index.text)
            self.assertNotIn(f'href="/runs/{run_id}/reviewer-prompts/security"', index.text)
            self.assertNotIn("contained non-prompt secret should not render", index.text)
            self.assertEqual(detail.status_code, 404)
            self.assertNotIn("contained non-prompt secret should not render", detail.text)

    def test_findings_index_empty_state_includes_cli_helper_and_is_read_only(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, run_dir = create_web_run(root)
            before_state = (run_dir / "state.json").read_text(encoding="utf-8")
            client = TestClient(create_app(root))

            response = client.get(f"/runs/{run_id}/findings")

            self.assertEqual(response.status_code, 200)
            self.assertIn("No review findings recorded", response.text)
            self.assertIn("record-findings", response.text)
            self.assertIn("--findings-file", response.text)
            self.assertNotIn("<form", response.text.lower())
            self.assertNotIn('method="post"', response.text.lower())
            self.assertFalse((root / ".web" / "jobs").exists())
            self.assertEqual((run_dir / "state.json").read_text(encoding="utf-8"), before_state)

    def test_findings_index_shows_existing_findings_summary_and_links(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, run_dir = create_web_run(root)
            write_review_findings_fixture(run_dir, run_id)
            client = TestClient(create_app(root))

            response = client.get(f"/runs/{run_id}/findings")

            self.assertEqual(response.status_code, 200)
            self.assertIn("overall_decision", response.text)
            self.assertIn("needs_rework", response.text)
            self.assertIn("source_profile", response.text)
            self.assertIn("qa", response.text)
            self.assertIn("blocking_open", response.text)
            self.assertIn("<dd>1</dd>", response.text)
            self.assertIn("QA001", response.text)
            self.assertIn("Missing regression test", response.text)
            self.assertIn(f'href="/runs/{run_id}/findings/QA001"', response.text)

    def test_findings_index_shows_markdown_artifact_status(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, run_dir = create_web_run(root)
            write_review_findings_fixture(run_dir, run_id)
            (run_dir / "REVIEW_FINDINGS.md").write_text("# Review Findings\n", encoding="utf-8")
            client = TestClient(create_app(root))

            response = client.get(f"/runs/{run_id}/findings")

            self.assertEqual(response.status_code, 200)
            self.assertIn("REVIEW_FINDINGS.md", response.text)
            self.assertIn(str(run_dir / "REVIEW_FINDINGS.md"), response.text)

    def test_findings_index_invalid_json_returns_friendly_error(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, run_dir = create_web_run(root)
            (run_dir / "REVIEW_FINDINGS.json").write_text("{invalid json", encoding="utf-8")
            client = TestClient(create_app(root))

            response = client.get(f"/runs/{run_id}/findings")

            self.assertEqual(response.status_code, 200)
            self.assertIn("could not be parsed safely", response.text)
            self.assertIn("REVIEW_FINDINGS.json could not be loaded", response.text)

    def test_finding_detail_shows_existing_finding_fields(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, run_dir = create_web_run(root)
            write_review_findings_fixture(run_dir, run_id)
            client = TestClient(create_app(root))

            response = client.get(f"/runs/{run_id}/findings/QA001")

            self.assertEqual(response.status_code, 200)
            self.assertIn("reviewer</dt><dd>qa", response.text)
            self.assertIn("severity</dt><dd>major", response.text)
            self.assertIn("category</dt><dd>qa", response.text)
            self.assertIn("blocking</dt><dd>true", response.text)
            self.assertIn("The changed route behavior is not covered", response.text)
            self.assertIn("Add a regression test", response.text)
            self.assertIn("tests/test_web_app.py", response.text)

    def test_finding_detail_unknown_id_returns_not_found(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, run_dir = create_web_run(root)
            write_review_findings_fixture(run_dir, run_id)
            client = TestClient(create_app(root))

            response = client.get(f"/runs/{run_id}/findings/MISSING")

            self.assertEqual(response.status_code, 404)

    def test_finding_detail_rejects_unsafe_finding_id(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, run_dir = create_web_run(root)
            write_review_findings_fixture(run_dir, run_id)
            client = TestClient(create_app(root))

            path_traversal = client.get(f"/runs/{run_id}/findings/..%2Fsecret")
            bad_name = client.get(f"/runs/{run_id}/findings/bad%5Cname")

            self.assertIn(path_traversal.status_code, {400, 404})
            self.assertIn(bad_name.status_code, {400, 404})

    def test_findings_routes_reject_unsafe_run_id(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            client = TestClient(create_app(root))

            index = client.get("/runs/bad%5Cname/findings")
            detail = client.get("/runs/bad%5Cname/findings/QA001")

            self.assertIn(index.status_code, {400, 404})
            self.assertIn(detail.status_code, {400, 404})

    def test_findings_pages_do_not_modify_state_or_findings_json(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, run_dir = create_web_run(root)
            findings_path = write_review_findings_fixture(run_dir, run_id)
            before_state = (run_dir / "state.json").read_text(encoding="utf-8")
            before_findings = findings_path.read_text(encoding="utf-8")
            client = TestClient(create_app(root))

            index = client.get(f"/runs/{run_id}/findings")
            detail = client.get(f"/runs/{run_id}/findings/QA001")

            self.assertEqual(index.status_code, 200)
            self.assertEqual(detail.status_code, 200)
            self.assertFalse((root / ".web" / "jobs").exists())
            self.assertEqual((run_dir / "state.json").read_text(encoding="utf-8"), before_state)
            self.assertEqual(findings_path.read_text(encoding="utf-8"), before_findings)

    def test_findings_pages_have_no_write_forms_or_dangerous_actions(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, run_dir = create_web_run(root)
            write_review_findings_fixture(run_dir, run_id)
            client = TestClient(create_app(root))

            pages = [
                client.get(f"/runs/{run_id}/findings"),
                client.get(f"/runs/{run_id}/findings/QA001"),
            ]

            for response in pages:
                self.assertEqual(response.status_code, 200)
                self.assertNotIn("<form", response.text.lower())
                self.assertNotIn('method="post"', response.text.lower())
                self.assertNotIn("record-arbitration", response.text)
                self.assertNotIn("review-run", response.text)
                self.assertNotIn("apply-run", response.text)
                self.assertNotIn("accept-run", response.text)
                self.assertNotIn("run-pipeline", response.text)

    def test_run_detail_links_to_findings_viewer(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, _run_dir = create_web_run(root)
            client = TestClient(create_app(root))

            response = client.get(f"/runs/{run_id}")

            self.assertEqual(response.status_code, 200)
            self.assertIn("View findings", response.text)
            self.assertIn(f'href="/runs/{run_id}/findings"', response.text)

    def test_new_findings_form_returns_200_for_run_without_findings(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, _run_dir = create_web_run(root)
            client = TestClient(create_app(root))

            response = client.get(f"/runs/{run_id}/findings/new")

            self.assertEqual(response.status_code, 200)
            self.assertIn("Record Findings JSON", response.text)
            self.assertIn("textarea", response.text)
            self.assertIn('name="findings_json"', response.text)
            self.assertIn("record-findings", response.text)
            self.assertIn('name="confirm_record_findings"', response.text)
            self.assertIn("does not run reviewer agents", response.text)
            self.assertIn("approve", response.text)
            self.assertIn("apply", response.text)

    def test_new_findings_form_warns_when_findings_exist(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, run_dir = create_web_run(root)
            write_review_findings_fixture(run_dir, run_id)
            client = TestClient(create_app(root))

            response = client.get(f"/runs/{run_id}/findings/new")

            self.assertEqual(response.status_code, 200)
            self.assertIn("Review findings already exist for this run", response.text)
            self.assertIn("does not overwrite findings yet", response.text)
            self.assertIn("disabled", response.text)

    def test_record_findings_without_confirmation_returns_error_without_job(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, run_dir = create_web_run(root)
            client = TestClient(create_app(root))

            response = client.post(
                f"/runs/{run_id}/findings/record",
                data={"findings_json": review_findings_json(run_id), "profile": "qa"},
            )

            self.assertEqual(response.status_code, 400)
            self.assertIn("Explicit record findings confirmation is required.", response.text)
            self.assertFalse((root / ".web" / "jobs").exists())
            self.assertFalse((run_dir / "REVIEW_FINDINGS.json").exists())

    def test_record_findings_invalid_json_returns_error_without_job(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, run_dir = create_web_run(root)
            client = TestClient(create_app(root))

            response = client.post(
                f"/runs/{run_id}/findings/record",
                data={
                    "findings_json": "{invalid json",
                    "confirm_record_findings": "yes",
                },
            )

            self.assertEqual(response.status_code, 400)
            self.assertIn("Findings JSON did not match ReviewFindingsReport", response.text)
            self.assertFalse((root / ".web" / "jobs").exists())
            self.assertFalse((run_dir / "REVIEW_FINDINGS.json").exists())

    def test_record_findings_schema_invalid_json_returns_error_without_job(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, run_dir = create_web_run(root)
            payload = review_findings_payload(run_id)
            payload["overall_decision"] = "pass"
            client = TestClient(create_app(root))

            response = client.post(
                f"/runs/{run_id}/findings/record",
                data={
                    "findings_json": json.dumps(payload),
                    "confirm_record_findings": "yes",
                },
            )

            self.assertEqual(response.status_code, 400)
            self.assertIn("Findings JSON did not match ReviewFindingsReport", response.text)
            self.assertFalse((root / ".web" / "jobs").exists())
            self.assertFalse((run_dir / "REVIEW_FINDINGS.json").exists())

    def test_record_findings_run_id_mismatch_returns_error_without_job(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, run_dir = create_web_run(root)
            client = TestClient(create_app(root))

            response = client.post(
                f"/runs/{run_id}/findings/record",
                data={
                    "findings_json": review_findings_json("other-run"),
                    "confirm_record_findings": "yes",
                },
            )

            self.assertEqual(response.status_code, 400)
            self.assertIn("Findings report run_id must match this run", response.text)
            self.assertFalse((root / ".web" / "jobs").exists())
            self.assertFalse((run_dir / "REVIEW_FINDINGS.json").exists())

    def test_record_findings_rejects_unsafe_profile_without_job(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, run_dir = create_web_run(root)
            client = TestClient(create_app(root))

            response = client.post(
                f"/runs/{run_id}/findings/record",
                data={
                    "findings_json": review_findings_json(run_id),
                    "profile": "..\\bad",
                    "confirm_record_findings": "yes",
                },
            )

            self.assertEqual(response.status_code, 400)
            self.assertIn("Profile may contain only", response.text)
            self.assertFalse((root / ".web" / "jobs").exists())
            self.assertFalse((run_dir / "REVIEW_FINDINGS.json").exists())

    def test_record_findings_refuses_existing_findings_without_job(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, run_dir = create_web_run(root)
            before_findings = write_review_findings_fixture(run_dir, run_id).read_text(encoding="utf-8")
            client = TestClient(create_app(root))

            response = client.post(
                f"/runs/{run_id}/findings/record",
                data={
                    "findings_json": review_findings_json(run_id),
                    "confirm_record_findings": "yes",
                },
            )

            self.assertEqual(response.status_code, 400)
            self.assertIn("does not overwrite findings yet", response.text)
            self.assertFalse((root / ".web" / "jobs").exists())
            self.assertEqual((run_dir / "REVIEW_FINDINGS.json").read_text(encoding="utf-8"), before_findings)

    def test_record_findings_valid_json_creates_hidden_job_with_generated_input(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, run_dir = create_web_run(root)
            client = TestClient(create_app(root))

            with patch(
                "ai_orchestrator_web.routes.runs.start_background_job",
                side_effect=create_job_without_starting_subprocess,
            ) as start_job:
                response = client.post(
                    f"/runs/{run_id}/findings/record",
                    data={
                        "findings_json": review_findings_json(run_id),
                        "profile": "qa",
                        "confirm_record_findings": "yes",
                    },
                    follow_redirects=False,
                )

            self.assertEqual(response.status_code, 303)
            start_job.assert_called_once()
            location = response.headers["location"]
            job_id = location.rsplit("/", 1)[-1]
            job = load_job(root, job_id)
            self.assertEqual(job.action, "record_findings")
            self.assertIn("record-findings", job.command)
            self.assertIn("--findings-file", job.command)
            self.assertIn("--profile", job.command)
            self.assertIn("qa", job.command)
            self.assertNotIn("--force", job.command)
            self.assertEqual(job.result_refs["findings_url"], f"/runs/{run_id}/findings")
            input_paths = list((root / ".web" / "findings_inputs").glob("*.json"))
            self.assertEqual(len(input_paths), 1)
            self.assertIn(str(input_paths[0].resolve()), job.command)
            normalized_payload = json.loads(input_paths[0].read_text(encoding="utf-8"))
            self.assertEqual(normalized_payload["run_id"], run_id)
            self.assertEqual(normalized_payload["counts"]["blocking_open"], 1)
            self.assertFalse((run_dir / "REVIEW_FINDINGS.json").exists())

            detail = client.get(location)

            self.assertEqual(detail.status_code, 200)
            self.assertIn(f'href="/runs/{run_id}/findings"', detail.text)

    def test_record_findings_reviewer_profile_json_auto_passes_profile_to_job(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, _run_dir = create_web_run(root)
            client = TestClient(create_app(root))

            with patch(
                "ai_orchestrator_web.routes.runs.start_background_job",
                side_effect=create_job_without_starting_subprocess,
            ):
                response = client.post(
                    f"/runs/{run_id}/findings/record",
                    data={
                        "findings_json": review_findings_json(run_id),
                        "confirm_record_findings": "yes",
                    },
                    follow_redirects=False,
                )

            self.assertEqual(response.status_code, 303)
            job_id = response.headers["location"].rsplit("/", 1)[-1]
            job = load_job(root, job_id)
            self.assertEqual(job.action, "record_findings")
            self.assertIn("--profile", job.command)
            self.assertIn("qa", job.command)
            self.assertNotIn("--force", job.command)

    def test_record_findings_rejects_unsafe_reviewer_profile_from_json_without_job(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, run_dir = create_web_run(root)
            payload = review_findings_payload(run_id)
            payload["source_profile"] = "../qa"
            client = TestClient(create_app(root))

            response = client.post(
                f"/runs/{run_id}/findings/record",
                data={
                    "findings_json": json.dumps(payload),
                    "confirm_record_findings": "yes",
                },
            )

            self.assertEqual(response.status_code, 400)
            self.assertIn("Findings report source_profile is not a safe reviewer profile id.", response.text)
            self.assertFalse((root / ".web" / "jobs").exists())
            self.assertFalse((run_dir / "REVIEW_FINDINGS.json").exists())

    def test_record_findings_action_builds_safe_argv(self) -> None:
        with TemporaryProject() as root:
            run_id, _run_dir = create_web_run(root)
            inputs_dir = root / ".web" / "findings_inputs"
            input_path = inputs_dir / "safe_findings.json"
            write_text(input_path, review_findings_json(run_id))

            command = build_action_command(
                "record_findings",
                root,
                params={"run_id": run_id, "findings_input_id": input_path.name, "profile": "qa"},
            )

            self.assertIsInstance(command, list)
            self.assertIn("record-findings", command)
            self.assertIn(run_id, command)
            self.assertIn("--runs-dir", command)
            self.assertIn(str((root / ".runs").resolve()), command)
            self.assertIn("--findings-file", command)
            self.assertIn(str(input_path.resolve()), command)
            self.assertIn("--profile", command)
            self.assertIn("qa", command)
            self.assertNotIn("--force", command)
            self.assertNotIn("apply-run", command)
            self.assertNotIn("accept-run", command)

    def test_record_findings_action_rejects_unsafe_or_missing_inputs(self) -> None:
        with TemporaryProject() as root:
            run_id, _run_dir = create_web_run(root)
            inputs_dir = root / ".web" / "findings_inputs"
            input_path = inputs_dir / "safe_findings.json"
            write_text(input_path, review_findings_json(run_id))

            with self.assertRaises(ValueError):
                build_action_command(
                    "record_findings",
                    root,
                    params={"run_id": "..\\bad", "findings_input_id": input_path.name},
                )
            with self.assertRaises(ValueError):
                build_action_command(
                    "record_findings",
                    root,
                    params={"run_id": run_id, "findings_input_id": "missing.json"},
                )
            with self.assertRaises(ValueError):
                build_action_command(
                    "record_findings",
                    root,
                    params={"run_id": run_id, "findings_input_id": "..\\outside.json"},
                )

    def test_jobs_selector_does_not_expose_record_findings(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            client = TestClient(create_app(root))

            response = client.get("/jobs")

            self.assertEqual(response.status_code, 200)
            self.assertNotIn("record_findings", response.text)

    def test_findings_pages_link_to_record_findings_form(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, _run_dir = create_web_run(root)
            client = TestClient(create_app(root))

            findings = client.get(f"/runs/{run_id}/findings")
            detail = client.get(f"/runs/{run_id}")

            self.assertEqual(findings.status_code, 200)
            self.assertEqual(detail.status_code, 200)
            self.assertIn(f'href="/runs/{run_id}/findings/new"', findings.text)
            self.assertIn(f'href="/runs/{run_id}/findings/new"', detail.text)

    def test_arbitration_index_empty_without_findings_shows_helper(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, run_dir = create_web_run(root)
            before_state = (run_dir / "state.json").read_text(encoding="utf-8")
            client = TestClient(create_app(root))

            response = client.get(f"/runs/{run_id}/arbitration")

            self.assertEqual(response.status_code, 200)
            self.assertIn("Arbitration requires REVIEW_FINDINGS.json", response.text)
            self.assertIn("record-arbitration", response.text)
            self.assertIn("read-only", response.text)
            self.assertNotIn("<form", response.text.lower())
            self.assertFalse((root / ".web" / "jobs").exists())
            self.assertEqual((run_dir / "state.json").read_text(encoding="utf-8"), before_state)

    def test_arbitration_index_shows_findings_when_arbitration_missing(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, run_dir = create_web_run(root)
            write_review_findings_fixture(run_dir, run_id)
            client = TestClient(create_app(root))

            response = client.get(f"/runs/{run_id}/arbitration")

            self.assertEqual(response.status_code, 200)
            self.assertIn("No review arbitration recorded yet.", response.text)
            self.assertIn("Open Blocking Findings", response.text)
            self.assertIn("QA001", response.text)
            self.assertIn("Missing regression test", response.text)

    def test_arbitration_index_shows_existing_arbitration_summary(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, run_dir = create_web_run(root)
            findings_path = write_review_findings_fixture(run_dir, run_id)
            source_sha = sha256_file(findings_path)
            write_review_arbitration_fixture(run_dir, run_id, source_sha256=source_sha)
            client = TestClient(create_app(root))

            response = client.get(f"/runs/{run_id}/arbitration")

            self.assertEqual(response.status_code, 200)
            self.assertIn("overall_decision", response.text)
            self.assertIn("needs_rework", response.text)
            self.assertIn("arbiter", response.text)
            self.assertIn("human", response.text)
            self.assertIn("final_blocking", response.text)
            self.assertIn("human_escalation_required", response.text)
            self.assertIn("source_findings_sha256", response.text)
            self.assertIn(source_sha, response.text)
            self.assertIn("QA001", response.text)
            self.assertIn(f'href="/runs/{run_id}/arbitration/QA001"', response.text)

    def test_arbitration_index_shows_markdown_artifact_status(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, run_dir = create_web_run(root)
            findings_path = write_review_findings_fixture(run_dir, run_id)
            write_review_arbitration_fixture(run_dir, run_id, source_sha256=sha256_file(findings_path))
            (run_dir / "REVIEW_ARBITRATION.md").write_text("# Review Arbitration\n", encoding="utf-8")
            client = TestClient(create_app(root))

            response = client.get(f"/runs/{run_id}/arbitration")

            self.assertEqual(response.status_code, 200)
            self.assertIn("REVIEW_ARBITRATION.md", response.text)
            self.assertIn(str(run_dir / "REVIEW_ARBITRATION.md"), response.text)

    def test_arbitration_index_shows_stale_warning(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, run_dir = create_web_run(root)
            write_review_findings_fixture(run_dir, run_id)
            write_review_arbitration_fixture(run_dir, run_id, source_sha256="badsha")
            client = TestClient(create_app(root))

            response = client.get(f"/runs/{run_id}/arbitration")

            self.assertEqual(response.status_code, 200)
            self.assertIn("Review arbitration is stale", response.text)
            self.assertIn("Do not rely on this arbitration", response.text)

    def test_arbitration_index_invalid_json_returns_friendly_error(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, run_dir = create_web_run(root)
            write_review_findings_fixture(run_dir, run_id)
            (run_dir / "REVIEW_ARBITRATION.json").write_text("{invalid json", encoding="utf-8")
            client = TestClient(create_app(root))

            response = client.get(f"/runs/{run_id}/arbitration")

            self.assertEqual(response.status_code, 200)
            self.assertIn("could not be parsed safely", response.text)
            self.assertIn("REVIEW_ARBITRATION.json could not be loaded", response.text)

    def test_arbitration_index_shows_missing_open_blocking_coverage(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, run_dir = create_web_run(root)
            payload = review_findings_payload(run_id)
            payload["findings"].append(
                {
                    "id": "QA002",
                    "reviewer": "qa",
                    "category": "qa",
                    "severity": "major",
                    "title": "Second blocker",
                    "evidence": "Evidence.",
                    "required_action": "Fix second blocker.",
                    "status": "open",
                }
            )
            findings_path = run_dir / "REVIEW_FINDINGS.json"
            findings_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            write_review_arbitration_fixture(run_dir, run_id, source_sha256=sha256_file(findings_path), finding_id="QA001")
            client = TestClient(create_app(root))

            response = client.get(f"/runs/{run_id}/arbitration")

            self.assertEqual(response.status_code, 200)
            self.assertIn("Missing arbitration coverage for open blocking findings: QA002", response.text)

    def test_arbitration_detail_shows_arbitrated_finding_fields(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, run_dir = create_web_run(root)
            findings_path = write_review_findings_fixture(run_dir, run_id)
            write_review_arbitration_fixture(run_dir, run_id, source_sha256=sha256_file(findings_path))
            client = TestClient(create_app(root))

            response = client.get(f"/runs/{run_id}/arbitration/QA001")

            self.assertEqual(response.status_code, 200)
            self.assertIn("source_reviewer</dt><dd>qa", response.text)
            self.assertIn("original_severity</dt><dd>major", response.text)
            self.assertIn("final_severity</dt><dd>major", response.text)
            self.assertIn("original_blocking</dt><dd>true", response.text)
            self.assertIn("final_blocking</dt><dd>true", response.text)
            self.assertIn("status</dt><dd>upheld", response.text)
            self.assertIn("The missing regression test is a valid blocker.", response.text)
            self.assertIn("Add a regression test", response.text)
            self.assertIn("human_escalation_required</dt><dd>false", response.text)
            self.assertIn("deterministic_hard_gate</dt><dd>false", response.text)

    def test_arbitration_detail_unknown_id_returns_not_found(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, run_dir = create_web_run(root)
            findings_path = write_review_findings_fixture(run_dir, run_id)
            write_review_arbitration_fixture(run_dir, run_id, source_sha256=sha256_file(findings_path))
            client = TestClient(create_app(root))

            response = client.get(f"/runs/{run_id}/arbitration/MISSING")

            self.assertEqual(response.status_code, 404)

    def test_arbitration_detail_rejects_unsafe_finding_id(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, run_dir = create_web_run(root)
            findings_path = write_review_findings_fixture(run_dir, run_id)
            write_review_arbitration_fixture(run_dir, run_id, source_sha256=sha256_file(findings_path))
            client = TestClient(create_app(root))

            path_traversal = client.get(f"/runs/{run_id}/arbitration/..%2Fsecret")
            bad_name = client.get(f"/runs/{run_id}/arbitration/bad%5Cname")

            self.assertIn(path_traversal.status_code, {400, 404})
            self.assertIn(bad_name.status_code, {400, 404})

    def test_arbitration_routes_reject_unsafe_run_id(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            client = TestClient(create_app(root))

            index = client.get("/runs/bad%5Cname/arbitration")
            detail = client.get("/runs/bad%5Cname/arbitration/QA001")

            self.assertIn(index.status_code, {400, 404})
            self.assertIn(detail.status_code, {400, 404})

    def test_arbitration_pages_do_not_modify_artifacts_or_create_jobs(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, run_dir = create_web_run(root)
            findings_path = write_review_findings_fixture(run_dir, run_id)
            arbitration_path = write_review_arbitration_fixture(
                run_dir,
                run_id,
                source_sha256=sha256_file(findings_path),
            )
            before_state = (run_dir / "state.json").read_text(encoding="utf-8")
            before_findings = findings_path.read_text(encoding="utf-8")
            before_arbitration = arbitration_path.read_text(encoding="utf-8")
            client = TestClient(create_app(root))

            index = client.get(f"/runs/{run_id}/arbitration")
            detail = client.get(f"/runs/{run_id}/arbitration/QA001")

            self.assertEqual(index.status_code, 200)
            self.assertEqual(detail.status_code, 200)
            self.assertFalse((root / ".web" / "jobs").exists())
            self.assertEqual((run_dir / "state.json").read_text(encoding="utf-8"), before_state)
            self.assertEqual(findings_path.read_text(encoding="utf-8"), before_findings)
            self.assertEqual(arbitration_path.read_text(encoding="utf-8"), before_arbitration)

    def test_arbitration_pages_have_no_write_forms_or_dangerous_actions(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, run_dir = create_web_run(root)
            findings_path = write_review_findings_fixture(run_dir, run_id)
            write_review_arbitration_fixture(run_dir, run_id, source_sha256=sha256_file(findings_path))
            client = TestClient(create_app(root))

            pages = [
                client.get(f"/runs/{run_id}/arbitration"),
                client.get(f"/runs/{run_id}/arbitration/QA001"),
            ]

            for response in pages:
                self.assertEqual(response.status_code, 200)
                self.assertNotIn("<form", response.text.lower())
                self.assertNotIn('method="post"', response.text.lower())
                self.assertNotIn("review-run", response.text)
                self.assertNotIn("apply-run", response.text)
                self.assertNotIn("accept-run", response.text)
                self.assertNotIn("run-pipeline", response.text)

    def test_run_detail_links_to_arbitration_viewer(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, _run_dir = create_web_run(root)
            client = TestClient(create_app(root))

            response = client.get(f"/runs/{run_id}")

            self.assertEqual(response.status_code, 200)
            self.assertIn("View arbitration", response.text)
            self.assertIn(f'href="/runs/{run_id}/arbitration"', response.text)

    def test_new_arbitration_form_returns_200_for_run_with_findings(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, run_dir = create_web_run(root)
            write_review_findings_fixture(run_dir, run_id)
            client = TestClient(create_app(root))

            response = client.get(f"/runs/{run_id}/arbitration/new")

            self.assertEqual(response.status_code, 200)
            self.assertIn("Record Arbitration JSON", response.text)
            self.assertIn("textarea", response.text)
            self.assertIn('name="arbitration_json"', response.text)
            self.assertIn("record-arbitration", response.text)
            self.assertIn('name="confirm_record_arbitration"', response.text)
            self.assertIn("does not approve", response.text)
            self.assertIn("apply", response.text)
            self.assertIn("commit", response.text)
            self.assertFalse((root / ".web" / "jobs").exists())

    def test_new_arbitration_form_requires_findings(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, _run_dir = create_web_run(root)
            client = TestClient(create_app(root))

            response = client.get(f"/runs/{run_id}/arbitration/new")

            self.assertEqual(response.status_code, 200)
            self.assertIn("Review findings are required before arbitration can be recorded", response.text)
            self.assertIn("disabled", response.text)
            self.assertFalse((root / ".web" / "jobs").exists())

    def test_new_arbitration_form_warns_when_arbitration_exists(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, run_dir = create_web_run(root)
            findings_path = write_review_findings_fixture(run_dir, run_id)
            write_review_arbitration_fixture(run_dir, run_id, source_sha256=sha256_file(findings_path))
            client = TestClient(create_app(root))

            response = client.get(f"/runs/{run_id}/arbitration/new")

            self.assertEqual(response.status_code, 200)
            self.assertIn("Review arbitration already exists for this run", response.text)
            self.assertIn("does not overwrite arbitration yet", response.text)
            self.assertIn("disabled", response.text)

    def test_record_arbitration_without_confirmation_returns_error_without_job(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, run_dir = create_web_run(root)
            write_review_findings_fixture(run_dir, run_id)
            client = TestClient(create_app(root))

            response = client.post(
                f"/runs/{run_id}/arbitration/record",
                data={"arbitration_json": review_arbitration_json(run_id)},
            )

            self.assertEqual(response.status_code, 400)
            self.assertIn("Explicit record arbitration confirmation is required.", response.text)
            self.assertFalse((root / ".web" / "jobs").exists())
            self.assertFalse((run_dir / "REVIEW_ARBITRATION.json").exists())

    def test_record_arbitration_invalid_json_returns_error_without_job(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, run_dir = create_web_run(root)
            write_review_findings_fixture(run_dir, run_id)
            client = TestClient(create_app(root))

            response = client.post(
                f"/runs/{run_id}/arbitration/record",
                data={
                    "arbitration_json": "{invalid json",
                    "confirm_record_arbitration": "yes",
                },
            )

            self.assertEqual(response.status_code, 400)
            self.assertIn("Arbitration JSON did not match ReviewArbitrationReport", response.text)
            self.assertFalse((root / ".web" / "jobs").exists())
            self.assertFalse((run_dir / "REVIEW_ARBITRATION.json").exists())

    def test_record_arbitration_schema_invalid_json_returns_error_without_job(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, run_dir = create_web_run(root)
            write_review_findings_fixture(run_dir, run_id)
            payload = review_arbitration_payload(run_id)
            payload["arbitrated_findings"][0]["final_required_action"] = None
            client = TestClient(create_app(root))

            response = client.post(
                f"/runs/{run_id}/arbitration/record",
                data={
                    "arbitration_json": json.dumps(payload),
                    "confirm_record_arbitration": "yes",
                },
            )

            self.assertEqual(response.status_code, 400)
            self.assertIn("Arbitration JSON did not match ReviewArbitrationReport", response.text)
            self.assertFalse((root / ".web" / "jobs").exists())
            self.assertFalse((run_dir / "REVIEW_ARBITRATION.json").exists())

    def test_record_arbitration_run_id_mismatch_returns_error_without_job(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, run_dir = create_web_run(root)
            write_review_findings_fixture(run_dir, run_id)
            client = TestClient(create_app(root))

            response = client.post(
                f"/runs/{run_id}/arbitration/record",
                data={
                    "arbitration_json": review_arbitration_json("other-run"),
                    "confirm_record_arbitration": "yes",
                },
            )

            self.assertEqual(response.status_code, 400)
            self.assertIn("Arbitration report run_id must match this run", response.text)
            self.assertFalse((root / ".web" / "jobs").exists())
            self.assertFalse((run_dir / "REVIEW_ARBITRATION.json").exists())

    def test_record_arbitration_requires_existing_findings_without_job(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, run_dir = create_web_run(root)
            client = TestClient(create_app(root))

            response = client.post(
                f"/runs/{run_id}/arbitration/record",
                data={
                    "arbitration_json": review_arbitration_json(run_id),
                    "confirm_record_arbitration": "yes",
                },
            )

            self.assertEqual(response.status_code, 400)
            self.assertIn("Review findings are required before arbitration can be recorded", response.text)
            self.assertFalse((root / ".web" / "jobs").exists())
            self.assertFalse((run_dir / "REVIEW_ARBITRATION.json").exists())

    def test_record_arbitration_refuses_existing_arbitration_without_job(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, run_dir = create_web_run(root)
            findings_path = write_review_findings_fixture(run_dir, run_id)
            before_arbitration = write_review_arbitration_fixture(
                run_dir,
                run_id,
                source_sha256=sha256_file(findings_path),
            ).read_text(encoding="utf-8")
            client = TestClient(create_app(root))

            response = client.post(
                f"/runs/{run_id}/arbitration/record",
                data={
                    "arbitration_json": review_arbitration_json(run_id),
                    "confirm_record_arbitration": "yes",
                },
            )

            self.assertEqual(response.status_code, 400)
            self.assertIn("does not overwrite arbitration yet", response.text)
            self.assertFalse((root / ".web" / "jobs").exists())
            self.assertEqual((run_dir / "REVIEW_ARBITRATION.json").read_text(encoding="utf-8"), before_arbitration)

    def test_record_arbitration_valid_json_creates_hidden_job_with_generated_input(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, run_dir = create_web_run(root)
            write_review_findings_fixture(run_dir, run_id)
            client = TestClient(create_app(root))

            with patch(
                "ai_orchestrator_web.routes.runs.start_background_job",
                side_effect=create_job_without_starting_subprocess,
            ) as start_job:
                response = client.post(
                    f"/runs/{run_id}/arbitration/record",
                    data={
                        "arbitration_json": review_arbitration_json(run_id),
                        "confirm_record_arbitration": "yes",
                    },
                    follow_redirects=False,
                )

            self.assertEqual(response.status_code, 303)
            start_job.assert_called_once()
            location = response.headers["location"]
            job_id = location.rsplit("/", 1)[-1]
            job = load_job(root, job_id)
            self.assertEqual(job.action, "record_arbitration")
            self.assertIn("record-arbitration", job.command)
            self.assertIn("--arbitration-file", job.command)
            self.assertNotIn("--force", job.command)
            self.assertEqual(job.result_refs["arbitration_url"], f"/runs/{run_id}/arbitration")
            input_paths = list((root / ".web" / "arbitration_inputs").glob("*.json"))
            self.assertEqual(len(input_paths), 1)
            self.assertIn(str(input_paths[0].resolve()), job.command)
            normalized_payload = json.loads(input_paths[0].read_text(encoding="utf-8"))
            self.assertEqual(normalized_payload["run_id"], run_id)
            self.assertEqual(normalized_payload["counts"]["final_blocking"], 1)
            self.assertFalse((run_dir / "REVIEW_ARBITRATION.json").exists())

            detail = client.get(location)

            self.assertEqual(detail.status_code, 200)
            self.assertIn(f'href="/runs/{run_id}/arbitration"', detail.text)

    def test_record_arbitration_action_builds_safe_argv(self) -> None:
        with TemporaryProject() as root:
            run_id, _run_dir = create_web_run(root)
            inputs_dir = root / ".web" / "arbitration_inputs"
            input_path = inputs_dir / "safe_arbitration.json"
            write_text(input_path, review_arbitration_json(run_id))

            command = build_action_command(
                "record_arbitration",
                root,
                params={"run_id": run_id, "arbitration_input_id": input_path.name},
            )

            self.assertIsInstance(command, list)
            self.assertIn("record-arbitration", command)
            self.assertIn(run_id, command)
            self.assertIn("--runs-dir", command)
            self.assertIn(str((root / ".runs").resolve()), command)
            self.assertIn("--arbitration-file", command)
            self.assertIn(str(input_path.resolve()), command)
            self.assertNotIn("--force", command)
            self.assertNotIn("review-run", command)
            self.assertNotIn("apply-run", command)
            self.assertNotIn("accept-run", command)

    def test_record_arbitration_action_rejects_unsafe_or_missing_inputs(self) -> None:
        with TemporaryProject() as root:
            run_id, _run_dir = create_web_run(root)
            inputs_dir = root / ".web" / "arbitration_inputs"
            input_path = inputs_dir / "safe_arbitration.json"
            write_text(input_path, review_arbitration_json(run_id))

            with self.assertRaises(ValueError):
                build_action_command(
                    "record_arbitration",
                    root,
                    params={"run_id": "..\\bad", "arbitration_input_id": input_path.name},
                )
            with self.assertRaises(ValueError):
                build_action_command(
                    "record_arbitration",
                    root,
                    params={"run_id": run_id, "arbitration_input_id": "missing.json"},
                )
            with self.assertRaises(ValueError):
                build_action_command(
                    "record_arbitration",
                    root,
                    params={"run_id": run_id, "arbitration_input_id": "..\\outside.json"},
                )

    def test_jobs_selector_does_not_expose_record_arbitration(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            client = TestClient(create_app(root))

            response = client.get("/jobs")

            self.assertEqual(response.status_code, 200)
            self.assertNotIn("record_arbitration", response.text)

    def test_arbitration_new_page_has_no_review_apply_accept_actions(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, run_dir = create_web_run(root)
            write_review_findings_fixture(run_dir, run_id)
            client = TestClient(create_app(root))

            response = client.get(f"/runs/{run_id}/arbitration/new")

            self.assertEqual(response.status_code, 200)
            self.assertNotIn("review-run", response.text)
            self.assertNotIn("apply-run", response.text)
            self.assertNotIn("accept-run", response.text)
            self.assertNotIn("run-pipeline", response.text)

    def test_arbitration_pages_link_to_record_arbitration_form_when_appropriate(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, run_dir = create_web_run(root)
            write_review_findings_fixture(run_dir, run_id)
            client = TestClient(create_app(root))

            arbitration = client.get(f"/runs/{run_id}/arbitration")
            detail = client.get(f"/runs/{run_id}")

            self.assertEqual(arbitration.status_code, 200)
            self.assertEqual(detail.status_code, 200)
            self.assertIn(f'href="/runs/{run_id}/arbitration/new"', arbitration.text)
            self.assertIn(f'href="/runs/{run_id}/arbitration/new"', detail.text)

    def test_one_active_job_rule_blocks_record_arbitration_job(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, run_dir = create_web_run(root)
            write_review_findings_fixture(run_dir, run_id)
            before_state = (run_dir / "state.json").read_text(encoding="utf-8")
            command = build_action_command("web_health_cli", root)
            job = create_job_record(action="web_health_cli", project_root=root, command=command)
            job.status = "running"
            save_job(root, job)
            client = TestClient(create_app(root))

            response = client.post(
                f"/runs/{run_id}/arbitration/record",
                data={
                    "arbitration_json": review_arbitration_json(run_id),
                    "confirm_record_arbitration": "yes",
                },
            )

            self.assertEqual(response.status_code, 409)
            self.assertEqual((run_dir / "state.json").read_text(encoding="utf-8"), before_state)
            self.assertFalse((root / ".web" / "arbitration_inputs").exists())
            self.assertFalse((run_dir / "REVIEW_ARBITRATION.json").exists())

    def test_new_review_decision_form_returns_200_for_run_without_decision(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, _run_dir = create_web_run(root)
            client = TestClient(create_app(root))

            response = client.get(f"/runs/{run_id}/review-decision/new")

            self.assertEqual(response.status_code, 200)
            self.assertIn("Record Review Decision", response.text)
            self.assertIn("approved", response.text)
            self.assertIn("rejected", response.text)
            self.assertIn("feedback", response.text)
            self.assertIn('name="confirm_review_decision"', response.text)
            self.assertIn("does not apply", response.text)
            self.assertIn("does not commit", response.text)
            self.assertFalse((root / ".web" / "jobs").exists())

    def test_new_review_decision_form_warns_when_decision_exists(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, run_dir = create_web_run(root)
            write_review_decision_fixture(run_dir, run_id)
            client = TestClient(create_app(root))

            response = client.get(f"/runs/{run_id}/review-decision/new")

            self.assertEqual(response.status_code, 200)
            self.assertIn("Human review decision already exists for this run", response.text)
            self.assertIn("does not overwrite review decisions yet", response.text)
            self.assertIn("disabled", response.text)

    def test_record_review_decision_without_confirmation_returns_error_without_job(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, run_dir = create_web_run(root)
            client = TestClient(create_app(root))

            response = client.post(
                f"/runs/{run_id}/review-decision/record",
                data={"decision": "approved"},
            )

            self.assertEqual(response.status_code, 400)
            self.assertIn("Explicit review decision confirmation is required.", response.text)
            self.assertFalse((root / ".web" / "jobs").exists())
            self.assertFalse((run_dir / "REVIEW_DECISION.json").exists())

    def test_record_review_decision_invalid_decision_returns_error_without_job(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, run_dir = create_web_run(root)
            client = TestClient(create_app(root))

            response = client.post(
                f"/runs/{run_id}/review-decision/record",
                data={
                    "decision": "maybe",
                    "confirm_review_decision": "yes",
                },
            )

            self.assertEqual(response.status_code, 400)
            self.assertIn("Review decision must be approved or rejected.", response.text)
            self.assertFalse((root / ".web" / "jobs").exists())
            self.assertFalse((run_dir / "REVIEW_DECISION.json").exists())

    def test_record_review_decision_rejected_requires_feedback_without_job(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, run_dir = create_web_run(root)
            client = TestClient(create_app(root))

            response = client.post(
                f"/runs/{run_id}/review-decision/record",
                data={
                    "decision": "rejected",
                    "feedback": "   ",
                    "confirm_review_decision": "yes",
                },
            )

            self.assertEqual(response.status_code, 400)
            self.assertIn("Feedback is required for rejected review decisions.", response.text)
            self.assertFalse((root / ".web" / "jobs").exists())
            self.assertFalse((run_dir / "REVIEW_DECISION.json").exists())

    def test_record_review_decision_refuses_existing_decision_without_job(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, run_dir = create_web_run(root)
            before_decision = write_review_decision_fixture(run_dir, run_id).read_text(encoding="utf-8")
            client = TestClient(create_app(root))

            response = client.post(
                f"/runs/{run_id}/review-decision/record",
                data={
                    "decision": "approved",
                    "confirm_review_decision": "yes",
                },
            )

            self.assertEqual(response.status_code, 400)
            self.assertIn("does not overwrite review decisions yet", response.text)
            self.assertFalse((root / ".web" / "jobs").exists())
            self.assertEqual((run_dir / "REVIEW_DECISION.json").read_text(encoding="utf-8"), before_decision)

    def test_record_review_decision_unsafe_run_id_returns_not_found(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            client = TestClient(create_app(root))

            get_response = client.get("/runs/bad%5Cname/review-decision/new")
            post_response = client.post(
                "/runs/bad%5Cname/review-decision/record",
                data={"decision": "approved", "confirm_review_decision": "yes"},
            )

            self.assertIn(get_response.status_code, {400, 404})
            self.assertIn(post_response.status_code, {400, 404})

    def test_record_review_decision_approved_creates_hidden_job_without_feedback(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, run_dir = create_web_run(root)
            client = TestClient(create_app(root))

            with patch(
                "ai_orchestrator_web.routes.runs.start_background_job",
                side_effect=create_job_without_starting_subprocess,
            ) as start_job:
                response = client.post(
                    f"/runs/{run_id}/review-decision/record",
                    data={
                        "decision": "approved",
                        "feedback": "ignored for approved decisions",
                        "confirm_review_decision": "yes",
                    },
                    follow_redirects=False,
                )

            self.assertEqual(response.status_code, 303)
            start_job.assert_called_once()
            location = response.headers["location"]
            job_id = location.rsplit("/", 1)[-1]
            job = load_job(root, job_id)
            self.assertEqual(job.action, "review_run")
            self.assertIn("review-run", job.command)
            self.assertIn(run_id, job.command)
            self.assertIn("--decision", job.command)
            self.assertIn("approved", job.command)
            self.assertNotIn("--feedback", job.command)
            self.assertNotIn("--force", job.command)
            self.assertNotIn("--from-findings", job.command)
            self.assertNotIn("--force-feedback", job.command)
            self.assertFalse((root / ".web" / "review_feedback_inputs").exists())
            self.assertFalse((run_dir / "REVIEW_DECISION.json").exists())

            detail = client.get(location)

            self.assertEqual(detail.status_code, 200)
            self.assertIn(f'href="/runs/{run_id}"', detail.text)

    def test_record_review_decision_rejected_creates_hidden_job_with_generated_feedback(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, run_dir = create_web_run(root)
            feedback = "Reviewer says this needs rework.\r\nPlease add tests."
            client = TestClient(create_app(root))

            with patch(
                "ai_orchestrator_web.routes.runs.start_background_job",
                side_effect=create_job_without_starting_subprocess,
            ) as start_job:
                response = client.post(
                    f"/runs/{run_id}/review-decision/record",
                    data={
                        "decision": "rejected",
                        "feedback": feedback,
                        "confirm_review_decision": "yes",
                    },
                    follow_redirects=False,
                )

            self.assertEqual(response.status_code, 303)
            start_job.assert_called_once()
            location = response.headers["location"]
            job_id = location.rsplit("/", 1)[-1]
            job = load_job(root, job_id)
            self.assertEqual(job.action, "review_run")
            self.assertIn("review-run", job.command)
            self.assertIn("--decision", job.command)
            self.assertIn("rejected", job.command)
            self.assertIn("--feedback", job.command)
            self.assertNotIn("--force", job.command)
            self.assertNotIn("--from-findings", job.command)
            self.assertNotIn("--force-feedback", job.command)
            input_paths = list((root / ".web" / "review_feedback_inputs").glob("*.md"))
            self.assertEqual(len(input_paths), 1)
            self.assertIn(str(input_paths[0].resolve()), job.command)
            self.assertEqual(input_paths[0].read_text(encoding="utf-8"), "Reviewer says this needs rework.\nPlease add tests.\n")
            self.assertFalse((run_dir / "REVIEW_DECISION.json").exists())

    def test_review_run_action_builds_safe_argv_for_approved_and_rejected(self) -> None:
        with TemporaryProject() as root:
            run_id, _run_dir = create_web_run(root)
            inputs_dir = root / ".web" / "review_feedback_inputs"
            input_path = inputs_dir / "safe_feedback.md"
            write_text(input_path, "Needs rework.")

            approved = build_action_command(
                "review_run",
                root,
                params={"run_id": run_id, "decision": "approved"},
            )
            rejected = build_action_command(
                "review_run",
                root,
                params={"run_id": run_id, "decision": "rejected", "feedback_input_id": input_path.name},
            )

            self.assertIsInstance(approved, list)
            self.assertIn("review-run", approved)
            self.assertIn("--decision", approved)
            self.assertIn("approved", approved)
            self.assertNotIn("--feedback", approved)
            self.assertNotIn("--force", approved)
            self.assertNotIn("--from-findings", approved)
            self.assertNotIn("--force-feedback", approved)
            self.assertIsInstance(rejected, list)
            self.assertIn("review-run", rejected)
            self.assertIn("--decision", rejected)
            self.assertIn("rejected", rejected)
            self.assertIn("--feedback", rejected)
            self.assertIn(str(input_path.resolve()), rejected)
            self.assertNotIn("--force", rejected)
            self.assertNotIn("--from-findings", rejected)
            self.assertNotIn("--force-feedback", rejected)
            self.assertNotIn("apply-run", rejected)
            self.assertNotIn("accept-run", rejected)

    def test_review_run_action_rejects_unsafe_invalid_or_missing_inputs(self) -> None:
        with TemporaryProject() as root:
            run_id, _run_dir = create_web_run(root)
            inputs_dir = root / ".web" / "review_feedback_inputs"
            input_path = inputs_dir / "safe_feedback.md"
            write_text(input_path, "Needs rework.")

            with self.assertRaises(ValueError):
                build_action_command("review_run", root, params={"run_id": "..\\bad", "decision": "approved"})
            with self.assertRaises(ValueError):
                build_action_command("review_run", root, params={"run_id": run_id, "decision": "maybe"})
            with self.assertRaises(ValueError):
                build_action_command("review_run", root, params={"run_id": run_id, "decision": "rejected"})
            with self.assertRaises(ValueError):
                build_action_command(
                    "review_run",
                    root,
                    params={"run_id": run_id, "decision": "rejected", "feedback_input_id": "..\\outside.md"},
                )
            with self.assertRaises(ValueError):
                build_action_command(
                    "review_run",
                    root,
                    params={"run_id": run_id, "decision": "approved", "feedback_input_id": input_path.name},
                )

    def test_jobs_selector_does_not_expose_review_run(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            client = TestClient(create_app(root))

            response = client.get("/jobs")

            self.assertEqual(response.status_code, 200)
            self.assertNotIn("review_run", response.text)

    def test_review_decision_page_has_no_apply_accept_actions(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, _run_dir = create_web_run(root)
            client = TestClient(create_app(root))

            response = client.get(f"/runs/{run_id}/review-decision/new")

            self.assertEqual(response.status_code, 200)
            self.assertNotIn("apply-run", response.text)
            self.assertNotIn("accept-run", response.text)

    def test_review_decision_validation_failure_creates_no_jobs_or_artifacts(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, run_dir = create_web_run(root)
            client = TestClient(create_app(root))

            response = client.post(
                f"/runs/{run_id}/review-decision/record",
                data={
                    "decision": "rejected",
                    "feedback": "",
                    "confirm_review_decision": "yes",
                },
            )

            self.assertEqual(response.status_code, 400)
            self.assertFalse((root / ".web" / "jobs").exists())
            self.assertFalse((root / ".web" / "review_feedback_inputs").exists())
            self.assertFalse((run_dir / "REVIEW_DECISION.json").exists())

    def test_run_detail_links_to_review_decision_form_when_no_decision_exists(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, _run_dir = create_web_run(root)
            client = TestClient(create_app(root))

            response = client.get(f"/runs/{run_id}")

            self.assertEqual(response.status_code, 200)
            self.assertIn("Record review decision", response.text)
            self.assertIn(f'href="/runs/{run_id}/review-decision/new"', response.text)

    def test_one_active_job_rule_blocks_review_decision_job(self) -> None:
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

            response = client.post(
                f"/runs/{run_id}/review-decision/record",
                data={
                    "decision": "rejected",
                    "feedback": "Needs rework.",
                    "confirm_review_decision": "yes",
                },
            )

            self.assertEqual(response.status_code, 409)
            self.assertEqual((run_dir / "state.json").read_text(encoding="utf-8"), before_state)
            self.assertFalse((root / ".web" / "review_feedback_inputs").exists())
            self.assertFalse((run_dir / "REVIEW_DECISION.json").exists())

    def test_apply_new_page_returns_200_for_approved_run_not_applied(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, run_dir = create_web_run(root)
            write_review_decision_fixture(run_dir, run_id, decision="approved")
            client = TestClient(create_app(root))

            response = client.get(f"/runs/{run_id}/apply/new")

            self.assertEqual(response.status_code, 200)
            self.assertIn("Apply Approved Run", response.text)
            self.assertIn("modify the target repository working tree", response.text)
            self.assertIn("will not commit", response.text)
            self.assertIn("git diff", response.text)
            self.assertIn('name="confirm_apply_run"', response.text)
            self.assertFalse((root / ".web" / "jobs").exists())

    def test_apply_new_page_warns_without_human_approval(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, _run_dir = create_web_run(root)
            client = TestClient(create_app(root))

            response = client.get(f"/runs/{run_id}/apply/new")

            self.assertEqual(response.status_code, 200)
            self.assertIn("This run is not human-approved", response.text)
            self.assertIn("disabled", response.text)

    def test_apply_new_page_warns_for_rejected_run(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, run_dir = create_web_run(root)
            write_review_decision_fixture(run_dir, run_id, decision="rejected")
            client = TestClient(create_app(root))

            response = client.get(f"/runs/{run_id}/apply/new")

            self.assertEqual(response.status_code, 200)
            self.assertIn("This run is rejected and cannot be applied from Web.", response.text)
            self.assertIn("disabled", response.text)

    def test_apply_new_page_warns_when_apply_report_exists(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, run_dir = create_web_run(root)
            write_review_decision_fixture(run_dir, run_id, decision="approved")
            (run_dir / "APPLY_REPORT.json").write_text("{}\n", encoding="utf-8")
            client = TestClient(create_app(root))

            response = client.get(f"/runs/{run_id}/apply/new")

            self.assertEqual(response.status_code, 200)
            self.assertIn("Apply report already exists for this run.", response.text)
            self.assertIn("disabled", response.text)

    def test_apply_post_without_confirmation_returns_error_without_job(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, run_dir = create_web_run(root)
            write_review_decision_fixture(run_dir, run_id, decision="approved")
            client = TestClient(create_app(root))

            response = client.post(f"/runs/{run_id}/apply", data={})

            self.assertEqual(response.status_code, 400)
            self.assertIn("Explicit apply confirmation is required.", response.text)
            self.assertFalse((root / ".web" / "jobs").exists())
            self.assertFalse((run_dir / "APPLY_REPORT.json").exists())

    def test_apply_post_missing_approval_refuses_without_job(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, run_dir = create_web_run(root)
            client = TestClient(create_app(root))

            response = client.post(f"/runs/{run_id}/apply", data={"confirm_apply_run": "yes"})

            self.assertEqual(response.status_code, 400)
            self.assertIn("This run is not human-approved", response.text)
            self.assertFalse((root / ".web" / "jobs").exists())
            self.assertFalse((run_dir / "APPLY_REPORT.json").exists())

    def test_apply_post_rejected_decision_refuses_without_job(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, run_dir = create_web_run(root)
            write_review_decision_fixture(run_dir, run_id, decision="rejected")
            client = TestClient(create_app(root))

            response = client.post(f"/runs/{run_id}/apply", data={"confirm_apply_run": "yes"})

            self.assertEqual(response.status_code, 400)
            self.assertIn("This run is rejected and cannot be applied from Web.", response.text)
            self.assertFalse((root / ".web" / "jobs").exists())
            self.assertFalse((run_dir / "APPLY_REPORT.json").exists())

    def test_apply_post_existing_apply_report_refuses_without_job(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, run_dir = create_web_run(root)
            write_review_decision_fixture(run_dir, run_id, decision="approved")
            apply_report = run_dir / "APPLY_REPORT.json"
            apply_report.write_text('{"status": "applied"}\n', encoding="utf-8")
            before_report = apply_report.read_text(encoding="utf-8")
            client = TestClient(create_app(root))

            response = client.post(f"/runs/{run_id}/apply", data={"confirm_apply_run": "yes"})

            self.assertEqual(response.status_code, 400)
            self.assertIn("does not re-apply runs yet", response.text)
            self.assertFalse((root / ".web" / "jobs").exists())
            self.assertEqual(apply_report.read_text(encoding="utf-8"), before_report)

    def test_apply_post_unsafe_run_id_returns_not_found(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            client = TestClient(create_app(root))

            get_response = client.get("/runs/bad%5Cname/apply/new")
            post_response = client.post("/runs/bad%5Cname/apply", data={"confirm_apply_run": "yes"})

            self.assertIn(get_response.status_code, {400, 404})
            self.assertIn(post_response.status_code, {400, 404})

    def test_apply_post_approved_creates_hidden_job_without_running_cli(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, run_dir = create_web_run(root)
            write_review_decision_fixture(run_dir, run_id, decision="approved")
            before_state = (run_dir / "state.json").read_text(encoding="utf-8")
            client = TestClient(create_app(root))

            with patch(
                "ai_orchestrator_web.routes.runs.start_background_job",
                side_effect=create_job_without_starting_subprocess,
            ) as start_job:
                response = client.post(
                    f"/runs/{run_id}/apply",
                    data={"confirm_apply_run": "yes"},
                    follow_redirects=False,
                )

            self.assertEqual(response.status_code, 303)
            start_job.assert_called_once()
            location = response.headers["location"]
            job_id = location.rsplit("/", 1)[-1]
            job = load_job(root, job_id)
            self.assertEqual(job.action, "apply_run")
            self.assertIn("apply-run", job.command)
            self.assertIn(run_id, job.command)
            self.assertIn("--runs-dir", job.command)
            self.assertIn(str((root / ".runs").resolve()), job.command)
            self.assertNotIn("--allow-unreviewed", job.command)
            self.assertNotIn("--target-workspace", job.command)
            self.assertNotIn("--dry-run", job.command)
            self.assertNotIn("accept-run", job.command)
            self.assertNotIn("git", job.command)
            self.assertNotIn("commit", job.command)
            self.assertNotIn("add", job.command)
            self.assertEqual((run_dir / "state.json").read_text(encoding="utf-8"), before_state)
            self.assertFalse((run_dir / "APPLY_REPORT.json").exists())

            detail = client.get(location)

            self.assertEqual(detail.status_code, 200)
            self.assertIn(f'href="/runs/{run_id}"', detail.text)

    def test_apply_run_action_builds_safe_argv(self) -> None:
        with TemporaryProject() as root:
            run_id, _run_dir = create_web_run(root)

            command = build_action_command("apply_run", root, params={"run_id": run_id})

            self.assertIsInstance(command, list)
            self.assertIn("apply-run", command)
            self.assertIn(run_id, command)
            self.assertIn("--runs-dir", command)
            self.assertIn(str((root / ".runs").resolve()), command)
            self.assertNotIn("--allow-unreviewed", command)
            self.assertNotIn("--target-workspace", command)
            self.assertNotIn("--dry-run", command)
            self.assertNotIn("accept-run", command)

    def test_apply_run_action_rejects_unsafe_or_missing_run(self) -> None:
        with TemporaryProject() as root:
            _run_id, _run_dir = create_web_run(root)

            with self.assertRaises(ValueError):
                build_action_command("apply_run", root, params={"run_id": "..\\bad"})
            with self.assertRaises(ValueError):
                build_action_command("apply_run", root, params={"run_id": "missing_run"})

    def test_jobs_selector_does_not_expose_apply_run(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            client = TestClient(create_app(root))

            response = client.get("/jobs")

            self.assertEqual(response.status_code, 200)
            self.assertNotIn("apply_run", response.text)

    def test_apply_page_and_run_detail_have_no_accept_actions(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, run_dir = create_web_run(root)
            write_review_decision_fixture(run_dir, run_id, decision="approved")
            client = TestClient(create_app(root))

            apply_response = client.get(f"/runs/{run_id}/apply/new")
            detail_response = client.get(f"/runs/{run_id}")

            self.assertEqual(apply_response.status_code, 200)
            self.assertEqual(detail_response.status_code, 200)
            self.assertNotIn(f'action="/runs/{run_id}/accept', apply_response.text)
            self.assertNotIn(f'action="/runs/{run_id}/accept', detail_response.text)
            self.assertNotIn("Accept run", apply_response.text)
            self.assertNotIn("Accept run", detail_response.text)

    def test_apply_validation_failure_creates_no_jobs_or_artifacts(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, run_dir = create_web_run(root)
            before_state = (run_dir / "state.json").read_text(encoding="utf-8")
            client = TestClient(create_app(root))

            response = client.post(f"/runs/{run_id}/apply", data={"confirm_apply_run": "yes"})

            self.assertEqual(response.status_code, 400)
            self.assertFalse((root / ".web" / "jobs").exists())
            self.assertEqual((run_dir / "state.json").read_text(encoding="utf-8"), before_state)
            self.assertFalse((run_dir / "APPLY_REPORT.json").exists())
            self.assertFalse((run_dir / "APPLY_REPORT.md").exists())

    def test_run_detail_links_to_apply_page_when_approved_not_applied(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, run_dir = create_web_run(root)
            write_review_decision_fixture(run_dir, run_id, decision="approved")
            client = TestClient(create_app(root))

            response = client.get(f"/runs/{run_id}")

            self.assertEqual(response.status_code, 200)
            self.assertIn("Apply approved run", response.text)
            self.assertIn(f'href="/runs/{run_id}/apply/new"', response.text)

    def test_one_active_job_rule_blocks_apply_job(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, run_dir = create_web_run(root)
            write_review_decision_fixture(run_dir, run_id, decision="approved")
            before_state = (run_dir / "state.json").read_text(encoding="utf-8")
            command = build_action_command("web_health_cli", root)
            job = create_job_record(action="web_health_cli", project_root=root, command=command)
            job.status = "running"
            save_job(root, job)
            client = TestClient(create_app(root))

            response = client.post(f"/runs/{run_id}/apply", data={"confirm_apply_run": "yes"})

            self.assertEqual(response.status_code, 409)
            self.assertEqual((run_dir / "state.json").read_text(encoding="utf-8"), before_state)
            self.assertFalse((run_dir / "APPLY_REPORT.json").exists())

    def test_apply_report_page_returns_empty_state_when_missing(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, run_dir = create_web_run(root)
            write_review_decision_fixture(run_dir, run_id, decision="approved")
            client = TestClient(create_app(root))

            response = client.get(f"/runs/{run_id}/apply-report")

            self.assertEqual(response.status_code, 200)
            self.assertIn("Apply report not found", response.text)
            self.assertIn(f'href="/runs/{run_id}/apply/new"', response.text)
            self.assertNotIn("<form", response.text.lower())
            self.assertFalse((root / ".web" / "jobs").exists())

    def test_apply_report_page_shows_existing_report_and_manual_checklist(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, run_dir = create_web_run(root)
            write_review_decision_fixture(run_dir, run_id, decision="approved")
            write_apply_report_fixture(run_dir, run_id)
            client = TestClient(create_app(root))

            response = client.get(f"/runs/{run_id}/apply-report")

            self.assertEqual(response.status_code, 200)
            self.assertIn("status", response.text)
            self.assertIn("applied", response.text)
            self.assertIn("target_workspace", response.text)
            self.assertIn("review_gate", response.text)
            self.assertIn("commit_created", response.text)
            self.assertIn("false", response.text)
            self.assertIn("git_add_performed", response.text)
            self.assertIn("docs/applied.md", response.text)
            self.assertIn("docs/deleted.md", response.text)
            self.assertIn("EXECUTION_REPORT.json", response.text)
            self.assertIn("git diff --stat", response.text)
            self.assertIn("git diff", response.text)
            self.assertIn("python -m unittest discover -s tests", response.text)
            self.assertIn("git add", response.text)
            self.assertIn("git commit", response.text)
            self.assertNotIn("<form", response.text.lower())

    def test_apply_report_page_invalid_json_returns_read_only_error(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, run_dir = create_web_run(root)
            (run_dir / "APPLY_REPORT.json").write_text("{invalid json", encoding="utf-8")
            client = TestClient(create_app(root))

            response = client.get(f"/runs/{run_id}/apply-report")

            self.assertEqual(response.status_code, 200)
            self.assertIn("Read-only error", response.text)
            self.assertIn("APPLY_REPORT.json could not be loaded", response.text)

    def test_apply_report_page_rejects_unsafe_run_id(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            client = TestClient(create_app(root))

            backslash_response = client.get("/runs/bad%5Cname/apply-report")
            traversal_response = client.get("/runs/..%2Fsecret/apply-report")

            self.assertIn(backslash_response.status_code, {400, 404})
            self.assertIn(traversal_response.status_code, {400, 404})

    def test_apply_report_page_does_not_modify_jobs_or_run_artifacts(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, run_dir = create_web_run(root)
            json_path, markdown_path = write_apply_report_fixture(run_dir, run_id)
            before_state = (run_dir / "state.json").read_text(encoding="utf-8")
            before_json = json_path.read_text(encoding="utf-8")
            before_markdown = markdown_path.read_text(encoding="utf-8")
            client = TestClient(create_app(root))

            response = client.get(f"/runs/{run_id}/apply-report")

            self.assertEqual(response.status_code, 200)
            self.assertFalse((root / ".web" / "jobs").exists())
            self.assertEqual((run_dir / "state.json").read_text(encoding="utf-8"), before_state)
            self.assertEqual(json_path.read_text(encoding="utf-8"), before_json)
            self.assertEqual(markdown_path.read_text(encoding="utf-8"), before_markdown)

    def test_apply_report_page_has_no_active_write_controls(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, run_dir = create_web_run(root)
            write_apply_report_fixture(run_dir, run_id)
            client = TestClient(create_app(root))

            response = client.get(f"/runs/{run_id}/apply-report")

            self.assertEqual(response.status_code, 200)
            lowered = response.text.lower()
            self.assertNotIn("<form", lowered)
            self.assertNotIn("method=\"post\"", lowered)
            self.assertNotIn("<button", lowered)
            self.assertNotIn('action="/runs/', lowered)

    def test_run_detail_links_to_apply_report_when_report_exists(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, run_dir = create_web_run(root)
            write_review_decision_fixture(run_dir, run_id, decision="approved")
            write_apply_report_fixture(run_dir, run_id)
            client = TestClient(create_app(root))

            response = client.get(f"/runs/{run_id}")

            self.assertEqual(response.status_code, 200)
            self.assertIn("View apply report", response.text)
            self.assertIn(f'href="/runs/{run_id}/apply-report"', response.text)

    def test_job_detail_for_apply_job_links_to_apply_report_without_accept_or_commit_links(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            run_id, _run_dir = create_web_run(root)
            command = build_action_command("apply_run", root, params={"run_id": run_id})
            job = create_job_record(
                action="apply_run",
                project_root=root,
                command=command,
                result_refs={"run_id": run_id, "run_url": f"/runs/{run_id}", "runs_url": "/runs"},
            )
            save_job(root, job)
            client = TestClient(create_app(root))

            response = client.get(f"/jobs/{job.job_id}")

            self.assertEqual(response.status_code, 200)
            self.assertIn(f'href="/runs/{run_id}"', response.text)
            self.assertIn(f'href="/runs/{run_id}/apply-report"', response.text)
            self.assertNotIn("Accept run", response.text)
            self.assertNotIn("Commit", response.text)

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

    def test_prepare_review_post_creates_analysis_job_without_starting_cli_in_test(self) -> None:
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
                response = client.post(f"/runs/{run_id}/prepare-review", follow_redirects=False)

            self.assertEqual(response.status_code, 303)
            location = response.headers["location"]
            self.assertTrue(location.startswith("/jobs/job_"))
            start_job.assert_called_once()
            job_id = location.rsplit("/", 1)[-1]
            job = load_job(root, job_id)
            self.assertEqual(job.action, "prepare_review")
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
            self.assertIn("prepare-review", job.command)
            self.assertIn(run_id, job.command)
            self.assertIn("--runs-dir", job.command)
            self.assertIn(str((root / ".runs").resolve()), job.command)
            self.assertIn("--required-profiles", job.command)
            self.assertNotIn("--codex-cmd", job.command)
            self.assertNotIn("run-pipeline", job.command)
            self.assertNotIn("record-findings", job.command)
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

    def test_prepare_review_unknown_run_returns_not_found(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            client = TestClient(create_app(root))

            response = client.post("/runs/missing-run/prepare-review")

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

    def test_prepare_review_path_traversal_run_id_returns_not_found(self) -> None:
        from fastapi.testclient import TestClient

        from ai_orchestrator_web.app import create_app

        with TemporaryProject() as root:
            client = TestClient(create_app(root))

            response = client.post("/runs/bad%5Cname/prepare-review")

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

    def test_one_active_job_rule_blocks_prepare_review_job(self) -> None:
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

            response = client.post(f"/runs/{run_id}/prepare-review")

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
            self.assertNotIn("prepare_review", response.text)
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

    def test_prepare_review_action_builds_safe_argv(self) -> None:
        with TemporaryProject() as root:
            run_id, _run_dir = create_web_run(root)

            command = build_action_command(
                "prepare_review",
                root,
                params={"run_id": run_id},
            )

            self.assertIsInstance(command, list)
            self.assertIn("prepare-review", command)
            self.assertIn(run_id, command)
            self.assertIn("--runs-dir", command)
            self.assertIn(str((root / ".runs").resolve()), command)
            self.assertIn("--required-profiles", command)
            self.assertNotIn("--codex-cmd", command)
            self.assertNotIn("run-pipeline", command)
            self.assertNotIn("record-findings", command)
            self.assertNotIn("apply-run", command)
            self.assertNotIn("accept-run", command)

    def test_prepare_review_action_rejects_missing_run(self) -> None:
        with TemporaryProject() as root:
            with self.assertRaises(ValueError):
                build_action_command(
                    "prepare_review",
                    root,
                    params={"run_id": "missing-run"},
                )

    def test_prepare_review_action_rejects_unsafe_run_id(self) -> None:
        with TemporaryProject() as root:
            create_web_run(root)

            with self.assertRaises(ValueError):
                build_action_command(
                    "prepare_review",
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
