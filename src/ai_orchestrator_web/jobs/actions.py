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
}


def list_allowed_actions() -> list[AllowedJobAction]:
    return [ALLOWED_ACTIONS[name] for name in sorted(ALLOWED_ACTIONS)]


def build_action_command(action: str, project_root: Path) -> list[str]:
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
    raise UnsupportedJobAction(f"unsupported job action: {action}")
