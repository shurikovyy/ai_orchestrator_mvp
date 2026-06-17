"""Launch the local AI Orchestrator web dashboard."""

from __future__ import annotations

import argparse
from pathlib import Path

from .config import WebConfig


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ai_orchestrator_web",
        description="Run the local AI Orchestrator web dashboard.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind.")
    parser.add_argument("--port", type=int, default=8765, help="Port to bind.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = WebConfig(project_root=Path.cwd().resolve(), host=args.host, port=args.port)

    from .app import create_app
    import uvicorn

    print(f"AI Orchestrator Web MVP: http://{config.host}:{config.port}")
    uvicorn.run(create_app(config.project_root), host=config.host, port=config.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
