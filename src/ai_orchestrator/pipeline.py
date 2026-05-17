from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from ai_orchestrator.task_queue import (
    ResolvedTaskDefinition,
    TaskQueueConfig,
    TaskQueueConfigError,
    load_task_queue_config,
    resolve_task_definition,
)
from ai_orchestrator.task_runner import build_run_config_from_resolved_task, execute_run, get_run_artifact_paths


class PipelineSelectedTask(BaseModel):
    task_id: str
    title: str | None = None
    enabled: bool = True


class PipelineTaskResult(BaseModel):
    task_id: str
    title: str | None = None
    status: Literal["approved", "failed"]
    run_id: str
    final_report: str
    review_packet: str | None = None
    state: str


class PipelineState(BaseModel):
    pipeline_id: str
    tasks_file: str
    status: Literal["running", "approved", "failed", "partial"]
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None
    continue_on_failure: bool = False
    dry_run: bool = False
    selected_tasks: list[PipelineSelectedTask] = Field(default_factory=list)
    skipped_task_ids: list[str] = Field(default_factory=list)
    not_run_task_ids: list[str] = Field(default_factory=list)
    tasks: list[PipelineTaskResult] = Field(default_factory=list)

    def save_json(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.model_dump_json(indent=2), encoding="utf-8")


@dataclass(frozen=True)
class SelectedPipelineTask:
    task_id: str
    title: str | None
    enabled: bool
    resolved_task: ResolvedTaskDefinition | None = None


@dataclass(frozen=True)
class PipelineSelection:
    tasks_file: Path
    selected_tasks: list[SelectedPipelineTask]

    @property
    def task_ids(self) -> list[str]:
        return [task.task_id for task in self.selected_tasks]

    @property
    def skipped_task_ids(self) -> list[str]:
        return [task.task_id for task in self.selected_tasks if not task.enabled]

    @property
    def tasks_to_run(self) -> list[ResolvedTaskDefinition]:
        return [task.resolved_task for task in self.selected_tasks if task.resolved_task is not None]

    @property
    def enabled_task_ids(self) -> list[str]:
        return [task.task_id for task in self.selected_tasks if task.enabled]


@dataclass(frozen=True)
class PipelinePlan:
    tasks_file: Path
    selected_tasks: list[SelectedPipelineTask]


@dataclass(frozen=True)
class PipelineRunResult:
    state: PipelineState
    pipeline_dir: Path
    pipeline_report: Path
    pipeline_state_path: Path


def generate_pipeline_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"pipeline_{timestamp}_{uuid4().hex[:6]}"


def _normalize_tasks_file_path(tasks_file: str | Path) -> Path:
    return Path(tasks_file).expanduser().resolve()


def select_pipeline_tasks(
    config: TaskQueueConfig,
    *,
    tasks_file: str | Path,
    from_task: str | None = None,
    only: list[str] | None = None,
    backend: str | None = None,
    codex_cmd: str | None = None,
    max_retries: int | None = None,
    verbose: bool | None = None,
    stream_codex_output: bool | None = None,
) -> PipelineSelection:
    if from_task and only:
        raise TaskQueueConfigError("--from-task and --only cannot be used together")

    declared_tasks = list(config.tasks)
    declared_ids = [task.id for task in declared_tasks]

    if from_task:
        if from_task not in declared_ids:
            raise TaskQueueConfigError(f"task id not found for --from-task: {from_task}")
        start_index = declared_ids.index(from_task)
        candidate_tasks = declared_tasks[start_index:]
    elif only:
        requested_ids = [task_id.strip() for task_id in only if task_id.strip()]
        missing_ids = [task_id for task_id in dict.fromkeys(requested_ids) if task_id not in declared_ids]
        if missing_ids:
            if len(missing_ids) == 1:
                raise TaskQueueConfigError(f"task id not found for --only: {missing_ids[0]}")
            raise TaskQueueConfigError("task ids not found for --only: " + ", ".join(missing_ids))
        requested_set = set(requested_ids)
        candidate_tasks = [task for task in declared_tasks if task.id in requested_set]
    else:
        candidate_tasks = declared_tasks

    normalized_tasks_file = _normalize_tasks_file_path(tasks_file)
    selected_tasks: list[SelectedPipelineTask] = []
    for task_definition in candidate_tasks:
        if not task_definition.enabled:
            selected_tasks.append(
                SelectedPipelineTask(
                    task_id=task_definition.id,
                    title=task_definition.title,
                    enabled=False,
                    resolved_task=None,
                )
            )
            continue

        resolved_task = resolve_task_definition(
            config,
            task_id=task_definition.id,
            tasks_file=normalized_tasks_file,
            backend=backend,
            codex_cmd=codex_cmd,
            max_retries=max_retries,
            verbose=verbose,
            stream_codex_output=stream_codex_output,
        )
        selected_tasks.append(
            SelectedPipelineTask(
                task_id=resolved_task.task_id,
                title=resolved_task.title,
                enabled=True,
                resolved_task=resolved_task,
            )
        )

    return PipelineSelection(tasks_file=normalized_tasks_file, selected_tasks=selected_tasks)


