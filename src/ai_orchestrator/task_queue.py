from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ai_orchestrator.schemas import TaskSpec

BackendName = Literal["mock", "codex", "codex_cli"]


class TaskQueueConfigError(ValueError):
    """Raised when tasks.yaml cannot be loaded or resolved safely."""


class TaskDefaults(BaseModel):
    model_config = ConfigDict(extra="forbid")

    backend: BackendName | None = None
    max_retries: int | None = Field(default=None, ge=0, le=10)
    require_structured_report: bool | None = None
    rerun_report_test_commands: bool | None = None
    validate_workspace_manifest: bool | None = None
    validation_command_timeout: int | None = Field(default=None, ge=1, le=600)
    stream_codex_output: bool | None = None
    verbose: bool | None = None
    codex_cmd: str | None = None

    @field_validator("codex_cmd")
    @classmethod
    def blank_codex_cmd_to_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None


class TaskDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str | None = None
    prompt: str
    enabled: bool = True
    criteria: list[str] = Field(default_factory=list)
    backend: BackendName | None = None
    codex_cmd: str | None = None
    seed_workspace: str | None = None
    max_retries: int | None = Field(default=None, ge=0, le=10)
    require_structured_report: bool | None = None
    rerun_report_test_commands: bool | None = None
    validate_workspace_manifest: bool | None = None
    validation_command_timeout: int | None = Field(default=None, ge=1, le=600)
    stream_codex_output: bool | None = None
    verbose: bool | None = None
    commit_message: str | None = None

    @field_validator("id")
    @classmethod
    def id_not_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("task id must not be empty")
        return value

    @field_validator("title", "codex_cmd", "seed_workspace", "commit_message")
    @classmethod
    def blank_string_to_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None

    @field_validator("prompt")
    @classmethod
    def prompt_not_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("task prompt must not be empty")
        return value

    @field_validator("criteria")
    @classmethod
    def strip_criteria(cls, value: list[str]) -> list[str]:
        return [item.strip() for item in value if item.strip()]


class TaskQueueConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project: str | None = None
    defaults: TaskDefaults = Field(default_factory=TaskDefaults)
    tasks: list[TaskDefinition]


@dataclass(frozen=True)
class ResolvedTaskDefinition:
    task_id: str
    title: str | None
    prompt: str
    criteria: list[str]
    backend: str
    codex_cmd: str | None
    seed_workspace: str | None
    max_retries: int
    require_structured_report: bool
    rerun_report_test_commands: bool
    validate_workspace_manifest: bool
    validation_command_timeout: int
    stream_codex_output: bool
    verbose: bool
    commit_message: str | None

    def to_task_spec(self) -> TaskSpec:
        return TaskSpec(
            id=self.task_id,
            title=self.title,
            description=self.prompt,
            acceptance_criteria=list(self.criteria),
            max_retries=self.max_retries,
            require_structured_report=self.require_structured_report,
            rerun_report_test_commands=self.rerun_report_test_commands,
            validate_workspace_manifest=self.validate_workspace_manifest,
            seed_workspace_path=self.seed_workspace,
            validation_command_timeout_seconds=self.validation_command_timeout,
            commit_message=self.commit_message,
        )


def _load_yaml_module():
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - exercised by runtime environments, not unit tests.
        raise RuntimeError(
            "PyYAML is required to read tasks.yaml. Install project dependencies with `python -m pip install -e .`."
        ) from exc
    return yaml


def _pick_first_not_none(*values):
    for value in values:
        if value is not None:
            return value
    return None


def _validate_raw_payload(payload: object) -> None:
    if payload is None:
        raise TaskQueueConfigError("tasks file is empty")
    if not isinstance(payload, dict):
        raise TaskQueueConfigError("tasks file must contain a YAML mapping at the top level")

    raw_defaults = payload.get("defaults", {})
    if raw_defaults is not None and not isinstance(raw_defaults, dict):
        raise TaskQueueConfigError("defaults must be a mapping")

    raw_tasks = payload.get("tasks")
    if not isinstance(raw_tasks, list):
        raise TaskQueueConfigError("tasks must be a list")

    seen_ids: set[str] = set()
    for index, raw_task in enumerate(raw_tasks, start=1):
        if not isinstance(raw_task, dict):
            raise TaskQueueConfigError(f"task at index {index} must be a mapping")

        raw_id = raw_task.get("id")
        if not isinstance(raw_id, str) or not raw_id.strip():
            raise TaskQueueConfigError(f"task at index {index} is missing required field: id")
        task_id = raw_id.strip()
        if task_id in seen_ids:
            raise TaskQueueConfigError(f"duplicate task id: {task_id}")
        seen_ids.add(task_id)

        raw_prompt = raw_task.get("prompt")
        if not isinstance(raw_prompt, str) or not raw_prompt.strip():
            raise TaskQueueConfigError(f"task `{task_id}` is missing required field: prompt")

        raw_criteria = raw_task.get("criteria", [])
        if not isinstance(raw_criteria, list) or any(not isinstance(item, str) for item in raw_criteria):
            raise TaskQueueConfigError(f"task `{task_id}` criteria must be a list of strings")


