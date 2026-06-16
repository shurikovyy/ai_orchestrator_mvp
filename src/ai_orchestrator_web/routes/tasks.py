"""Read-only task queue web routes."""

from __future__ import annotations

from pathlib import Path, PurePath

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from ai_orchestrator.task_inspection import (
    TaskInspectionSummary,
    build_task_inspection_summary,
    list_task_inspection_summaries,
)
from ai_orchestrator.task_queue import TaskQueueConfigError


PROMPT_PREVIEW_LIMIT = 4000


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
            },
        )

    return router


def _validate_task_id(task_id: str) -> str:
    if not task_id or task_id in {".", ".."}:
        raise HTTPException(status_code=404, detail="task not found")
    if "/" in task_id or "\\" in task_id:
        raise HTTPException(status_code=404, detail="task not found")
    if Path(task_id).is_absolute() or ".." in PurePath(task_id).parts:
        raise HTTPException(status_code=404, detail="task not found")
    return task_id


def _prompt_preview(prompt: str) -> dict[str, object]:
    clipped = len(prompt) > PROMPT_PREVIEW_LIMIT
    return {
        "content": prompt[:PROMPT_PREVIEW_LIMIT],
        "clipped": clipped,
    }
