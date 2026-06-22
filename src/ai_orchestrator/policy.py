"""Read-only autonomy policy loading and dry-run evaluation."""

from __future__ import annotations

from dataclasses import dataclass
import fnmatch
import json
from pathlib import Path, PurePosixPath
from typing import Literal

import yaml

from ai_orchestrator.config import ProjectConfigError, resolve_project_config


POLICY_SCHEMA_VERSION = "1.0"
RISK_LEVELS = ("docs_only", "low", "medium", "high", "critical", "safety_sensitive")
MAX_AUTO_RISK_LEVELS = ("docs_only", "low", "medium", "high", "critical")
SUPPORTED_ACTIONS = (
    "task-intake",
    "run-pipeline",
    "reviewer-agents",
    "record-findings",
    "record-arbitration",
    "review-run",
    "apply-run",
    "accept-run",
    "commit",
)
DECISIONS = ("allowed", "blocked", "human_gate_required")
DEFAULT_HUMAN_GATE_ACTIONS = (
    "record-findings",
    "record-arbitration",
    "review-run",
    "apply-run",
    "accept-run",
    "commit",
)


class PolicyError(ValueError):
    """Raised when an autonomy policy cannot be loaded or evaluated."""


@dataclass(frozen=True)
class AutonomySettings:
    allow_auto_task_intake: bool
    allow_auto_execution: bool
    allow_auto_reviewer_agents: bool
    allow_auto_apply: bool
    allow_auto_commit: bool


@dataclass(frozen=True)
class RiskPolicy:
    max_auto_risk: str
    safety_sensitive_requires_human: bool


@dataclass(frozen=True)
class GatePolicy:
    require_human_before: tuple[str, ...]


@dataclass(frozen=True)
class PathPolicy:
    forbidden: tuple[str, ...]
    require_human: tuple[str, ...]


@dataclass(frozen=True)
class BackendPolicy:
    allowed: tuple[str, ...]


@dataclass(frozen=True)
class AutonomyPolicy:
    schema_version: str
    autonomy: AutonomySettings
    risk: RiskPolicy
    gates: GatePolicy
    paths: PathPolicy
    backends: BackendPolicy
    review_requirements: dict[str, tuple[str, ...]]


@dataclass(frozen=True)
class LoadedPolicy:
    policy: AutonomyPolicy
    policy_file: Path | None
    requested_policy_file: Path | None
    policy_source: Literal["file", "default"]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class PolicyDecision:
    action: str
    risk_level: str
    decision: str
    allowed: bool
    reasons: tuple[str, ...]
    policy_file: Path | None
    policy_source: str
    changed_files: tuple[str, ...] = ()
    backend: str | None = None
    warnings: tuple[str, ...] = ()


def default_policy() -> AutonomyPolicy:
    return AutonomyPolicy(
        schema_version=POLICY_SCHEMA_VERSION,
        autonomy=AutonomySettings(
            allow_auto_task_intake=False,
            allow_auto_execution=False,
            allow_auto_reviewer_agents=False,
            allow_auto_apply=False,
            allow_auto_commit=False,
        ),
        risk=RiskPolicy(max_auto_risk="low", safety_sensitive_requires_human=True),
        gates=GatePolicy(require_human_before=DEFAULT_HUMAN_GATE_ACTIONS),
        paths=PathPolicy(forbidden=(), require_human=()),
        backends=BackendPolicy(allowed=("mock", "codex")),
        review_requirements={
            "medium": ("qa",),
            "high": ("security", "architecture", "qa"),
            "critical": ("security", "architecture", "qa", "ops", "maintainability"),
        },
    )


def load_policy_file(path: Path) -> AutonomyPolicy:
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    except yaml.YAMLError as exc:
        raise PolicyError(f"invalid YAML in policy file {path}: {exc}") from exc
    except OSError as exc:
        raise PolicyError(f"could not read policy file {path}: {exc}") from exc

    if not isinstance(loaded, dict):
        raise PolicyError("autonomy policy must be a YAML mapping")
    return _parse_policy(loaded)