def _pipeline_paths(runs_dir: str | Path, pipeline_id: str) -> tuple[Path, Path, Path]:
    pipeline_dir = Path(runs_dir) / "pipelines" / pipeline_id
    return pipeline_dir, pipeline_dir / "PIPELINE_REPORT.md", pipeline_dir / "pipeline_state.json"


def _log(verbose: bool, message: str) -> None:
    if verbose:
        print(f"[pipeline] {message}", flush=True)


def _derive_final_status(
    *,
    continue_on_failure: bool,
    enabled_task_ids: list[str],
    executed_results: list[PipelineTaskResult],
) -> tuple[str, list[str]]:
    executed_task_ids = [task.task_id for task in executed_results]
    failed = [task for task in executed_results if task.status == "failed"]
    not_run_task_ids = [task_id for task_id in enabled_task_ids if task_id not in executed_task_ids]

    if failed:
        if continue_on_failure or not not_run_task_ids:
            return "failed", not_run_task_ids
        return "partial", not_run_task_ids
    return "approved", not_run_task_ids


def write_pipeline_report(state: PipelineState, pipeline_dir: str | Path) -> Path:
    pipeline_dir = Path(pipeline_dir)
    report_path = pipeline_dir / "PIPELINE_REPORT.md"
    results_by_task_id = {task.task_id: task for task in state.tasks}

    lines = [
        f"# Pipeline Report: {state.pipeline_id}",
        "",
        f"Status: `{state.status}`",
        f"Tasks file: `{state.tasks_file}`",
        f"Started at: `{state.started_at.isoformat()}`",
        f"Finished at: `{state.finished_at.isoformat() if state.finished_at else '(running)'}`",
        f"Continue on failure: `{state.continue_on_failure}`",
        "",
        "## Selected tasks",
        ", ".join(task.task_id for task in state.selected_tasks) or "(none)",
    ]

    if state.skipped_task_ids:
        lines.extend(["", "## Disabled tasks skipped", ", ".join(state.skipped_task_ids)])

    if state.not_run_task_ids:
        lines.extend(["", "## Enabled tasks not run", ", ".join(state.not_run_task_ids)])

    lines.extend([
        "",
        "## Task results",
        "",
        "| task_id | title | status | run_id | final_report | review_packet |",
        "|---|---|---|---|---|---|",
    ])
    for selected_task in state.selected_tasks:
        result = results_by_task_id.get(selected_task.task_id)
        if result is not None:
            lines.append(
                "| "
                f"`{selected_task.task_id}` | "
                f"{selected_task.title or ''} | "
                f"`{result.status}` | "
                f"`{result.run_id}` | "
                f"`{result.final_report}` | "
                f"`{result.review_packet or ''}` |"
            )
        elif not selected_task.enabled:
            lines.append(
                "| "
                f"`{selected_task.task_id}` | "
                f"{selected_task.title or ''} | "
                "`skipped` |  |  |  |"
            )
        else:
            lines.append(
                "| "
                f"`{selected_task.task_id}` | "
                f"{selected_task.title or ''} | "
                "`not_run` |  |  |  |"
            )

    failed_tasks = [task for task in state.tasks if task.status == "failed"]
    lines.extend(["", "## Failed task details"])
    if failed_tasks:
        for task in failed_tasks:
            lines.extend([
                f"- task_id=`{task.task_id}` run_id=`{task.run_id}` state=`{task.state}`",
                f"  final_report=`{task.final_report}`",
                f"  review_packet=`{task.review_packet or '(none)'}`",
            ])
    else:
        lines.append("No failed tasks.")

    lines.extend([
        "",
        "No accept-run or commit was performed by run-pipeline.",
    ])
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report_path


