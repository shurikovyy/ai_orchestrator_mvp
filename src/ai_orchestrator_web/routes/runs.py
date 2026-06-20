"""Run lifecycle web routes, including post-run analysis actions."""

from __future__ import annotations

from pathlib import Path, PurePath
import re
from urllib.parse import parse_qs

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ai_orchestrator.run_inspection import list_run_status_summaries
from ai_orchestrator.run_status import RunStatusSummary, build_run_status_summary
from ai_orchestrator_web.arbitration_inspection import (
    ArbitrationFindingNotFound,
    build_arbitration_detail,
    build_arbitration_format_helper,
    build_arbitration_index,
    validate_arbitration_submission,
    write_arbitration_input_file,
)
from ai_orchestrator_web.findings_inspection import (
    FindingNotFound,
    build_finding_detail,
    build_findings_index,
    build_findings_format_helper,
    validate_findings_submission,
    write_findings_input_file,
)
from ai_orchestrator_web.jobs.actions import UnsupportedJobAction
from ai_orchestrator_web.jobs.runner import ActiveJobExists, start_background_job
from ai_orchestrator_web.jobs.store import has_active_job
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

    @router.get("/runs/{run_id}/findings", response_class=HTMLResponse)
    def findings_index(request: Request, run_id: str) -> HTMLResponse:
        safe_run_id = _validate_id(run_id, "run not found")
        _ensure_run_exists(runs_dir=runs_dir, run_id=safe_run_id)
        index = build_findings_index(run_id=safe_run_id, runs_dir=runs_dir)
        return templates.TemplateResponse(
            request,
            "findings.html",
            {"index": index},
        )

    @router.get("/runs/{run_id}/findings/new", response_class=HTMLResponse)
    def new_findings(request: Request, run_id: str) -> HTMLResponse:
        safe_run_id = _validate_id(run_id, "run not found")
        _ensure_run_exists(runs_dir=runs_dir, run_id=safe_run_id)
        return _render_findings_form(
            request=request,
            templates=templates,
            runs_dir=runs_dir,
            run_id=safe_run_id,
        )

    @router.post("/runs/{run_id}/findings/record", response_model=None)
    async def record_findings(request: Request, run_id: str) -> HTMLResponse | RedirectResponse:
        safe_run_id = _validate_id(run_id, "run not found")
        _ensure_run_exists(runs_dir=runs_dir, run_id=safe_run_id)
        form = await _parse_form(request)
        if (runs_dir / safe_run_id / "REVIEW_FINDINGS.json").is_file():
            return _render_findings_form(
                request=request,
                templates=templates,
                runs_dir=runs_dir,
                run_id=safe_run_id,
                values=form,
                error=OVERWRITE_NOT_SUPPORTED_MESSAGE,
                status_code=400,
            )
        if form.get("confirm_record_findings") != "yes":
            return _render_findings_form(
                request=request,
                templates=templates,
                runs_dir=runs_dir,
                run_id=safe_run_id,
                values=form,
                error="Explicit record findings confirmation is required.",
                status_code=400,
            )
        profile = form.get("profile", "")
        submission, error = validate_findings_submission(
            run_id=safe_run_id,
            findings_json=form.get("findings_json", ""),
            profile=profile,
        )
        if error or submission is None:
            return _render_findings_form(
                request=request,
                templates=templates,
                runs_dir=runs_dir,
                run_id=safe_run_id,
                values=form,
                error=error or "Findings JSON could not be validated.",
                status_code=400,
            )
        if has_active_job(project_root.resolve()):
            raise HTTPException(status_code=409, detail="another job is already queued or running")
        findings_input_id, _findings_input_path = write_findings_input_file(
            project_root=project_root,
            run_id=safe_run_id,
            normalized_json=submission.normalized_json,
        )
        try:
            job = start_background_job(
                project_root=project_root,
                action="record_findings",
                params={
                    "run_id": safe_run_id,
                    "findings_input_id": findings_input_id,
                    "profile": submission.profile or "",
                },
                result_refs={
                    "run_id": safe_run_id,
                    "run_url": f"/runs/{safe_run_id}",
                    "runs_url": "/runs",
                    "findings_url": f"/runs/{safe_run_id}/findings",
                },
            )
        except ActiveJobExists as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except UnsupportedJobAction as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse(url=f"/jobs/{job.job_id}", status_code=303)

    @router.get("/runs/{run_id}/findings/{finding_id}", response_class=HTMLResponse)
    def finding_detail(request: Request, run_id: str, finding_id: str) -> HTMLResponse:
        safe_run_id = _validate_id(run_id, "run not found")
        safe_finding_id = _validate_id(finding_id, "review finding not found")
        _ensure_run_exists(runs_dir=runs_dir, run_id=safe_run_id)
        try:
            detail = build_finding_detail(run_id=safe_run_id, runs_dir=runs_dir, finding_id=safe_finding_id)
        except FindingNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return templates.TemplateResponse(
            request,
            "finding_detail.html",
            {"detail": detail},
        )

    @router.get("/runs/{run_id}/arbitration", response_class=HTMLResponse)
    def arbitration_index(request: Request, run_id: str) -> HTMLResponse:
        safe_run_id = _validate_id(run_id, "run not found")
        _ensure_run_exists(runs_dir=runs_dir, run_id=safe_run_id)
        index = build_arbitration_index(run_id=safe_run_id, runs_dir=runs_dir)
        return templates.TemplateResponse(
            request,
            "arbitration.html",
            {"index": index},
        )

    @router.get("/runs/{run_id}/arbitration/new", response_class=HTMLResponse)
    def new_arbitration(request: Request, run_id: str) -> HTMLResponse:
        safe_run_id = _validate_id(run_id, "run not found")
        _ensure_run_exists(runs_dir=runs_dir, run_id=safe_run_id)
        return _render_arbitration_form(
            request=request,
            templates=templates,
            runs_dir=runs_dir,
            run_id=safe_run_id,
        )

    @router.post("/runs/{run_id}/arbitration/record", response_model=None)
    async def record_arbitration(request: Request, run_id: str) -> HTMLResponse | RedirectResponse:
        safe_run_id = _validate_id(run_id, "run not found")
        _ensure_run_exists(runs_dir=runs_dir, run_id=safe_run_id)
        form = await _parse_form(request)
        run_dir = runs_dir / safe_run_id
        if not (run_dir / "REVIEW_FINDINGS.json").is_file():
            return _render_arbitration_form(
                request=request,
                templates=templates,
                runs_dir=runs_dir,
                run_id=safe_run_id,
                values=form,
                error=ARBITRATION_FINDINGS_REQUIRED_MESSAGE,
                status_code=400,
            )
        if (run_dir / "REVIEW_ARBITRATION.json").is_file():
            return _render_arbitration_form(
                request=request,
                templates=templates,
                runs_dir=runs_dir,
                run_id=safe_run_id,
                values=form,
                error=ARBITRATION_OVERWRITE_NOT_SUPPORTED_MESSAGE,
                status_code=400,
            )
        if form.get("confirm_record_arbitration") != "yes":
            return _render_arbitration_form(
                request=request,
                templates=templates,
                runs_dir=runs_dir,
                run_id=safe_run_id,
                values=form,
                error="Explicit record arbitration confirmation is required.",
                status_code=400,
            )
        submission, error = validate_arbitration_submission(
            run_id=safe_run_id,
            arbitration_json=form.get("arbitration_json", ""),
        )
        if error or submission is None:
            return _render_arbitration_form(
                request=request,
                templates=templates,
                runs_dir=runs_dir,
                run_id=safe_run_id,
                values=form,
                error=error or "Arbitration JSON could not be validated.",
                status_code=400,
            )
        if has_active_job(project_root.resolve()):
            raise HTTPException(status_code=409, detail="another job is already queued or running")
        arbitration_input_id, _arbitration_input_path = write_arbitration_input_file(
            project_root=project_root,
            run_id=safe_run_id,
            normalized_json=submission.normalized_json,
        )
        try:
            job = start_background_job(
                project_root=project_root,
                action="record_arbitration",
                params={
                    "run_id": safe_run_id,
                    "arbitration_input_id": arbitration_input_id,
                },
                result_refs={
                    "run_id": safe_run_id,
                    "run_url": f"/runs/{safe_run_id}",
                    "runs_url": "/runs",
                    "arbitration_url": f"/runs/{safe_run_id}/arbitration",
                },
            )
        except ActiveJobExists as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except UnsupportedJobAction as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse(url=f"/jobs/{job.job_id}", status_code=303)

    @router.get("/runs/{run_id}/arbitration/{finding_id}", response_class=HTMLResponse)
    def arbitration_detail(request: Request, run_id: str, finding_id: str) -> HTMLResponse:
        safe_run_id = _validate_id(run_id, "run not found")
        safe_finding_id = _validate_id(finding_id, "arbitrated finding not found")
        _ensure_run_exists(runs_dir=runs_dir, run_id=safe_run_id)
        try:
            detail = build_arbitration_detail(run_id=safe_run_id, runs_dir=runs_dir, finding_id=safe_finding_id)
        except ArbitrationFindingNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return templates.TemplateResponse(
            request,
            "arbitration_detail.html",
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


OVERWRITE_NOT_SUPPORTED_MESSAGE = (
    "Review findings already exist for this run. This web UI does not overwrite findings yet. "
    "Use the CLI with --force only if you deliberately want to replace them."
)

ARBITRATION_FINDINGS_REQUIRED_MESSAGE = (
    "Review findings are required before arbitration can be recorded. Record findings first."
)

ARBITRATION_OVERWRITE_NOT_SUPPORTED_MESSAGE = (
    "Review arbitration already exists for this run. This web UI does not overwrite arbitration yet. "
    "Use the CLI with --force only if you deliberately want to replace it."
)


def _render_findings_form(
    *,
    request: Request,
    templates: Jinja2Templates,
    runs_dir: Path,
    run_id: str,
    values: dict[str, str] | None = None,
    error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    index = build_findings_index(run_id=run_id, runs_dir=runs_dir)
    form_values = {"findings_json": "", "profile": ""}
    if values:
        form_values.update(values)
    return templates.TemplateResponse(
        request,
        "findings_new.html",
        {
            "run_id": run_id,
            "index": index,
            "values": form_values,
            "error": error,
            "overwrite_message": OVERWRITE_NOT_SUPPORTED_MESSAGE if index.json_exists else None,
            "format_helper": build_findings_format_helper(run_id=run_id),
        },
        status_code=status_code,
    )


def _render_arbitration_form(
    *,
    request: Request,
    templates: Jinja2Templates,
    runs_dir: Path,
    run_id: str,
    values: dict[str, str] | None = None,
    error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    index = build_arbitration_index(run_id=run_id, runs_dir=runs_dir)
    form_values = {"arbitration_json": ""}
    if values:
        form_values.update(values)
    return templates.TemplateResponse(
        request,
        "arbitration_new.html",
        {
            "run_id": run_id,
            "index": index,
            "values": form_values,
            "error": error,
            "findings_required_message": ARBITRATION_FINDINGS_REQUIRED_MESSAGE if not index.findings_exists else None,
            "overwrite_message": ARBITRATION_OVERWRITE_NOT_SUPPORTED_MESSAGE if index.arbitration_exists else None,
            "format_helper": build_arbitration_format_helper(run_id=run_id),
        },
        status_code=status_code,
    )


async def _parse_form(request: Request) -> dict[str, str]:
    body = (await request.body()).decode("utf-8")
    parsed = parse_qs(body, keep_blank_values=True)
    return {key: values[0] if values else "" for key, values in parsed.items()}


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
