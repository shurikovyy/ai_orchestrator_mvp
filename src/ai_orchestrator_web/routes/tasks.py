"""Task queue inspection and allowlisted lifecycle action routes."""

from __future__ import annotations

from pathlib import Path, PurePath
import re
from urllib.parse import parse_qs

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ai_orchestrator.task_inspection import (
    TaskInspectionSummary,
    build_task_inspection_summary,
    list_task_inspection_summaries,
    set_task_enabled,
)
from ai_orchestrator_web.config import get_configured_codex_cmd
from ai_orchestrator_web.jobs.actions import UnsupportedJobAction
from ai_orchestrator_web.jobs.runner import ActiveJobExists, start_background_job
from ai_orchestrator.task_queue import TaskQueueConfigError


PROMPT_PREVIEW_LIMIT = 4000
TASK_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")


def create_tasks_router(*, project_root: Path, templates: Jinja2Templates) -> APIRouter:
    router = APIRouter()
    tasks_file = project_root / "tasks.yaml"

    @router.get("/tasks", response_class=HTMLResponse)
    def tasks_index(request: Request) -> HTMLResponse:
        summaries: list[TaskInspectionSummary] = []
        error = ""
        if tasks_file.exists():
            try:
                summaries = list_task_inspection_summaries(tasks_file)
            except TaskQueueConfigError as exc:
                error = str(exc)
        return templates.TemplateResponse(
            request,
            "tasks.html",
            {
                "tasks_file": tasks_file,
                "tasks_file_exists": tasks_file.exists(),
                "summaries": summaries,
                "error": error,
            },
        )

    @router.get("/tasks/{task_id}", response_class=HTMLResponse)
    def task_detail(request: Request, task_id: str) -> HTMLResponse:
        safe_task_id = _validate_task_id(task_id)
        if not tasks_file.exists():
            raise HTTPException(status_code=404, detail="tasks.yaml not found")
        try:
            summary = build_task_inspection_summary(tasks_file=tasks_file, task_id=safe_task_id)
        except TaskQueueConfigError as exc:
            message = str(exc)
            status_code = 404 if "task id not found" in message else 400
            raise HTTPException(status_code=status_code, detail=message) from exc

        return templates.TemplateResponse(
            request,
            "task_detail.html",
            {
                "summary": summary,
                "prompt_preview": _prompt_preview(summary.prompt),
                "codex_cmd_configured": get_configured_codex_cmd() is not None,
            },
        )

    @router.post("/tasks/{task_id}/doctor-dry-run")
    def doctor_dry_run(task_id: str) -> RedirectResponse:
        safe_task_id = _validate_task_id(task_id)
        if not tasks_file.exists():
            raise HTTPException(status_code=404, detail="tasks.yaml not found")
        try:
            build_task_inspection_summary(tasks_file=tasks_file, task_id=safe_task_id)
        except TaskQueueConfigError as exc:
            message = str(exc)
            status_code = 404 if "task id not found" in message else 400
            raise HTTPException(status_code=status_code, detail=message) from exc
        try:
            job = start_background_job(
                project_root=project_root,
                action="doctor_dry_run",
                params={"task_id": safe_task_id},
                result_refs={
                    "task_id": safe_task_id,
                    "task_url": f"/tasks/{safe_task_id}",
                    "tasks_url": "/tasks",
                },
            )
        except ActiveJobExists as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except UnsupportedJobAction as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse(url=f"/jobs/{job.job_id}", status_code=303)

    @router.post("/tasks/{task_id}/pipeline-dry-run")
    def pipeline_dry_run(task_id: str) -> RedirectResponse:
        safe_task_id = _validate_task_id(task_id)
        if not tasks_file.exists():
            raise HTTPException(status_code=404, detail="tasks.yaml not found")
        try:
            build_task_inspection_summary(tasks_file=tasks_file, task_id=safe_task_id)
        except TaskQueueConfigError as exc:
            message = str(exc)
            status_code = 404 if "task id not found" in message else 400
            raise HTTPException(status_code=status_code, detail=message) from exc
        try:
            job = start_background_job(
                project_root=project_root,
                action="pipeline_dry_run",
                params={"task_id": safe_task_id},
                result_refs={
                    "task_id": safe_task_id,
                    "task_url": f"/tasks/{safe_task_id}",
                    "tasks_url": "/tasks",
                },
            )
        except ActiveJobExists as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except UnsupportedJobAction as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse(url=f"/jobs/{job.job_id}", status_code=303)

    @router.post("/tasks/{task_id}/doctor-real-run")
    def doctor_real_run(task_id: str) -> RedirectResponse:
        safe_task_id = _validate_task_id(task_id)
        if not tasks_file.exists():
            raise HTTPException(status_code=404, detail="tasks.yaml not found")
        try:
            build_task_inspection_summary(tasks_file=tasks_file, task_id=safe_task_id)
        except TaskQueueConfigError as exc:
            message = str(exc)
            status_code = 404 if "task id not found" in message else 400
            raise HTTPException(status_code=status_code, detail=message) from exc
        codex_cmd = get_configured_codex_cmd()
        if codex_cmd is None:
            raise HTTPException(
                status_code=400,
                detail="Set CODEX_CMD or AI_ORCHESTRATOR_CODEX_CMD before starting the web app.",
            )
        try:
            job = start_background_job(
                project_root=project_root,
                action="doctor_real_run",
                params={"task_id": safe_task_id, "codex_cmd": codex_cmd},
                result_refs={
                    "task_id": safe_task_id,
                    "task_url": f"/tasks/{safe_task_id}",
                    "tasks_url": "/tasks",
                },
            )
        except ActiveJobExists as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except UnsupportedJobAction as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse(url=f"/jobs/{job.job_id}", status_code=303)

    @router.post("/tasks/{task_id}/run-pipeline")
    async def run_pipeline(task_id: str, request: Request) -> RedirectResponse:
        safe_task_id = _validate_task_id(task_id)
        if not tasks_file.exists():
            raise HTTPException(status_code=404, detail="tasks.yaml not found")
        try:
            summary = build_task_inspection_summary(tasks_file=tasks_file, task_id=safe_task_id)
        except TaskQueueConfigError as exc:
            message = str(exc)
            status_code = 404 if "task id not found" in message else 400
            raise HTTPException(status_code=status_code, detail=message) from exc
        if not summary.enabled:
            raise HTTPException(status_code=400, detail="Task is disabled. Enable it before real pipeline execution.")
        codex_cmd = get_configured_codex_cmd()
        if codex_cmd is None:
            raise HTTPException(
                status_code=400,
                detail="Set CODEX_CMD or AI_ORCHESTRATOR_CODEX_CMD before starting the web app.",
            )
        if await _form_value(request, "confirm_real_pipeline") != "yes":
            raise HTTPException(status_code=400, detail="Explicit real pipeline confirmation is required.")
        try:
            job = start_background_job(
                project_root=project_root,
                action="run_pipeline_real",
                params={"task_id": safe_task_id, "codex_cmd": codex_cmd},
                result_refs={
                    "task_id": safe_task_id,
                    "task_url": f"/tasks/{safe_task_id}",
                    "tasks_url": "/tasks",
                    "pipelines_url": "/pipelines",
                    "runs_url": "/runs",
                },
            )
        except ActiveJobExists as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except UnsupportedJobAction as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse(url=f"/jobs/{job.job_id}", status_code=303)

    @router.post("/tasks/{task_id}/enable")
    def enable_task(task_id: str) -> RedirectResponse:
        safe_task_id = _validate_task_id(task_id)
        if not tasks_file.exists():
            raise HTTPException(status_code=404, detail="tasks.yaml not found")
        try:
            set_task_enabled(tasks_file=tasks_file, task_id=safe_task_id, enabled=True)
        except TaskQueueConfigError as exc:
            message = str(exc)
            status_code = 404 if "task id not found" in message else 400
            raise HTTPException(status_code=status_code, detail=message) from exc
        return RedirectResponse(url=f"/tasks/{safe_task_id}", status_code=303)

    @router.post("/tasks/{task_id}/disable")
    def disable_task(task_id: str) -> RedirectResponse:
        safe_task_id = _validate_task_id(task_id)
        if not tasks_file.exists():
            raise HTTPException(status_code=404, detail="tasks.yaml not found")
        try:
            set_task_enabled(tasks_file=tasks_file, task_id=safe_task_id, enabled=False)
        except TaskQueueConfigError as exc:
            message = str(exc)
            status_code = 404 if "task id not found" in message else 400
            raise HTTPException(status_code=status_code, detail=message) from exc
        return RedirectResponse(url=f"/tasks/{safe_task_id}", status_code=303)

    return router


def _validate_task_id(task_id: str) -> str:
    if not task_id or task_id in {".", ".."}:
        raise HTTPException(status_code=404, detail="task not found")
    if "/" in task_id or "\\" in task_id:
        raise HTTPException(status_code=404, detail="task not found")
    if Path(task_id).is_absolute() or ".." in PurePath(task_id).parts:
        raise HTTPException(status_code=404, detail="task not found")
    if not TASK_ID_PATTERN.fullmatch(task_id):
        raise HTTPException(status_code=404, detail="task not found")
    return task_id


def _prompt_preview(prompt: str) -> dict[str, object]:
    clipped = len(prompt) > PROMPT_PREVIEW_LIMIT
    return {
        "content": prompt[:PROMPT_PREVIEW_LIMIT],
        "clipped": clipped,
    }


async def _form_value(request: Request, key: str) -> str:
    body = (await request.body()).decode("utf-8")
    values = parse_qs(body)
    items = values.get(key)
    if items:
        return items[0]
    return ""
