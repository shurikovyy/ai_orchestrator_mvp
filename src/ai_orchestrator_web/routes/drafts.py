"""Read-only task draft web routes."""

from __future__ import annotations

from pathlib import Path, PurePath

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from ai_orchestrator.task_draft_inspection import (
    TaskDraftInspectionSummary,
    build_task_draft_inspection_summary,
    list_task_draft_summaries,
)


ARTIFACT_PREVIEW_LIMIT = 4000
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


def _validate_draft_id(draft_id: str) -> str:
    if not draft_id or draft_id in {".", ".."}:
        raise HTTPException(status_code=404, detail="task draft not found")
    if "/" in draft_id or "\\" in draft_id:
        raise HTTPException(status_code=404, detail="task draft not found")
    if Path(draft_id).is_absolute() or ".." in PurePath(draft_id).parts:
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
