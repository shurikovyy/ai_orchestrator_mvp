"""JSON store for local web job metadata and logs."""

from __future__ import annotations

import json
from pathlib import Path, PurePath
import time
from uuid import uuid4

from .models import JobRecord


ACTIVE_STATUSES = {"queued", "running"}


def jobs_dir(project_root: Path) -> Path:
    return project_root / ".web" / "jobs"


def job_json_path(project_root: Path, job_id: str) -> Path:
    safe_job_id = validate_job_id(job_id)
    return jobs_dir(project_root) / f"{safe_job_id}.json"


def job_stdout_path(project_root: Path, job_id: str) -> Path:
    safe_job_id = validate_job_id(job_id)
    return jobs_dir(project_root) / f"{safe_job_id}.stdout.log"


def job_stderr_path(project_root: Path, job_id: str) -> Path:
    safe_job_id = validate_job_id(job_id)
    return jobs_dir(project_root) / f"{safe_job_id}.stderr.log"


def validate_job_id(job_id: str) -> str:
    if not job_id or job_id in {".", ".."}:
        raise ValueError("job not found")
    if "/" in job_id or "\\" in job_id:
        raise ValueError("job not found")
    if Path(job_id).is_absolute() or ".." in PurePath(job_id).parts:
        raise ValueError("job not found")
    return job_id


def save_job(project_root: Path, job: JobRecord) -> None:
    target = job_json_path(project_root, job.job_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target.with_name(f"{target.name}.{uuid4().hex}.tmp")
    tmp_path.write_text(json.dumps(job.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    for attempt in range(20):
        try:
            tmp_path.replace(target)
            return
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.01)


def load_job(project_root: Path, job_id: str) -> JobRecord:
    path = job_json_path(project_root, job_id)
    if not path.exists():
        raise FileNotFoundError(f"job not found: {job_id}")
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError(f"job metadata is invalid: {job_id}")
    return JobRecord.from_dict(payload)


def list_jobs(project_root: Path) -> list[JobRecord]:
    root = jobs_dir(project_root)
    if not root.exists():
        return []
    if not root.is_dir():
        raise ValueError(f"jobs path is not a directory: {root}")
    records: list[JobRecord] = []
    for path in sorted(root.glob("*.json"), key=lambda item: item.name, reverse=True):
        records.append(load_job(project_root, path.stem))
    return records


def has_active_job(project_root: Path) -> bool:
    return any(job.status in ACTIVE_STATUSES for job in list_jobs(project_root))
