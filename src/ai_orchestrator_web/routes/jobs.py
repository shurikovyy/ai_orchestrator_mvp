"""Allowlisted local job routes."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ai_orchestrator_web.jobs.actions import UnsupportedJobAction, list_allowed_actions
from ai_orchestrator_web.jobs.runner import ActiveJobExists, start_background_job
from ai_orchestrator_web.jobs.store import (
    job_stderr_path,
    job_stdout_path,
    list_jobs,
    load_job,
    validate_job_id,
)


LOG_PREVIEW_LIMIT = 8000


def create_jobs_router(*, project_root: Path, templates: Jinja2Templates) -> APIRouter:
    router = APIRouter()
    root = project_root.resolve()

    @router.get("/jobs", response_class=HTMLResponse)
    def jobs_index(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "jobs.html",
            {
                "jobs": list_jobs(root),
                "allowed_actions": list_allowed_actions(),
            },
        )

    @router.get("/jobs/{job_id}", response_class=HTMLResponse)
    def job_detail(request: Request, job_id: str) -> HTMLResponse:
        safe_job_id = _validate_job_id_or_404(job_id)
        try:
            job = load_job(root, safe_job_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return templates.TemplateResponse(
            request,
            "job_detail.html",
            {
                "job": job,
                "stdout_preview": _log_preview(job_stdout_path(root, safe_job_id)),
                "stderr_preview": _log_preview(job_stderr_path(root, safe_job_id)),
            },
        )

    @router.post("/jobs/start")
    async def start_job(request: Request) -> RedirectResponse:
        action = await _extract_form_action(request)
        try:
            job = start_background_job(project_root=root, action=action)
        except UnsupportedJobAction as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ActiveJobExists as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return RedirectResponse(url=f"/jobs/{job.job_id}", status_code=303)

    return router


def _validate_job_id_or_404(job_id: str) -> str:
    try:
        return validate_job_id(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


async def _extract_form_action(request: Request) -> str:
    body = (await request.body()).decode("utf-8")
    values = parse_qs(body)
    action_values = values.get("action")
    if action_values:
        return action_values[0]
    return request.query_params.get("action", "")


def _log_preview(path: Path) -> dict[str, object]:
    if not path.exists():
        return {"path": path, "exists": False, "content": "", "clipped": False}
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    return {
        "path": path,
        "exists": True,
        "content": text[:LOG_PREVIEW_LIMIT],
        "clipped": len(text) > LOG_PREVIEW_LIMIT,
    }
