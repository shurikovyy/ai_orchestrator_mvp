from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ai_orchestrator.backends import Backend, get_backend
from ai_orchestrator.engine import TaskExecutionEngine
from ai_orchestrator.schemas import RunState, TaskSpec
from ai_orchestrator.task_queue import ResolvedTaskDefinition


@dataclass(frozen=True)
class RunCommandConfig:
    task: TaskSpec
    backend_name: str
    runs_dir: Path
    codex_cmd: str | None = None
    verbose: bool = False
    stream_codex_output: bool = False


def build_run_config_from_resolved_task(
    resolved_task: ResolvedTaskDefinition,
    *,
    runs_dir: str | Path,
) -> RunCommandConfig:
    return RunCommandConfig(
        task=resolved_task.to_task_spec(),
        backend_name=resolved_task.backend,
        runs_dir=Path(runs_dir),
        codex_cmd=resolved_task.codex_cmd,
        verbose=resolved_task.verbose,
        stream_codex_output=resolved_task.stream_codex_output,
    )


def build_backend(config: RunCommandConfig) -> Backend:
    if config.backend_name in {"codex", "codex_cli"} and (config.codex_cmd or config.stream_codex_output):
        from ai_orchestrator.backends.codex_cli import CodexCliBackend

        return CodexCliBackend(codex_cmd=config.codex_cmd, stream_output=config.stream_codex_output)
    return get_backend(config.backend_name)


def execute_run(config: RunCommandConfig) -> tuple[RunState, Backend]:
    backend = build_backend(config)
    engine = TaskExecutionEngine(backend=backend, runs_dir=config.runs_dir, verbose=config.verbose)
    state = engine.run(config.task)
    return state, backend


def get_run_artifact_paths(runs_dir: str | Path, run_id: str) -> tuple[Path, Path, Path]:
    run_dir = Path(runs_dir) / run_id
    return run_dir / "final_report.md", run_dir / "REVIEW_PACKET.md", run_dir / "state.json"
