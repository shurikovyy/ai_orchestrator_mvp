"""Read-only run lifecycle web routes."""

from __future__ import annotations

from pathlib import Path, PurePath

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from ai_orchestrator.run_inspection import list_run_status_summaries
from ai_orchestrator.run_status import RunStatusSummary, build_run_status_summary


ARTIFACT_PREVIEW_LIMIT = 4000
RUN_PREVIEW_ARTIFACTS = (
    ("final_report", "final_report.md"),
    ("review_packet", "REVIEW_PACKET.md"),
    ("review_findings_markdown", "REVIEW_FINDINGS.md"),
    ("risk_classification_markdown", "RISK_CLASSIFICATION.md"),
    ("review_arbitration_markdown", "REVIEW_ARBITRATION.md"),
    ("review_decision_md", "REVIEW_DECISION.md"),
    ("apply_report", "APPLY_REPORT.md"),
    ("acceptance", "ACCEPTANCE.md"),
)


def create_runs_router(*, project_root: Path, templates: Jinja2Templates) -> APIRouter:
    router = APIRouter()
    runs_dir = project_root / ".runs"

    @router.get("/runs", response_class=HTMLResponse)
    def runs_index(request: Request) -> HTMLResponse:
        summaries = list_run_status_summaries(runs_dir=runs_dir)
        return templates.TemplateResponse(
            request,
            "runs.html",
            {"runs_dir": runs_dir, "summaries": summaries},
        )

    @router.get("/runs/{run_id}", response_class=HTMLResponse)
    def run_detail(request: Request, run_id: str) -> HTMLResponse:
        safe_run_id = _validate_id(run_id, "run not found")
        try:
            summary = build_run_status_summary(run_id=safe_run_id, runs_dir=runs_dir)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return templates.TemplateResponse(
            request,
            "run_detail.html",
            {"summary": summary, "artifact_previews": _artifact_previews(summary)},
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


def _artifact_previews(summary: RunStatusSummary) -> list[dict[str, object]]:
    previews: list[dict[str, object]] = []
    for artifact_key, label in RUN_PREVIEW_ARTIFACTS:
        path = Path(summary.artifacts[artifact_key])
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
