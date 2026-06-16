"""Configuration helpers for the local web dashboard."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class WebConfig:
    """Small launch-time config for the local web MVP."""

    project_root: Path
    host: str = "127.0.0.1"
    port: int = 8765


def get_default_config(project_root: Path | None = None) -> WebConfig:
    root = Path.cwd() if project_root is None else Path(project_root)
    return WebConfig(project_root=root.resolve())