def load_or_default_policy(
    *,
    project_root: str | Path = ".",
    policy_path: str | Path | None = None,
    config_path: str | Path | None = None,
) -> LoadedPolicy:
    root = Path(project_root).resolve()
    requested = _resolve_requested_policy_path(root=root, policy_path=policy_path, config_path=config_path)
    if requested is not None and requested.is_file():
        return LoadedPolicy(policy=load_policy_file(requested), policy_file=requested, requested_policy_file=requested, policy_source="file")
    warnings = ()
    if requested is not None:
        warnings = ("policy_file_missing_using_defaults",)
    return LoadedPolicy(
        policy=default_policy(),
        policy_file=None,
        requested_policy_file=requested,
        policy_source="default",
        warnings=warnings,
    )


def check_policy(
    loaded: LoadedPolicy,
    *,
    action: str,
    risk_level: str,
    changed_files: tuple[str, ...] | list[str] = (),
    backend: str | None = None,
) -> PolicyDecision:
    normalized_action = action.strip()
    if normalized_action not in SUPPORTED_ACTIONS:
        raise PolicyError(f"unknown policy action: {action}")
    normalized_risk = risk_level.strip()
    if normalized_risk not in RISK_LEVELS:
        raise PolicyError(f"unknown risk level: {risk_level}")
    normalized_files = tuple(_normalize_changed_file(path) for path in changed_files)
    normalized_backend = _normalize_backend(backend)

    policy = loaded.policy
    reasons: list[str] = list(loaded.warnings)
    block_reasons: list[str] = []
    human_reasons: list[str] = []

    if normalized_action in policy.gates.require_human_before:
        human_reasons.append("action_requires_human_gate")

    _add_autonomy_reason(policy, normalized_action, human_reasons)

    if normalized_risk == "safety_sensitive" and policy.risk.safety_sensitive_requires_human:
        human_reasons.append("safety_sensitive_requires_human")
    elif _risk_rank(normalized_risk) > _risk_rank(policy.risk.max_auto_risk):
        human_reasons.append("risk_exceeds_max_auto_risk")

    if normalized_backend is not None and normalized_backend not in policy.backends.allowed:
        block_reasons.append("backend_not_allowed")

    for changed_file in normalized_files:
        if _matches_any(changed_file, policy.paths.forbidden):
            block_reasons.append("path_forbidden")
        if _matches_any(changed_file, policy.paths.require_human):
            human_reasons.append("path_requires_human")

    reasons.extend(_unique(block_reasons + human_reasons))
    if block_reasons:
        decision = "blocked"
    elif human_reasons:
        decision = "human_gate_required"
    else:
        decision = "allowed"

    return PolicyDecision(
        action=normalized_action,
        risk_level=normalized_risk,
        decision=decision,
        allowed=decision == "allowed",
        reasons=tuple(_unique(reasons)),
        policy_file=loaded.policy_file,
        policy_source=loaded.policy_source,
        changed_files=normalized_files,
        backend=normalized_backend,
        warnings=loaded.warnings,
    )


def format_policy_json(loaded: LoadedPolicy) -> str:
    return json.dumps(_loaded_policy_payload(loaded), indent=2, ensure_ascii=False)