def run_pipeline(
    *,
    tasks_file: str | Path,
    runs_dir: str | Path = ".runs",
    from_task: str | None = None,
    only: list[str] | None = None,
    dry_run: bool = False,
    continue_on_failure: bool = False,
    backend: str | None = None,
    codex_cmd: str | None = None,
    max_retries: int | None = None,
    verbose: bool | None = None,
    stream_codex_output: bool | None = None,
) -> PipelinePlan | PipelineRunResult:
    config = load_task_queue_config(tasks_file)
    selection = select_pipeline_tasks(
        config,
        tasks_file=tasks_file,
        from_task=from_task,
        only=only,
        backend=backend,
        codex_cmd=codex_cmd,
        max_retries=max_retries,
        verbose=verbose,
        stream_codex_output=stream_codex_output,
    )

    if dry_run:
        return PipelinePlan(tasks_file=selection.tasks_file, selected_tasks=selection.selected_tasks)

    pipeline_verbose = bool(verbose) if verbose is not None else any(
        task.resolved_task.verbose for task in selection.selected_tasks if task.resolved_task is not None
    )
    pipeline_id = generate_pipeline_id()
    pipeline_dir, _pipeline_report_path, pipeline_state_path = _pipeline_paths(runs_dir, pipeline_id)
    state = PipelineState(
        pipeline_id=pipeline_id,
        tasks_file=str(selection.tasks_file),
        status="running",
        continue_on_failure=continue_on_failure,
        dry_run=False,
        selected_tasks=[
            PipelineSelectedTask(task_id=task.task_id, title=task.title, enabled=task.enabled)
            for task in selection.selected_tasks
        ],
        skipped_task_ids=selection.skipped_task_ids,
    )
    _log(pipeline_verbose, f"created pipeline_id={pipeline_id}")
    _log(pipeline_verbose, "selected tasks: " + (", ".join(selection.task_ids) or "(none)"))
    state.save_json(pipeline_state_path)

    for selected_task in selection.selected_tasks:
        if not selected_task.enabled or selected_task.resolved_task is None:
            continue

        _log(pipeline_verbose, f"starting task={selected_task.task_id}")
        run_config = build_run_config_from_resolved_task(selected_task.resolved_task, runs_dir=runs_dir)
        run_state, _backend = execute_run(run_config)
        final_report, review_packet, state_path = get_run_artifact_paths(run_config.runs_dir, run_state.run_id)
        task_result = PipelineTaskResult(
            task_id=selected_task.task_id,
            title=selected_task.title,
            status="approved" if run_state.final_status == "approved" else "failed",
            run_id=run_state.run_id,
            final_report=str(final_report.resolve()),
            review_packet=str(review_packet.resolve()) if review_packet.exists() else None,
            state=str(state_path.resolve()),
        )
        state.tasks.append(task_result)
        print(f"task_id={task_result.task_id} run_id={task_result.run_id} status={task_result.status}")
        _log(pipeline_verbose, f"finished task={selected_task.task_id} status={task_result.status}")
        state.save_json(pipeline_state_path)
        if task_result.status == "failed" and not continue_on_failure:
            _log(pipeline_verbose, f"stopping on failed task={selected_task.task_id}")
            break

    final_status, not_run_task_ids = _derive_final_status(
        continue_on_failure=continue_on_failure,
        enabled_task_ids=selection.enabled_task_ids,
        executed_results=state.tasks,
    )
    state.status = final_status
    state.not_run_task_ids = not_run_task_ids
    state.finished_at = datetime.now(timezone.utc)
    state.save_json(pipeline_state_path)
    report_path = write_pipeline_report(state, pipeline_dir)
    _log(pipeline_verbose, f"finished status={state.status}")
    return PipelineRunResult(
        state=state,
        pipeline_dir=pipeline_dir,
        pipeline_report=report_path,
        pipeline_state_path=pipeline_state_path,
    )
