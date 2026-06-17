"""Read-only task draft web routes."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path, PurePath
import re
from urllib.parse import parse_qs
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ai_orchestrator.task_draft_inspection import (
    TaskDraftInspectionSummary,
    build_task_draft_inspection_summary,
    list_task_draft_summaries,
)
from ai_orchestrator_web.jobs.runner import ActiveJobExists, start_background_job
from ai_orchestrator_web.jobs.actions import UnsupportedJobAction
from ai_orchestrator_web.jobs.store import has_active_job


ARTIFACT_PREVIEW_LIMIT = 4000
MAX_RAW_REQUEST_CHARS = 50_000
MAX_TITLE_CHARS = 200
TASK_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")
RISK_LEVELS = ("unknown", "low", "medium", "high", "critical")
PROMPT_LANGUAGES = ("ru", "en")
PREVIEW_ARTIFACTS = (
    ("raw_request", "raw_request.md"),
    ("task_draft", "task_draft.yaml"),
    ("codex_prompt", "codex_prompt.md"),
    ("task_review", "task_review.md"),
    ("validator_report_md", "task_draft_validator_report.md"),
)


def create_drafts_router(*, project_root: Path, templates: Jinja2Templates) -> APIRouter:
    router = APIRouter()
    drafts_dir = project_root / ".task_drafts"

    @router.get("/drafts", response_class=HTMLResponse)
    def drafts_index(request: Request) -> HTMLResponse:
        summaries = list_task_draft_summaries(drafts_dir=drafts_dir)
        return templates.TemplateResponse(
            request,
            "drafts.html",
            {"drafts_dir": drafts_dir, "summaries": summaries},
        )

    @router.get("/drafts/new", response_class=HTMLResponse)
    def new_draft_request(request: Request) -> HTMLResponse:
        return _render_new_draft_form(request=request, templates=templates)

    @router.post("/drafts/create", response_model=None)
    async def create_draft_request(request: Request) -> HTMLResponse | RedirectResponse:
        form = await _parse_form(request)
        values, error = _validated_create_form(form)
        if error:
            return _render_new_draft_form(
                request=request,
                templates=templates,
                values=form,
                error=error,
                status_code=400,
            )
        if has_active_job(project_root.resolve()):
            raise HTTPException(status_code=409, detail="another job is already queued or running")

        raw_request_path = _write_raw_request(
            drafts_dir=drafts_dir,
            raw_request=values["raw_request"],
        )
        params = {
            "request_path": str(raw_request_path),
            "title": values.get("title", ""),
            "task_id": values.get("task_id", ""),
            "risk_level": values.get("risk_level", ""),
            "prompt_language": values.get("prompt_language", ""),
        }
        try:
            job = start_background_job(
                project_root=project_root,
                action="draft_task_scaffold",
                params=params,
            )
        except ActiveJobExists as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return RedirectResponse(url=f"/jobs/{job.job_id}", status_code=303)

    @router.post("/drafts/{draft_id}/validate")
    def validate_draft(draft_id: str) -> RedirectResponse:
        safe_draft_id = _validate_draft_id(draft_id)
        draft_dir = drafts_dir / safe_draft_id
        if not draft_dir.is_dir():
            raise HTTPException(status_code=404, detail=f"task draft not found: {safe_draft_id}")
        try:
            job = start_background_job(
                project_root=project_root,
                action="validate_task_draft",
                params={"draft_id": safe_draft_id},
                result_refs={"draft_id": safe_draft_id},
            )
        except ActiveJobExists as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except UnsupportedJobAction as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse(url=f"/jobs/{job.job_id}", status_code=303)

    @router.get("/drafts/{draft_id}", response_class=HTMLResponse)
    def draft_detail(request: Request, draft_id: str) -> HTMLResponse:
        safe_draft_id = _validate_draft_id(draft_id)
        try:
            summary = build_task_draft_inspection_summary(
                draft_id=safe_draft_id,
                drafts_dir=drafts_dir,
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        return templates.TemplateResponse(
            request,
            "draft_detail.html",
            {
                "summary": summary,
                "artifact_previews": _artifact_previews(summary),
            },
        )

    return router


def _render_new_draft_form(
    *,
    request: Request,
    templates: Jinja2Templates,
    values: dict[str, str] | None = None,
    error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    form_values = {
        "title": "",
        "task_id": "",
        "risk_level": "unknown",
        "prompt_language": "ru",
        "raw_request": "",
    }
    if values:
        form_values.update(values)
    return templates.TemplateResponse(
        request,
        "draft_new.html",
        {
            "error": error,
            "values": form_values,
            "risk_levels": RISK_LEVELS,
            "prompt_languages": PROMPT_LANGUAGES,
            "max_raw_request_chars": MAX_RAW_REQUEST_CHARS,
            "max_title_chars": MAX_TITLE_CHARS,
        },
        status_code=status_code,
    )


async def _parse_form(request: Request) -> dict[str, str]:
    body = (await request.body()).decode("utf-8")
    parsed = parse_qs(body, keep_blank_values=True)
    return {key: values[0] if values else "" for key, values in parsed.items()}


def _validated_create_form(form: dict[str, str]) -> tuple[dict[str, str], str | None]:
    raw_request = form.get("raw_request", "")
    if not raw_request.strip():
        return {}, "Raw request is required."
    if len(raw_request) > MAX_RAW_REQUEST_CHARS:
        return {}, f"Raw request must be {MAX_RAW_REQUEST_CHARS} characters or fewer."

    title = form.get("title", "").strip()
    if len(title) > MAX_TITLE_CHARS:
        return {}, f"Title must be {MAX_TITLE_CHARS} characters or fewer."

    task_id_raw = form.get("task_id", "")
    task_id = task_id_raw.strip()
    if task_id_raw and task_id_raw != task_id:
        return {}, "Task ID must not contain leading or trailing whitespace."
    if task_id and not _is_safe_task_id(task_id):
        return {}, "Task ID may contain only letters, numbers, dot, dash, and underscore."

    risk_level = form.get("risk_level", "unknown").strip() or "unknown"
    if risk_level not in RISK_LEVELS:
        return {}, "Risk level is not supported."

    prompt_language = form.get("prompt_language", "ru").strip() or "ru"
    if prompt_language not in PROMPT_LANGUAGES:
        return {}, "Prompt language is not supported."

    return {
        "raw_request": raw_request,
        "title": title,
        "task_id": task_id,
        "risk_level": risk_level,
        "prompt_language": prompt_language,
    }, None


def _is_safe_task_id(task_id: str) -> bool:
    if task_id in {".", ".."}:
        return False
    if "/" in task_id or "\\" in task_id:
        return False
    if Path(task_id).is_absolute() or ".." in PurePath(task_id).parts:
        return False
    return bool(TASK_ID_PATTERN.fullmatch(task_id))


def _write_raw_request(*, drafts_dir: Path, raw_request: str) -> Path:
    raw_requests_dir = drafts_dir / "raw_requests"
    raw_requests_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = raw_requests_dir / f"web_request_{timestamp}_{uuid4().hex[:6]}.md"
    path.write_text(raw_request, encoding="utf-8")
    return path.resolve()


def _validate_draft_id(draft_id: str) -> str:
    if not draft_id or draft_id in {".", ".."}:
        raise HTTPException(status_code=404, detail="task draft not found")
    if "/" in draft_id or "\\" in draft_id:
        raise HTTPException(status_code=404, detail="task draft not found")
    if Path(draft_id).is_absolute() or ".." in PurePath(draft_id).parts:
        raise HTTPException(status_code=404, detail="task draft not found")
    if not TASK_ID_PATTERN.fullmatch(draft_id):
        raise HTTPException(status_code=404, detail="task draft not found")
    return draft_id


def _artifact_previews(summary: TaskDraftInspectionSummary) -> list[dict[str, object]]:
    previews: list[dict[str, object]] = []
    for artifact_key, label in PREVIEW_ARTIFACTS:
        path = summary.paths[artifact_key]
        exists = summary.exists[artifact_key]
        content = ""
        clipped = False
        if exists:
            text = path.read_text(encoding="utf-8-sig", errors="replace")
            clipped = len(text) > ARTIFACT_PREVIEW_LIMIT
            content = text[:ARTIFACT_PREVIEW_LIMIT]
        previews.append(
            {
                "key": artifact_key,
                "label": label,
                "path": path,
                "exists": exists,
                "content": content,
                "clipped": clipped,
            }
        )
    return previews
