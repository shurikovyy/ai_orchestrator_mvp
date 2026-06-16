from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ai_orchestrator.schemas import TaskPlanStepSpec
from ai_orchestrator.task_queue import (
    TaskDefaults,
    TaskDefinition,
    get_task_definition,
    load_task_queue_config,
)


@dataclass(frozen=True)
class TaskInspectionSummary:
    tasks_file: Path
    task_id: str
    title: str
    enabled: bool
    backend: str
    seed_workspace: str | None
    seed_workspace_resolved: Path | None
    seed_workspace_exists: bool | None
    max_retries: int
    require_structured_report: bool
    rerun_report_test_commands: bool
    validate_workspace_manifest: bool
    validation_command_timeout: int
    stream_codex_output: bool
    verbose: bool
    commit_message: str | None
    prompt: str
    criteria: list[str]
    plan_steps: list[TaskPlanStepSpec]
    dry_run_readiness: str

    @property
    def criteria_count(self) -> int:
        return len(self.criteria)

    @property
    def plan_steps_count(self) -> int:
        return len(self.plan_steps)


def list_task_inspection_summaries(tasks_file: str | Path) -> list[TaskInspectionSummary]:
    tasks_path = Path(tasks_file).expanduser().resolve()
    config = load_task_queue_config(tasks_path)
    return [_build_task_summary(task, config.defaults, tasks_path) for task in config.tasks]


def build_task_inspection_summary(*, tasks_file: str | Path, task_id: str) -> TaskInspectionSummary:
    tasks_path = Path(tasks_file).expanduser().resolve()
    config = load_task_queue_config(tasks_path)
    task = get_task_definition(config, task_id)
    return _build_task_summary(task, config.defaults, tasks_path)


def _build_task_summary(
    task: TaskDefinition,
    defaults: TaskDefaults,
    tasks_file: Path,
) -> TaskInspectionSummary:
    seed_workspace_resolved = _resolve_seed_workspace_path(task.seed_workspace, tasks_file)
    return TaskInspectionSummary(
        tasks_file=tasks_file,
        task_id=task.id,
        title=task.title or "",
        enabled=bool(task.enabled),
        backend=str(_first_not_none(task.backend, defaults.backend, "mock")),
        seed_workspace=task.seed_workspace,
        seed_workspace_resolved=seed_workspace_resolved,
        seed_workspace_exists=seed_workspace_resolved.exists() if seed_workspace_resolved is not None else None,
        max_retries=int(_first_not_none(task.max_retries, defaults.max_retries, 2)),
        require_structured_report=bool(
            _first_not_none(task.require_structured_report, defaults.require_structured_report, False)
        ),
        rerun_report_test_commands=bool(
            _first_not_none(task.rerun_report_test_commands, defaults.rerun_report_test_commands, False)
        ),
        validate_workspace_manifest=bool(
            _first_not_none(task.validate_workspace_manifest, defaults.validate_workspace_manifest, False)
        ),
        validation_command_timeout=int(
            _first_not_none(task.validation_command_timeout, defaults.validation_command_timeout, 60)
        ),
        stream_codex_output=bool(_first_not_none(task.stream_codex_output, defaults.stream_codex_output, False)),
        verbose=bool(_first_not_none(task.verbose, defaults.verbose, False)),
        commit_message=task.commit_message,
        prompt=task.prompt,
        criteria=list(task.criteria),
        plan_steps=[step.model_copy(deep=True) for step in task.plan_steps],
        dry_run_readiness=_dry_run_readiness(task),
    )


def _first_not_none(*values):
    for value in values:
        if value is not None:
            return value
    return None


def _resolve_seed_workspace_path(seed_workspace: str | None, tasks_file: Path) -> Path | None:
    if seed_workspace is None:
        return None
    seed_path = Path(seed_workspace).expanduser()
    if not seed_path.is_absolute():
        seed_path = tasks_file.parent / seed_path
    return seed_path.resolve()


def _dry_run_readiness(task: TaskDefinition) -> str:
    if not task.enabled:
        return "disabled_requires_explicit_enable"
    return "enabled_check_with_doctor"
