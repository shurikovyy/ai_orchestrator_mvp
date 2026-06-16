"""FastAPI app factory for the read-only local web MVP."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .project_status import get_current_project_status
from .routes.drafts import create_drafts_router
from .routes.tasks import create_tasks_router


PACKAGE_DIR = Path(__file__).resolve().parent


def create_app(project_root: Path | str | None = None) -> FastAPI:
    root = (Path.cwd() if project_root is None else Path(project_root)).resolve()
    web_app = FastAPI(title="AI Orchestrator Web MVP")
    templates = Jinja2Templates(directory=str(PACKAGE_DIR / "templates"))
    web_app.mount(
        "/static",
        StaticFiles(directory=str(PACKAGE_DIR / "static")),
        name="static",
    )

    @web_app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "app": "ai_orchestrator_web"}

    @web_app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request) -> HTMLResponse:
        status = get_current_project_status(root)
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {"status": status},
        )

    web_app.include_router(create_drafts_router(project_root=root, templates=templates))
    web_app.include_router(create_tasks_router(project_root=root, templates=templates))

    return web_app


app = create_app()
