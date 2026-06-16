from __future__ import annotations

from pathlib import Path

from ai_orchestrator.pipeline_status import PipelineStatusSummary, build_pipeline_status_summary
from ai_orchestrator.run_status import RunStatusSummary, build_run_status_summary


def list_run_status_summaries(*, runs_dir: str | Path = ".runs") -> list[RunStatusSummary]:
    runs_root = Path(runs_dir)
    if not runs_root.exists():
        return []
    if not runs_root.is_dir():
        raise ValueError(f"runs path is not a directory: {runs_root}")
    summaries: list[RunStatusSummary] = []
    for run_dir in sorted((path for path in runs_root.iterdir() if _is_run_dir(path)), key=lambda path: path.name):
        summaries.append(build_run_status_summary(run_id=run_dir.name, runs_dir=runs_root))
    return summaries


def list_pipeline_status_summaries(*, runs_dir: str | Path = ".runs") -> list[PipelineStatusSummary]:
    runs_root = Path(runs_dir)
    pipelines_root = runs_root / "pipelines"
    if not pipelines_root.exists():
        return []
    if not pipelines_root.is_dir():
        raise ValueError(f"pipelines path is not a directory: {pipelines_root}")
    summaries: list[PipelineStatusSummary] = []
    for pipeline_dir in sorted(
        (path for path in pipelines_root.iterdir() if _is_pipeline_dir(path)),
        key=lambda path: path.name,
    ):
        summaries.append(build_pipeline_status_summary(pipeline_id=pipeline_dir.name, runs_dir=runs_root))
    return summaries


def _is_run_dir(path: Path) -> bool:
    return path.is_dir() and path.name != "pipelines" and (path / "state.json").exists()


def _is_pipeline_dir(path: Path) -> bool:
    return path.is_dir() and (path / "pipeline_state.json").exists()