def _format_validation_error(exc: ValidationError) -> str:
    first = exc.errors()[0]
    location = ".".join(str(part) for part in first.get("loc", ())) or "tasks.yaml"
    return f"invalid tasks.yaml field `{location}`: {first.get('msg', 'validation error')}"


def load_task_queue_config(tasks_file: str | Path) -> TaskQueueConfig:
    tasks_path = Path(tasks_file).expanduser()
    if not tasks_path.exists():
        raise FileNotFoundError(f"tasks file not found: {tasks_path}")
    if not tasks_path.is_file():
        raise FileNotFoundError(f"tasks file is not a file: {tasks_path}")

    yaml = _load_yaml_module()
    try:
        payload = yaml.safe_load(tasks_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise TaskQueueConfigError(f"failed to parse YAML in {tasks_path}: {exc}") from exc

    _validate_raw_payload(payload)
    try:
        return TaskQueueConfig.model_validate(payload)
    except ValidationError as exc:
        raise TaskQueueConfigError(_format_validation_error(exc)) from exc


def get_task_definition(config: TaskQueueConfig, task_id: str) -> TaskDefinition:
    normalized_task_id = task_id.strip()
    if not normalized_task_id:
        raise TaskQueueConfigError("task id must not be empty")

    task = next((item for item in config.tasks if item.id == normalized_task_id), None)
    if task is None:
        raise TaskQueueConfigError(f"task id not found: {normalized_task_id}")
    return task


def resolve_task_definition(
    config: TaskQueueConfig,
    *,
    task_id: str,
    tasks_file: str | Path,
    backend: str | None = None,
    codex_cmd: str | None = None,
    max_retries: int | None = None,
    verbose: bool | None = None,
    stream_codex_output: bool | None = None,
) -> ResolvedTaskDefinition:
    task = get_task_definition(config, task_id)
    defaults = config.defaults
    resolved_seed_workspace: str | None = None
    if task.seed_workspace is not None:
        seed_path = Path(task.seed_workspace).expanduser()
        if not seed_path.is_absolute():
            seed_path = Path(tasks_file).expanduser().resolve().parent / seed_path
        seed_path = seed_path.resolve()
        if not seed_path.exists():
            raise FileNotFoundError(f"task `{task.id}` seed_workspace does not exist: {seed_path}")
        if not seed_path.is_dir():
            raise NotADirectoryError(f"task `{task.id}` seed_workspace is not a directory: {seed_path}")
        resolved_seed_workspace = str(seed_path)

    resolved_backend = _pick_first_not_none(backend, task.backend, defaults.backend, "mock")
    resolved_codex_cmd = _pick_first_not_none(codex_cmd, task.codex_cmd, defaults.codex_cmd)
    resolved_max_retries = _pick_first_not_none(max_retries, task.max_retries, defaults.max_retries, 2)
    resolved_require_structured_report = _pick_first_not_none(
        task.require_structured_report,
        defaults.require_structured_report,
        False,
    )
    resolved_rerun_report_test_commands = _pick_first_not_none(
        task.rerun_report_test_commands,
        defaults.rerun_report_test_commands,
        False,
    )
    resolved_validate_workspace_manifest = _pick_first_not_none(
        task.validate_workspace_manifest,
        defaults.validate_workspace_manifest,
        False,
    )
    resolved_validation_command_timeout = _pick_first_not_none(
        task.validation_command_timeout,
        defaults.validation_command_timeout,
        60,
    )
    resolved_stream_codex_output = _pick_first_not_none(
        stream_codex_output,
        task.stream_codex_output,
        defaults.stream_codex_output,
        False,
    )
    resolved_verbose = _pick_first_not_none(verbose, task.verbose, defaults.verbose, False)

    return ResolvedTaskDefinition(
        task_id=task.id,
        title=task.title,
        prompt=task.prompt,
        criteria=list(task.criteria),
        backend=str(resolved_backend),
        codex_cmd=resolved_codex_cmd,
        seed_workspace=resolved_seed_workspace,
        max_retries=int(resolved_max_retries),
        require_structured_report=bool(resolved_require_structured_report),
        rerun_report_test_commands=bool(resolved_rerun_report_test_commands),
        validate_workspace_manifest=bool(resolved_validate_workspace_manifest),
        validation_command_timeout=int(resolved_validation_command_timeout),
        stream_codex_output=bool(resolved_stream_codex_output),
        verbose=bool(resolved_verbose),
        commit_message=task.commit_message,
    )
