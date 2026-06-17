"""In-process background runner for allowlisted web jobs."""

from __future__ import annotations

from pathlib import Path
import subprocess
import threading

from .actions import build_action_command
from .models import JobRecord, create_job_record, now_utc_iso
from .store import has_active_job, job_stderr_path, job_stdout_path, save_job


class ActiveJobExists(RuntimeError):
    """Raised when the project already has a queued/running job."""


def start_background_job(
    *,
    project_root: Path,
    action: str,
    params: dict[str, str] | None = None,
    result_refs: dict[str, str] | None = None,
) -> JobRecord:
    root = project_root.resolve()
    if has_active_job(root):
        raise ActiveJobExists("another job is already queued or running")
    command = build_action_command(action, root, params=params)
    job = create_job_record(action=action, project_root=root, command=command, result_refs=result_refs)
    save_job(root, job)
    thread = threading.Thread(target=run_job_sync, args=(job, root), daemon=True)
    thread.start()
    return job


def run_job_sync(job: JobRecord, project_root: Path) -> JobRecord:
    root = project_root.resolve()
    stdout_path = job_stdout_path(root, job.job_id)
    stderr_path = job_stderr_path(root, job.job_id)
    stdout_path.parent.mkdir(parents=True, exist_ok=True)

    job.status = "running"
    job.started_at = now_utc_iso()
    save_job(root, job)

    try:
        with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open("w", encoding="utf-8") as stderr:
            process = subprocess.Popen(  # noqa: S603 - command is built from a strict allowlist.
                job.command,
                cwd=root,
                shell=False,
                stdout=stdout,
                stderr=stderr,
                text=True,
            )
            exit_code = process.wait()
        job.exit_code = exit_code
        job.status = "succeeded" if exit_code == 0 else "failed"
    except Exception as exc:  # noqa: BLE001 - persisted as operator-visible job failure.
        job.status = "failed"
        job.error = str(exc)
        if not stderr_path.exists():
            stderr_path.write_text("", encoding="utf-8")
        with stderr_path.open("a", encoding="utf-8") as stderr:
            stderr.write(f"\n{exc}\n")
    finally:
        job.finished_at = now_utc_iso()
        save_job(root, job)
    return job
