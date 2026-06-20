"""Strict allowlist for web-started CLI jobs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePath
import re
import sys

from ai_orchestrator.task_inspection import build_task_inspection_summary
from ai_orchestrator.task_queue import TaskQueueConfigError


class UnsupportedJobAction(ValueError):
    """Raised when a web request asks for a non-allowlisted action."""


@dataclass(frozen=True)
class AllowedJobAction:
    name: str
    label: str
    description: str
    show_in_jobs_form: bool = True


ALLOWED_ACTIONS: dict[str, AllowedJobAction] = {
    "web_health_cli": AllowedJobAction(
        name="web_health_cli",
        label="CLI help smoke",
        description="Run python -m ai_orchestrator.cli --help.",
    ),
    "list_task_drafts": AllowedJobAction(
        name="list_task_drafts",
        label="List task drafts",
        description="Run list-task-drafts as JSON.",
    ),
    "list_tasks": AllowedJobAction(
        name="list_tasks",
        label="List tasks",
        description="Run list-tasks against tasks.yaml as JSON.",
    ),
    "draft_task_scaffold": AllowedJobAction(
        name="draft_task_scaffold",
        label="Create task draft scaffold",
        description="Create a deterministic local task draft scaffold from a backend-generated raw request file.",
        show_in_jobs_form=False,
    ),
    "validate_task_draft": AllowedJobAction(
        name="validate_task_draft",
        label="Validate task draft",
        description="Run deterministic validation for one existing local task draft.",
        show_in_jobs_form=False,
    ),
    "promote_task_draft_disabled": AllowedJobAction(
        name="promote_task_draft_disabled",
        label="Promote task draft disabled",
        description="Promote one validated task draft into tasks.yaml with enabled=false.",
        show_in_jobs_form=False,
    ),
    "doctor_dry_run": AllowedJobAction(
        name="doctor_dry_run",
        label="Doctor dry-run",
        description="Run read-only doctor diagnostics for one task with dry-run intent.",
        show_in_jobs_form=False,
    ),
    "pipeline_dry_run": AllowedJobAction(
        name="pipeline_dry_run",
        label="Pipeline dry-run",
        description="Preview the run-pipeline plan for one task with --dry-run.",
        show_in_jobs_form=False,
    ),
    "doctor_real_run": AllowedJobAction(
        name="doctor_real_run",
        label="Doctor real-run",
        description="Run real-run readiness diagnostics for one task with configured Codex command.",
        show_in_jobs_form=False,
    ),
    "run_pipeline_real": AllowedJobAction(
        name="run_pipeline_real",
        label="Run real pipeline",
        description="Run the orchestrator pipeline for one task with configured Codex command.",
        show_in_jobs_form=False,
    ),
    "classify_run": AllowedJobAction(
        name="classify_run",
        label="Classify run",
        description="Classify risk for one existing run artifact directory.",
        show_in_jobs_form=False,
    ),
    "run_review_checks": AllowedJobAction(
        name="run_review_checks",
        label="Run review checks",
        description="Run deterministic review checks for one existing run artifact directory.",
        show_in_jobs_form=False,
    ),
    "prepare_review": AllowedJobAction(
        name="prepare_review",
        label="Prepare review",
        description="Prepare required reviewer prompt packets for one existing run artifact directory.",
        show_in_jobs_form=False,
    ),
    "record_findings": AllowedJobAction(
        name="record_findings",
        label="Record findings",
        description="Record structured review findings for one existing run from a server-generated JSON input file.",
        show_in_jobs_form=False,
    ),
}


SAFE_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")


def list_allowed_actions(*, include_parameterized: bool = False) -> list[AllowedJobAction]:
    actions = [ALLOWED_ACTIONS[name] for name in sorted(ALLOWED_ACTIONS)]
    if include_parameterized:
        return actions
    return [action for action in actions if action.show_in_jobs_form]


def build_action_command(action: str, project_root: Path, params: dict[str, str] | None = None) -> list[str]:
    values = params or {}
    if action == "web_health_cli":
        return [sys.executable, "-m", "ai_orchestrator.cli", "--help"]
    if action == "list_task_drafts":
        return [sys.executable, "-m", "ai_orchestrator.cli", "list-task-drafts", "--format", "json"]
    if action == "list_tasks":
        return [
            sys.executable,
            "-m",
            "ai_orchestrator.cli",
            "list-tasks",
            "--tasks-file",
            str(project_root / "tasks.yaml"),
            "--format",
            "json",
        ]
    if action == "draft_task_scaffold":
        request_path = _safe_raw_request_path(project_root, _required_param(values, "request_path"))
        command = [
            sys.executable,
            "-m",
            "ai_orchestrator.cli",
            "draft-task-scaffold",
            "--request",
            str(request_path),
        ]
        _append_optional(command, "--title", values.get("title"))
        _append_optional(command, "--task-id", values.get("task_id"))
        _append_optional(command, "--risk-level", values.get("risk_level"))
        _append_optional(command, "--prompt-language", values.get("prompt_language"))
        return command
    if action == "validate_task_draft":
        draft_id = _safe_existing_draft_id(project_root, _required_param(values, "draft_id"))
        return [
            sys.executable,
            "-m",
            "ai_orchestrator.cli",
            "validate-task-draft",
            draft_id,
            "--drafts-dir",
            str((project_root / ".task_drafts").resolve()),
            "--force",
        ]
    if action == "promote_task_draft_disabled":
        draft_id = _safe_existing_draft_id(project_root, _required_param(values, "draft_id"))
        return [
            sys.executable,
            "-m",
            "ai_orchestrator.cli",
            "promote-task-draft",
            draft_id,
            "--drafts-dir",
            str((project_root / ".task_drafts").resolve()),
            "--tasks-file",
            str((project_root / "tasks.yaml").resolve()),
        ]
    if action == "doctor_dry_run":
        task_id = _safe_existing_task_id(project_root, _required_param(values, "task_id"))
        return [
            sys.executable,
            "-m",
            "ai_orchestrator.cli",
            "doctor",
            "--tasks-file",
            str((project_root / "tasks.yaml").resolve()),
            "--task-id",
            task_id,
            "--intent",
            "dry-run",
        ]
    if action == "pipeline_dry_run":
        task_id = _safe_existing_task_id(project_root, _required_param(values, "task_id"))
        return [
            sys.executable,
            "-m",
            "ai_orchestrator.cli",
            "run-pipeline",
            "--tasks-file",
            str((project_root / "tasks.yaml").resolve()),
            "--only",
            task_id,
            "--dry-run",
        ]
    if action == "doctor_real_run":
        task_id = _safe_existing_task_id(project_root, _required_param(values, "task_id"))
        codex_cmd = _required_param(values, "codex_cmd").strip()
        if not codex_cmd:
            raise UnsupportedJobAction("missing required parameter for job action: codex_cmd")
        return [
            sys.executable,
            "-m",
            "ai_orchestrator.cli",
            "doctor",
            "--tasks-file",
            str((project_root / "tasks.yaml").resolve()),
            "--task-id",
            task_id,
            "--intent",
            "real-run",
            "--codex-cmd",
            codex_cmd,
        ]
    if action == "run_pipeline_real":
        task_id = _safe_existing_task_id(project_root, _required_param(values, "task_id"))
        codex_cmd = _required_param(values, "codex_cmd").strip()
        if not codex_cmd:
            raise UnsupportedJobAction("missing required parameter for job action: codex_cmd")
        return [
            sys.executable,
            "-m",
            "ai_orchestrator.cli",
            "run-pipeline",
            "--tasks-file",
            str((project_root / "tasks.yaml").resolve()),
            "--only",
            task_id,
            "--codex-cmd",
            codex_cmd,
            "--verbose",
            "--stream-codex-output",
        ]
    if action == "classify_run":
        run_id = _safe_existing_run_id(project_root, _required_param(values, "run_id"))
        return [
            sys.executable,
            "-m",
            "ai_orchestrator.cli",
            "classify-run",
            run_id,
            "--runs-dir",
            str((project_root / ".runs").resolve()),
        ]
    if action == "run_review_checks":
        run_id = _safe_existing_run_id(project_root, _required_param(values, "run_id"))
        return [
            sys.executable,
            "-m",
            "ai_orchestrator.cli",
            "run-review-checks",
            run_id,
            "--runs-dir",
            str((project_root / ".runs").resolve()),
        ]
    if action == "prepare_review":
        run_id = _safe_existing_run_id(project_root, _required_param(values, "run_id"))
        return [
            sys.executable,
            "-m",
            "ai_orchestrator.cli",
            "prepare-review",
            run_id,
            "--runs-dir",
            str((project_root / ".runs").resolve()),
            "--required-profiles",
        ]
    if action == "record_findings":
        run_id = _safe_existing_run_id(project_root, _required_param(values, "run_id"))
        findings_input = _safe_existing_findings_input(project_root, _required_param(values, "findings_input_id"))
        command = [
            sys.executable,
            "-m",
            "ai_orchestrator.cli",
            "record-findings",
            run_id,
            "--runs-dir",
            str((project_root / ".runs").resolve()),
            "--findings-file",
            str(findings_input),
        ]
        profile_raw = values.get("profile", "")
        profile = profile_raw.strip()
        if profile_raw and profile_raw != profile:
            raise UnsupportedJobAction("profile must not contain leading or trailing whitespace")
        if profile:
            if not _is_safe_identifier(profile):
                raise UnsupportedJobAction("profile is not safe")
            command.extend(["--profile", profile])
        return command
    raise UnsupportedJobAction(f"unsupported job action: {action}")


def _append_optional(command: list[str], flag: str, value: str | None) -> None:
    if value:
        command.extend([flag, value])


def _required_param(params: dict[str, str], key: str) -> str:
    value = params.get(key, "")
    if not value:
        raise UnsupportedJobAction(f"missing required parameter for job action: {key}")
    return value


def _safe_raw_request_path(project_root: Path, value: str) -> Path:
    root = project_root.resolve()
    raw_requests_dir = (root / ".task_drafts" / "raw_requests").resolve()
    request_path = Path(value).resolve()
    try:
        request_path.relative_to(raw_requests_dir)
    except ValueError as exc:
        raise UnsupportedJobAction("raw request path must be under .task_drafts/raw_requests") from exc
    return request_path


def _safe_existing_draft_id(project_root: Path, value: str) -> str:
    draft_id = value.strip()
    if value != draft_id:
        raise UnsupportedJobAction("draft id must not contain leading or trailing whitespace")
    if not _is_safe_identifier(draft_id):
        raise UnsupportedJobAction("draft id is not safe")
    draft_dir = (project_root / ".task_drafts" / draft_id).resolve()
    drafts_dir = (project_root / ".task_drafts").resolve()
    try:
        draft_dir.relative_to(drafts_dir)
    except ValueError as exc:
        raise UnsupportedJobAction("draft id must resolve under .task_drafts") from exc
    if not draft_dir.is_dir():
        raise UnsupportedJobAction(f"task draft not found: {draft_id}")
    return draft_id


def _safe_existing_task_id(project_root: Path, value: str) -> str:
    task_id = value.strip()
    if value != task_id:
        raise UnsupportedJobAction("task id must not contain leading or trailing whitespace")
    if not _is_safe_identifier(task_id):
        raise UnsupportedJobAction("task id is not safe")
    tasks_file = (project_root / "tasks.yaml").resolve()
    if not tasks_file.is_file():
        raise UnsupportedJobAction("tasks.yaml not found")
    try:
        build_task_inspection_summary(tasks_file=tasks_file, task_id=task_id)
    except (FileNotFoundError, TaskQueueConfigError) as exc:
        raise UnsupportedJobAction(str(exc)) from exc
    return task_id


def _safe_existing_run_id(project_root: Path, value: str) -> str:
    run_id = value.strip()
    if value != run_id:
        raise UnsupportedJobAction("run id must not contain leading or trailing whitespace")
    if not _is_safe_identifier(run_id):
        raise UnsupportedJobAction("run id is not safe")
    run_dir = (project_root / ".runs" / run_id).resolve()
    runs_dir = (project_root / ".runs").resolve()
    try:
        run_dir.relative_to(runs_dir)
    except ValueError as exc:
        raise UnsupportedJobAction("run id must resolve under .runs") from exc
    if not run_dir.is_dir():
        raise UnsupportedJobAction(f"run not found: {run_id}")
    return run_id


def _safe_existing_findings_input(project_root: Path, value: str) -> Path:
    input_id = value.strip()
    if value != input_id:
        raise UnsupportedJobAction("findings input id must not contain leading or trailing whitespace")
    if not input_id.endswith(".json"):
        raise UnsupportedJobAction("findings input id must be a JSON file")
    if "/" in input_id or "\\" in input_id:
        raise UnsupportedJobAction("findings input id must not contain path separators")
    if Path(input_id).is_absolute() or ".." in PurePath(input_id).parts:
        raise UnsupportedJobAction("findings input id must not be a path")
    if not re.fullmatch(r"[A-Za-z0-9._-]+\.json", input_id):
        raise UnsupportedJobAction("findings input id is not safe")
    inputs_dir = (project_root / ".web" / "findings_inputs").resolve()
    input_path = (inputs_dir / input_id).resolve()
    try:
        input_path.relative_to(inputs_dir)
    except ValueError as exc:
        raise UnsupportedJobAction("findings input file must resolve under .web/findings_inputs") from exc
    if not input_path.is_file():
        raise UnsupportedJobAction(f"findings input file not found: {input_id}")
    return input_path


def _is_safe_identifier(value: str) -> bool:
    if not value or value in {".", ".."}:
        return False
    if "/" in value or "\\" in value:
        return False
    if Path(value).is_absolute() or ".." in PurePath(value).parts:
        return False
    return bool(SAFE_ID_PATTERN.fullmatch(value))
