"""Read-only project status helpers for the local web dashboard."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess

from ai_orchestrator import __version__ as ai_orchestrator_version


@dataclass(frozen=True)
class GitStatusSummary:
    status: str
    tracked_changes: int = 0
    untracked_changes: int = 0
    raw_lines: tuple[str, ...] = ()
    message: str = ""


@dataclass(frozen=True)
class ProjectStatus:
    project_root: Path
    ai_orchestrator_version: str
    git: GitStatusSummary
    tasks_yaml_exists: bool
    task_drafts_exists: bool
    runs_exists: bool


def get_git_status_summary(root: Path) -> GitStatusSummary:
    """Return a read-only summary of `git status --short` for root."""

    try:
        completed = subprocess.run(
            ["git", "status", "--short"],
            cwd=root,
            capture_output=True,
            text=True,
            shell=False,
            timeout=5,
            check=False,
        )
    except FileNotFoundError:
        return GitStatusSummary(status="unknown", message="git executable not found")
    except subprocess.TimeoutExpired:
        return GitStatusSummary(status="unknown", message="git status timed out")
    except OSError as exc:
        return GitStatusSummary(status="unknown", message=str(exc))

    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        status = "not_git_repo" if "not a git repository" in stderr.lower() else "unknown"
        return GitStatusSummary(status=status, message=stderr)

    lines = tuple(line.rstrip() for line in completed.stdout.splitlines() if line.strip())
    untracked_changes = sum(1 for line in lines if line.startswith("??"))
    tracked_changes = len(lines) - untracked_changes
    status = "clean" if not lines else "dirty"
    return GitStatusSummary(
        status=status,
        tracked_changes=tracked_changes,
        untracked_changes=untracked_changes,
        raw_lines=lines,
    )


def get_current_project_status(root: Path | str | None = None) -> ProjectStatus:
    project_root = (Path.cwd() if root is None else Path(root)).resolve()
    return ProjectStatus(
        project_root=project_root,
        ai_orchestrator_version=ai_orchestrator_version,
        git=get_git_status_summary(project_root),
        tasks_yaml_exists=(project_root / "tasks.yaml").exists(),
        task_drafts_exists=(project_root / ".task_drafts").exists(),
        runs_exists=(project_root / ".runs").exists(),
    )