def format_policy_text(loaded: LoadedPolicy) -> str:
    policy = loaded.policy
    lines = [
        "Autonomy policy",
        f"  policy_source: {loaded.policy_source}",
        f"  policy_file: {loaded.policy_file if loaded.policy_file is not None else 'not found'}",
        f"  requested_policy_file: {loaded.requested_policy_file if loaded.requested_policy_file is not None else 'none'}",
        f"  schema_version: {policy.schema_version}",
    ]
    if loaded.warnings:
        lines.append("  warnings:")
        lines.extend(f"    - {warning}" for warning in loaded.warnings)
    lines.extend(
        [
            "",
            "Autonomy",
            f"  allow_auto_task_intake: {str(policy.autonomy.allow_auto_task_intake).lower()}",
            f"  allow_auto_execution: {str(policy.autonomy.allow_auto_execution).lower()}",
            f"  allow_auto_reviewer_agents: {str(policy.autonomy.allow_auto_reviewer_agents).lower()}",
            f"  allow_auto_apply: {str(policy.autonomy.allow_auto_apply).lower()}",
            f"  allow_auto_commit: {str(policy.autonomy.allow_auto_commit).lower()}",
            "",
            "Risk",
            f"  max_auto_risk: {policy.risk.max_auto_risk}",
            f"  safety_sensitive_requires_human: {str(policy.risk.safety_sensitive_requires_human).lower()}",
            "",
            "Gates",
            f"  require_human_before: {_join_or_none(policy.gates.require_human_before)}",
            "",
            "Paths",
            f"  forbidden: {_join_or_none(policy.paths.forbidden)}",
            f"  require_human: {_join_or_none(policy.paths.require_human)}",
            "",
            "Backends",
            f"  allowed: {_join_or_none(policy.backends.allowed)}",
            "",
            "Review requirements",
        ]
    )
    for risk, profiles in policy.review_requirements.items():
        lines.append(f"  {risk}: {_join_or_none(profiles)}")
    return "\n".join(lines)


