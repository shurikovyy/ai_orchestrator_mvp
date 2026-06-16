"""Job metadata model for local web jobs."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import uuid4


JobStatus = Literal["queued", "running", "succeeded", "failed"]


@dataclass
class JobRecord:
    job_id: str
    action: str
    status: JobStatus
    project_root: str
    command: list[str]
    created_at: str
    started_at: str | None
    finished_at: str | None
    exit_code: int | None
    stdout_path: str
    stderr_path: str
    error: str | None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "JobRecord":
        return cls(
            job_id=str(payload["job_id"]),
            action=str(payload["action"]),
            status=str(payload["status"]),  # type: ignore[arg-type]
            project_root=str(payload["project_root"]),
            command=[str(item) for item in payload["command"]],  # type: ignore[index]
            created_at=str(payload["created_at"]),
            started_at=_optional_str(payload.get("started_at")),
            finished_at=_optional_str(payload.get("finished_at")),
            exit_code=_optional_int(payload.get("exit_code")),
            stdout_path=str(payload["stdout_path"]),
            stderr_path=str(payload["stderr_path"]),
            error=_optional_str(payload.get("error")),
        )


def create_job_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"job_{timestamp}_{uuid4().hex[:6]}"


def create_job_record(*, action: str, project_root: Path, command: list[str]) -> JobRecord:
    job_id = create_job_id()
    jobs_dir = project_root / ".web" / "jobs"
    return JobRecord(
        job_id=job_id,
        action=action,
        status="queued",
        project_root=str(project_root.resolve()),
        command=list(command),
        created_at=now_utc_iso(),
        started_at=None,
        finished_at=None,
        exit_code=None,
        stdout_path=str((jobs_dir / f"{job_id}.stdout.log").resolve()),
        stderr_path=str((jobs_dir / f"{job_id}.stderr.log").resolve()),
        error=None,
    )


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    return int(value)
