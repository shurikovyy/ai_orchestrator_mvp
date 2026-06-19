"""Run lifecycle web routes, including post-run analysis actions."""

from __future__ import annotations

from pathlib import Path, PurePath
import re

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ai_orchestrator.run_inspection import list_run_status_summaries
from ai_orchestrator.run_status import RunStatusSummary, build_run_status_summary
from ai_orchestrator_web.jobs.actions import UnsupportedJobAction
from ai_orchestrator_web.jobs.runner import ActiveJobExists, start_background_job
from ai_orchestrator_web.reviewer_prompt_inspection import (
    ReviewerPromptNotFound,
    build_reviewer_prompt_detail,
    build_reviewer_prompt_index,
)


ARTIFACT_PREVIEW_LIMIT = 4000
RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")
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

    @router.get("/runs/{run_id}/reviewer-prompts", response_class=HTMLResponse)
    def reviewer_prompts_index(request: Request, run_id: str) -> HTMLResponse:
        safe_run_id = _validate_id(run_id, "run not found")
        _ensure_run_exists(runs_dir=runs_dir, run_id=safe_run_id)
        index = build_reviewer_prompt_index(run_id=safe_run_id, runs_dir=runs_dir)
        return templates.TemplateResponse(
            request,
            "reviewer_prompts.html",
            {"index": index},
        )

    @router.get("/runs/{run_id}/reviewer-prompts/{profile}", response_class=HTMLResponse)
    def reviewer_prompt_detail(request: Request, run_id: str, profile: str) -> HTMLResponse:
        safe_run_id = _validate_id(run_id, "run not found")
        safe_profile = _validate_id(profile, "reviewer prompt not found")
        _ensure_run_exists(runs_dir=runs_dir, run_id=safe_run_id)
        try:
            detail = build_reviewer_prompt_detail(run_id=safe_run_id, runs_dir=runs_dir, profile=safe_profile)
        except ReviewerPromptNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return templates.TemplateResponse(
            request,
            "reviewer_prompt_detail.html",
            {"detail": detail},
        )

    @router.post("/runs/{run_id}/classify")
    def classify_run(run_id: str) -> RedirectResponse:
        safe_run_id = _validate_id(run_id, "run not found")
        if not runs_dir.exists():
            raise HTTPException(status_code=404, detail="runs directory not found")
        if not (runs_dir / safe_run_id).is_dir():
            raise HTTPException(status_code=404, detail=f"run not found: {safe_run_id}")
        try:
            job = start_background_job(
                project_root=project_root,
                action="classify_run",
                params={"run_id": safe_run_id},
                result_refs={
                    "run_id": safe_run_id,
                    "run_url": f"/runs/{safe_run_id}",
                    "runs_url": "/runs",
                    "pipelines_url": "/pipelines",
                },
            )
        except ActiveJobExists as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except UnsupportedJobAction as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse(url=f"/jobs/{job.job_id}", status_code=303)

    @router.post("/runs/{run_id}/review-checks")
    def run_review_checks(run_id: str) -> RedirectResponse:
        safe_run_id = _validate_id(run_id, "run not found")
        if not runs_dir.exists():
            raise HTTPException(status_code=404, detail="runs directory not found")
        if not (runs_dir / safe_run_id).is_dir():
            raise HTTPException(status_code=404, detail=f"run not found: {safe_run_id}")
        try:
            job = start_background_job(
                project_root=project_root,
                action="run_review_checks",
                params={"run_id": safe_run_id},
                result_refs={
                    "run_id": safe_run_id,
                    "run_url": f"/runs/{safe_run_id}",
                    "runs_url": "/runs",
                    "pipelines_url": "/pipelines",
                },
            )
        except ActiveJobExists as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except UnsupportedJobAction as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse(url=f"/jobs/{job.job_id}", status_code=303)

    @router.post("/runs/{run_id}/prepare-review")
    def prepare_review(run_id: str) -> RedirectResponse:
        safe_run_id = _validate_id(run_id, "run not found")
        if not runs_dir.exists():
            raise HTTPException(status_code=404, detail="runs directory not found")
        if not (runs_dir / safe_run_id).is_dir():
            raise HTTPException(status_code=404, detail=f"run not found: {safe_run_id}")
        try:
            job = start_background_job(
                project_root=project_root,
                action="prepare_review",
                params={"run_id": safe_run_id},
                result_refs={
                    "run_id": safe_run_id,
                    "run_url": f"/runs/{safe_run_id}",
                    "runs_url": "/runs",
                    "pipelines_url": "/pipelines",
                },
            )
        except ActiveJobExists as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except UnsupportedJobAction as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse(url=f"/jobs/{job.job_id}", status_code=303)

    return router


def _validate_id(identifier: str, message: str) -> str:
    if not identifier or identifier in {".", ".."}:
        raise HTTPException(status_code=404, detail=message)
    if "/" in identifier or "\\" in identifier:
        raise HTTPException(status_code=404, detail=message)
    if Path(identifier).is_absolute() or ".." in PurePath(identifier).parts:
        raise HTTPException(status_code=404, detail=message)
    if not RUN_ID_PATTERN.fullmatch(identifier):
        raise HTTPException(status_code=404, detail=message)
    return identifier


def _ensure_run_exists(*, runs_dir: Path, run_id: str) -> None:
    if not runs_dir.exists():
        raise HTTPException(status_code=404, detail="runs directory not found")
    if not (runs_dir / run_id).is_dir():
        raise HTTPException(status_code=404, detail=f"run not found: {run_id}")


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
