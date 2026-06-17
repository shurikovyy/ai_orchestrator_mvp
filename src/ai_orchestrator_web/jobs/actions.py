"""Strict allowlist for web-started CLI jobs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sys


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
}


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
