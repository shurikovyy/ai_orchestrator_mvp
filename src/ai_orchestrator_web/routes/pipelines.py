"""Read-only pipeline lifecycle web routes."""

from __future__ import annotations

from pathlib import Path, PurePath

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from ai_orchestrator.pipeline_status import PipelineStatusSummary, build_pipeline_status_summary
from ai_orchestrator.run_inspection import list_pipeline_status_summaries


PIPELINE_REPORT_PREVIEW_LIMIT = 4000


def create_pipelines_router(*, project_root: Path, templates: Jinja2Templates) -> APIRouter:
    router = APIRouter()
    runs_dir = project_root / ".runs"
    pipelines_dir = runs_dir / "pipelines"

    @router.get("/pipelines", response_class=HTMLResponse)
    def pipelines_index(request: Request) -> HTMLResponse:
        summaries = list_pipeline_status_summaries(runs_dir=runs_dir)
        return templates.TemplateResponse(
            request,
            "pipelines.html",
            {"pipelines_dir": pipelines_dir, "summaries": summaries},
        )

    @router.get("/pipelines/{pipeline_id}", response_class=HTMLResponse)
    def pipeline_detail(request: Request, pipeline_id: str) -> HTMLResponse:
        safe_pipeline_id = _validate_id(pipeline_id, "pipeline not found")
        try:
            summary = build_pipeline_status_summary(pipeline_id=safe_pipeline_id, runs_dir=runs_dir)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return templates.TemplateResponse(
            request,
            "pipeline_detail.html",
            {
                "summary": summary,
                "pipeline_report_preview": _pipeline_report_preview(summary),
            },
        )

    return router


def _validate_id(identifier: str, message: str) -> str:
    if not identifier or identifier in {".", ".."}:
        raise HTTPException(status_code=404, detail=message)
    if "/" in identifier or "\\" in identifier:
        raise HTTPException(status_code=404, detail=message)
    if Path(identifier).is_absolute() or ".." in PurePath(identifier).parts:
        raise HTTPException(status_code=404, detail=message)
    return identifier


def _pipeline_report_preview(summary: PipelineStatusSummary) -> dict[str, object]:
    path = Path(summary.artifacts["pipeline_report"])
    exists = summary.exists["pipeline_report"]
    content = ""
    clipped = False
    if exists:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        clipped = len(text) > PIPELINE_REPORT_PREVIEW_LIMIT
        content = text[:PIPELINE_REPORT_PREVIEW_LIMIT]
    return {"path": path, "exists": exists, "content": content, "clipped": clipped}
