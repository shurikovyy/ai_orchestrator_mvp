"""Project config resolver for future governed workflow policy."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Literal, Mapping

import yaml


ConfigSource = Literal["default", "config", "env", "cli"]
CONFIG_SCHEMA_VERSION = "1.0"
DEFAULT_REVIEW_PROFILES = ("qa", "maintainability")
CONFIG_DISCOVERY_PATHS = (
    "ai_orchestrator.yaml",
    ".ai_orchestrator/config.yaml",
)


class ProjectConfigError(ValueError):
    """Raised when project config cannot be loaded or resolved."""


@dataclass(frozen=True)
class ResolvedValue:
    value: object
    source: ConfigSource


@dataclass(frozen=True)
class ConfigPaths:
    tasks_file: str | None = None
    runs_dir: str | None = None
    task_drafts_dir: str | None = None


@dataclass(frozen=True)
class CodexConfig:
    cmd: str | None = None


@dataclass(frozen=True)
class ReviewConfig:
    default_profiles: tuple[str, ...] | None = None


@dataclass(frozen=True)
class PolicyConfig:
    policy_file: str | None = None


@dataclass(frozen=True)
class ProjectConfig:
    schema_version: str
    paths: ConfigPaths
    codex: CodexConfig
    review: ReviewConfig
    policy: PolicyConfig


@dataclass(frozen=True)
class ResolvedProjectConfig:
    project_root: Path
    config_file: Path | None
    schema_version: str
    tasks_file: ResolvedValue
    runs_dir: ResolvedValue
    task_drafts_dir: ResolvedValue
    codex_cmd: ResolvedValue
    default_review_profiles: ResolvedValue
    policy_file: ResolvedValue


def discover_config_file(project_root: Path) -> Path | None:
    root = project_root.resolve()
    for relative_path in CONFIG_DISCOVERY_PATHS:
        candidate = root / relative_path
        if candidate.is_file():
            return candidate.resolve()
    return None


def load_project_config(path: Path) -> ProjectConfig:
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    except yaml.YAMLError as exc:
        raise ProjectConfigError(f"invalid YAML in config file {path}: {exc}") from exc
    except OSError as exc:
        raise ProjectConfigError(f"could not read config file {path}: {exc}") from exc

    if not isinstance(loaded, dict):
        raise ProjectConfigError("project config must be a YAML mapping")
    return _parse_project_config(loaded)


def resolve_project_config(
    *,
    project_root: str | Path = ".",
    config_path: str | Path | None = None,
    cli_overrides: Mapping[str, str | None] | None = None,
    env: Mapping[str, str] | None = None,
) -> ResolvedProjectConfig:
    root = Path(project_root).resolve()
    config_file = _resolve_config_file(root, config_path)
    config = load_project_config(config_file) if config_file is not None else _default_project_config()
    environment = os.environ if env is None else env
    cli_values = cli_overrides or {}

    tasks_file = _resolve_path_value(
        root=root,
        default="tasks.yaml",
        config_value=config.paths.tasks_file,
        env_value=_env_value(environment, "AI_ORCHESTRATOR_TASKS_FILE"),
        cli_value=cli_values.get("tasks_file"),
    )
    runs_dir = _resolve_path_value(
        root=root,
        default=".runs",
        config_value=config.paths.runs_dir,
        env_value=_env_value(environment, "AI_ORCHESTRATOR_RUNS_DIR"),
        cli_value=cli_values.get("runs_dir"),
    )
    task_drafts_dir = _resolve_path_value(
        root=root,
        default=".task_drafts",
        config_value=config.paths.task_drafts_dir,
        env_value=_env_value(environment, "AI_ORCHESTRATOR_TASK_DRAFTS_DIR"),
        cli_value=cli_values.get("task_drafts_dir"),
    )
    codex_cmd = _resolve_string_value(
        default=None,
        config_value=config.codex.cmd,
        env_value=_codex_env_value(environment),
        cli_value=cli_values.get("codex_cmd"),
    )
    default_review_profiles = ResolvedValue(
        value=list(config.review.default_profiles or DEFAULT_REVIEW_PROFILES),
        source="config" if config.review.default_profiles is not None else "default",
    )
    policy_file = _resolve_path_value(
        root=root,
        default="autonomy_policy.yaml",
        config_value=config.policy.policy_file,
        env_value=_env_value(environment, "AI_ORCHESTRATOR_POLICY_FILE"),
        cli_value=cli_values.get("policy_file"),
    )
    return ResolvedProjectConfig(
        project_root=root,
        config_file=config_file,
        schema_version=config.schema_version,
        tasks_file=tasks_file,
        runs_dir=runs_dir,
        task_drafts_dir=task_drafts_dir,
        codex_cmd=codex_cmd,
        default_review_profiles=default_review_profiles,
        policy_file=policy_file,
    )


def format_project_config_json(config: ResolvedProjectConfig) -> str:
    payload = {
        "project_root": str(config.project_root),
        "config_file": str(config.config_file) if config.config_file is not None else None,
        "schema_version": config.schema_version,
        "values": {
            "tasks_file": _resolved_value_payload(config.tasks_file),
            "runs_dir": _resolved_value_payload(config.runs_dir),
            "task_drafts_dir": _resolved_value_payload(config.task_drafts_dir),
            "codex_cmd": _resolved_value_payload(config.codex_cmd),
            "default_review_profiles": _resolved_value_payload(config.default_review_profiles),
            "policy_file": _resolved_value_payload(config.policy_file),
        },
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


def format_project_config_text(config: ResolvedProjectConfig) -> str:
    profiles = ", ".join(str(item) for item in config.default_review_profiles.value)
    lines = [
        "Project config",
        f"  project_root: {config.project_root}",
        f"  config_file: {config.config_file if config.config_file is not None else 'not found'}",
        f"  schema_version: {config.schema_version}",
        "",
        "Resolved paths",
        _format_resolved_line("tasks_file", config.tasks_file),
        _format_resolved_line("runs_dir", config.runs_dir),
        _format_resolved_line("task_drafts_dir", config.task_drafts_dir),
        "",
        "Codex",
        _format_resolved_line("cmd", config.codex_cmd, none_text="not configured"),
        "",
        "Review",
        f"  default_profiles: {profiles}    source: {config.default_review_profiles.source}",
        "",
        "Policy",
        _format_resolved_line("policy_file", config.policy_file),
    ]
    return "\n".join(lines)


def _parse_project_config(payload: dict[object, object]) -> ProjectConfig:
    allowed_top_level = {"schema_version", "paths", "codex", "review", "policy"}
    unknown = sorted(str(key) for key in payload if key not in allowed_top_level)
    if unknown:
        raise ProjectConfigError(f"unknown top-level config key(s): {', '.join(unknown)}")
    schema_version = payload.get("schema_version", CONFIG_SCHEMA_VERSION)
    if not isinstance(schema_version, str):
        raise ProjectConfigError("schema_version must be a string")
    if schema_version != CONFIG_SCHEMA_VERSION:
        raise ProjectConfigError(f"unsupported config schema_version: {schema_version}")

    paths_payload = _section_mapping(payload, "paths")
    codex_payload = _section_mapping(payload, "codex")
    review_payload = _section_mapping(payload, "review")
    policy_payload = _section_mapping(payload, "policy")

    _reject_unknown_section_keys(paths_payload, "paths", {"tasks_file", "runs_dir", "task_drafts_dir"})
    _reject_unknown_section_keys(codex_payload, "codex", {"cmd"})
    _reject_unknown_section_keys(review_payload, "review", {"default_profiles"})
    _reject_unknown_section_keys(policy_payload, "policy", {"policy_file"})

    return ProjectConfig(
        schema_version=schema_version,
        paths=ConfigPaths(
            tasks_file=_optional_string(paths_payload, "tasks_file"),
            runs_dir=_optional_string(paths_payload, "runs_dir"),
            task_drafts_dir=_optional_string(paths_payload, "task_drafts_dir"),
        ),
        codex=CodexConfig(cmd=_optional_string(codex_payload, "cmd")),
        review=ReviewConfig(default_profiles=_optional_string_tuple(review_payload, "default_profiles")),
        policy=PolicyConfig(policy_file=_optional_string(policy_payload, "policy_file")),
    )


def _resolve_config_file(root: Path, config_path: str | Path | None) -> Path | None:
    if config_path is None:
        return discover_config_file(root)
    candidate = Path(config_path)
    resolved = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    if not resolved.is_file():
        raise ProjectConfigError(f"config file not found: {resolved}")
    return resolved


def _default_project_config() -> ProjectConfig:
    return ProjectConfig(
        schema_version=CONFIG_SCHEMA_VERSION,
        paths=ConfigPaths(),
        codex=CodexConfig(),
        review=ReviewConfig(),
        policy=PolicyConfig(),
    )


def _section_mapping(payload: dict[object, object], key: str) -> dict[object, object]:
    value = payload.get(key, {})
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ProjectConfigError(f"{key} must be a mapping")
    return value


def _reject_unknown_section_keys(payload: dict[object, object], section: str, allowed: set[str]) -> None:
    unknown = sorted(str(key) for key in payload if key not in allowed)
    if unknown:
        raise ProjectConfigError(f"unknown {section} config key(s): {', '.join(unknown)}")


def _optional_string(payload: dict[object, object], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ProjectConfigError(f"{key} must be a string or null")
    stripped = value.strip()
    return stripped or None


def _optional_string_tuple(payload: dict[object, object], key: str) -> tuple[str, ...] | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ProjectConfigError(f"{key} must be a list of strings")
    return tuple(item for item in value)


def _resolve_path_value(
    *,
    root: Path,
    default: str,
    config_value: str | None,
    env_value: str | None,
    cli_value: str | None,
) -> ResolvedValue:
    value, source = _pick_value(
        default=default,
        config_value=config_value,
        env_value=env_value,
        cli_value=cli_value,
    )
    return ResolvedValue(value=str(_resolve_project_path(root, str(value))), source=source)


def _resolve_string_value(
    *,
    default: str | None,
    config_value: str | None,
    env_value: str | None,
    cli_value: str | None,
) -> ResolvedValue:
    value, source = _pick_value(
        default=default,
        config_value=config_value,
        env_value=env_value,
        cli_value=cli_value,
    )
    return ResolvedValue(value=value, source=source)


def _pick_value(
    *,
    default: object,
    config_value: str | None,
    env_value: str | None,
    cli_value: str | None,
) -> tuple[object, ConfigSource]:
    normalized_cli = _normalize_override(cli_value)
    if normalized_cli is not None:
        return normalized_cli, "cli"
    normalized_env = _normalize_override(env_value)
    if normalized_env is not None:
        return normalized_env, "env"
    normalized_config = _normalize_override(config_value)
    if normalized_config is not None:
        return normalized_config, "config"
    return default, "default"


def _resolve_project_path(root: Path, value: str) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def _env_value(env: Mapping[str, str], key: str) -> str | None:
    return env.get(key)


def _codex_env_value(env: Mapping[str, str]) -> str | None:
    return _normalize_override(env.get("AI_ORCHESTRATOR_CODEX_CMD")) or _normalize_override(env.get("CODEX_CMD"))


def _normalize_override(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _resolved_value_payload(value: ResolvedValue) -> dict[str, object]:
    return {"value": value.value, "source": value.source}


def _format_resolved_line(name: str, value: ResolvedValue, *, none_text: str = "") -> str:
    rendered = none_text if value.value is None else str(value.value)
    return f"  {name}: {rendered}    source: {value.source}"