def format_policy_decision_json(decision: PolicyDecision) -> str:
    payload = {
        "action": decision.action,
        "risk_level": decision.risk_level,
        "decision": decision.decision,
        "allowed": decision.allowed,
        "reasons": list(decision.reasons),
        "policy_file": str(decision.policy_file) if decision.policy_file is not None else None,
        "policy_source": decision.policy_source,
        "changed_files": list(decision.changed_files),
        "backend": decision.backend,
        "warnings": list(decision.warnings),
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


def format_policy_decision_text(decision: PolicyDecision) -> str:
    lines = [
        "Policy decision",
        f"  action: {decision.action}",
        f"  risk_level: {decision.risk_level}",
        f"  decision: {decision.decision}",
        f"  allowed: {str(decision.allowed).lower()}",
        f"  policy_source: {decision.policy_source}",
        f"  policy_file: {decision.policy_file if decision.policy_file is not None else 'not found'}",
    ]
    if decision.backend is not None:
        lines.append(f"  backend: {decision.backend}")
    if decision.changed_files:
        lines.append("  changed_files:")
        lines.extend(f"    - {path}" for path in decision.changed_files)
    if decision.reasons:
        lines.append("  reasons:")
        lines.extend(f"    - {reason}" for reason in decision.reasons)
    return "\n".join(lines)


def _resolve_requested_policy_path(
    *,
    root: Path,
    policy_path: str | Path | None,
    config_path: str | Path | None,
) -> Path | None:
    if policy_path is not None and str(policy_path).strip():
        return _resolve_path(root, policy_path)
    try:
        resolved_config = resolve_project_config(project_root=root, config_path=config_path)
    except ProjectConfigError as exc:
        raise PolicyError(str(exc)) from exc
    configured_policy = resolved_config.policy_file.value
    if configured_policy is None:
        return None
    return Path(str(configured_policy)).resolve()


def _parse_policy(payload: dict[object, object]) -> AutonomyPolicy:
    allowed_top_level = {"schema_version", "autonomy", "risk", "gates", "paths", "backends", "review_requirements"}
    unknown = sorted(str(key) for key in payload if key not in allowed_top_level)
    if unknown:
        raise PolicyError(f"unknown top-level policy key(s): {', '.join(unknown)}")
    schema_version = payload.get("schema_version", POLICY_SCHEMA_VERSION)
    if not isinstance(schema_version, str):
        raise PolicyError("schema_version must be a string")
    if schema_version != POLICY_SCHEMA_VERSION:
        raise PolicyError(f"unsupported policy schema_version: {schema_version}")

    autonomy_payload = _section_mapping(payload, "autonomy")
    risk_payload = _section_mapping(payload, "risk")
    gates_payload = _section_mapping(payload, "gates")
    paths_payload = _section_mapping(payload, "paths")
    backends_payload = _section_mapping(payload, "backends")
    review_payload = _section_mapping(payload, "review_requirements")

    _reject_unknown_section_keys(
        autonomy_payload,
        "autonomy",
        {
            "allow_auto_task_intake",
            "allow_auto_execution",
            "allow_auto_reviewer_agents",
            "allow_auto_apply",
            "allow_auto_commit",
        },
    )
    _reject_unknown_section_keys(risk_payload, "risk", {"max_auto_risk", "safety_sensitive_requires_human"})
    _reject_unknown_section_keys(gates_payload, "gates", {"require_human_before"})
    _reject_unknown_section_keys(paths_payload, "paths", {"forbidden", "require_human"})
    _reject_unknown_section_keys(backends_payload, "backends", {"allowed"})

    max_auto_risk = _string_value(risk_payload, "max_auto_risk", default="low")
    if max_auto_risk not in MAX_AUTO_RISK_LEVELS:
        raise PolicyError(f"risk.max_auto_risk must be one of: {', '.join(MAX_AUTO_RISK_LEVELS)}")

    require_human_before = _string_list(gates_payload, "require_human_before", default=DEFAULT_HUMAN_GATE_ACTIONS)
    for action in require_human_before:
        if action not in SUPPORTED_ACTIONS:
            raise PolicyError(f"gates.require_human_before contains unknown action: {action}")

    return AutonomyPolicy(
        schema_version=schema_version,
        autonomy=AutonomySettings(
            allow_auto_task_intake=_bool_value(autonomy_payload, "allow_auto_task_intake", default=False),
            allow_auto_execution=_bool_value(autonomy_payload, "allow_auto_execution", default=False),
            allow_auto_reviewer_agents=_bool_value(autonomy_payload, "allow_auto_reviewer_agents", default=False),
            allow_auto_apply=_bool_value(autonomy_payload, "allow_auto_apply", default=False),
            allow_auto_commit=_bool_value(autonomy_payload, "allow_auto_commit", default=False),
        ),
        risk=RiskPolicy(
            max_auto_risk=max_auto_risk,
            safety_sensitive_requires_human=_bool_value(risk_payload, "safety_sensitive_requires_human", default=True),
        ),
        gates=GatePolicy(require_human_before=require_human_before),
        paths=PathPolicy(
            forbidden=_string_list(paths_payload, "forbidden"),
            require_human=_string_list(paths_payload, "require_human"),
        ),
        backends=BackendPolicy(allowed=_string_list(backends_payload, "allowed", default=("mock", "codex"))),
        review_requirements=_review_requirements(review_payload),
    )


def _loaded_policy_payload(loaded: LoadedPolicy) -> dict[str, object]:
    policy = loaded.policy
    return {
        "policy_source": loaded.policy_source,
        "policy_file": str(loaded.policy_file) if loaded.policy_file is not None else None,
        "requested_policy_file": str(loaded.requested_policy_file) if loaded.requested_policy_file is not None else None,
        "warnings": list(loaded.warnings),
        "schema_version": policy.schema_version,
        "autonomy": {
            "allow_auto_task_intake": policy.autonomy.allow_auto_task_intake,
            "allow_auto_execution": policy.autonomy.allow_auto_execution,
            "allow_auto_reviewer_agents": policy.autonomy.allow_auto_reviewer_agents,
            "allow_auto_apply": policy.autonomy.allow_auto_apply,
            "allow_auto_commit": policy.autonomy.allow_auto_commit,
        },
        "risk": {
            "max_auto_risk": policy.risk.max_auto_risk,
            "safety_sensitive_requires_human": policy.risk.safety_sensitive_requires_human,
        },
        "gates": {"require_human_before": list(policy.gates.require_human_before)},
        "paths": {"forbidden": list(policy.paths.forbidden), "require_human": list(policy.paths.require_human)},
        "backends": {"allowed": list(policy.backends.allowed)},
        "review_requirements": {risk: list(profiles) for risk, profiles in policy.review_requirements.items()},
    }


def _section_mapping(payload: dict[object, object], key: str) -> dict[object, object]:
    value = payload.get(key, {})
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise PolicyError(f"{key} must be a mapping")
    return value


def _reject_unknown_section_keys(payload: dict[object, object], section: str, allowed: set[str]) -> None:
    unknown = sorted(str(key) for key in payload if key not in allowed)
    if unknown:
        raise PolicyError(f"unknown {section} policy key(s): {', '.join(unknown)}")


def _bool_value(payload: dict[object, object], key: str, *, default: bool) -> bool:
    value = payload.get(key, default)
    if not isinstance(value, bool):
        raise PolicyError(f"{key} must be a boolean")
    return value


def _string_value(payload: dict[object, object], key: str, *, default: str) -> str:
    value = payload.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise PolicyError(f"{key} must be a non-empty string")
    return value.strip()


def _string_list(payload: dict[object, object], key: str, *, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    value = payload.get(key, list(default))
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise PolicyError(f"{key} must be a list of strings")
    return tuple(item.strip() for item in value)


def _review_requirements(payload: dict[object, object]) -> dict[str, tuple[str, ...]]:
    if not payload:
        return default_policy().review_requirements
    requirements: dict[str, tuple[str, ...]] = {}
    for risk, profiles in payload.items():
        if not isinstance(risk, str) or risk not in RISK_LEVELS:
            raise PolicyError(f"review_requirements contains unsupported risk level: {risk}")
        if risk in ("docs_only", "low", "safety_sensitive"):
            raise PolicyError(f"review_requirements does not support entries for risk level: {risk}")
        if not isinstance(profiles, list) or not all(isinstance(item, str) and item.strip() for item in profiles):
            raise PolicyError("review_requirements values must be lists of strings")
        requirements[risk] = tuple(item.strip() for item in profiles)
    return requirements


def _add_autonomy_reason(policy: AutonomyPolicy, action: str, human_reasons: list[str]) -> None:
    if action == "task-intake" and not policy.autonomy.allow_auto_task_intake:
        human_reasons.append("auto_task_intake_disabled")
    elif action == "run-pipeline" and not policy.autonomy.allow_auto_execution:
        human_reasons.append("auto_execution_disabled")
    elif action == "reviewer-agents" and not policy.autonomy.allow_auto_reviewer_agents:
        human_reasons.append("auto_reviewer_agents_disabled")
    elif action == "apply-run" and not policy.autonomy.allow_auto_apply:
        human_reasons.append("auto_apply_disabled")
    elif action in ("accept-run", "commit") and not policy.autonomy.allow_auto_commit:
        human_reasons.append("auto_commit_disabled")


def _normalize_changed_file(value: str) -> str:
    raw = value.strip()
    if not raw:
        raise PolicyError("changed-file must not be empty")
    if "\\" in raw:
        raise PolicyError(f"changed-file must use project-relative POSIX paths: {value}")
    path = PurePosixPath(raw)
    if path.is_absolute() or raw.startswith("/"):
        raise PolicyError(f"changed-file must be project-relative: {value}")
    parts = tuple(part for part in path.parts if part not in ("", "."))
    if not parts or any(part == ".." for part in parts):
        raise PolicyError(f"changed-file must not contain path traversal: {value}")
    return "/".join(parts)


def _normalize_backend(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _matches_any(path: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def _risk_rank(risk_level: str) -> int:
    return RISK_LEVELS.index(risk_level)


def _resolve_path(root: Path, value: str | Path) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _join_or_none(values: tuple[str, ...]) -> str:
    return ", ".join(values) if values else "(none)"
