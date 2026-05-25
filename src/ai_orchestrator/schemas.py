from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator

from ai_orchestrator.review_arbitration_schemas import (
    ArbitratedFinding,
    ReviewArbitrationCounts,
    ReviewArbitrationReport,
    finding_severity_rank,
)
from ai_orchestrator.review_findings_schemas import (
    REVIEW_FINDINGS_SOURCE_KINDS,
    ReviewFinding,
    ReviewFindingsCounts,
    ReviewFindingsReport,
)
from ai_orchestrator.risk_schemas import (
    RISK_CHANGE_TYPES,
    RISK_CLASSIFICATION_LEVELS,
    RiskClassification,
    RiskReason,
)
from ai_orchestrator.schema_utils import normalize_safe_relative_path


class TaskPlanStepSpec(BaseModel):
    id: str
    title: str | None = None
    description: str
    assigned_role: Literal["planner", "executor", "validator", "codex_executor"] = "executor"
    criteria: list[str] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def id_not_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("plan step id must not be empty")
        return value

    @field_validator("title")
    @classmethod
    def title_blank_to_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None

    @field_validator("description")
    @classmethod
    def description_not_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("plan step description cannot be empty")
        return value

    @field_validator("criteria")
    @classmethod
    def criteria_not_empty(cls, value: list[str]) -> list[str]:
        return [item.strip() for item in value if item.strip()]


class TaskSpec(BaseModel):
    """User-level task contract."""

    id: str = Field(default_factory=lambda: f"task_{uuid4().hex[:8]}")
    title: str | None = None
    description: str
    acceptance_criteria: list[str] = Field(default_factory=list)
    plan_steps: list[TaskPlanStepSpec] = Field(default_factory=list)
    max_retries: int = Field(default=2, ge=0, le=10)
    require_structured_report: bool = False
    rerun_report_test_commands: bool = False
    validate_workspace_manifest: bool = False
    seed_workspace_path: str | None = None
    validation_command_timeout_seconds: int = Field(default=60, ge=1, le=600)
    commit_message: str | None = None
    rework_of_run_id: str | None = None
    rework_feedback: str | None = None
    rework_feedback_path: str | None = None

    @field_validator("title", "commit_message", "rework_of_run_id", "rework_feedback", "rework_feedback_path")
    @classmethod
    def blank_optional_strings_to_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None

    @field_validator("seed_workspace_path")
    @classmethod
    def seed_workspace_path_blank_to_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None

    @field_validator("description")
    @classmethod
    def description_not_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Task description cannot be empty")
        return value

    @field_validator("acceptance_criteria")
    @classmethod
    def criteria_not_empty(cls, value: list[str]) -> list[str]:
        return [item.strip() for item in value if item.strip()]

    @field_validator("plan_steps")
    @classmethod
    def plan_steps_have_unique_ids(cls, value: list[TaskPlanStepSpec]) -> list[TaskPlanStepSpec]:
        seen_ids: set[str] = set()
        for step in value:
            if step.id in seen_ids:
                raise ValueError(f"duplicate plan step id: {step.id}")
            seen_ids.add(step.id)
        return value


class PlanStep(BaseModel):
    id: str
    title: str
    description: str
    assigned_role: Literal["planner", "executor", "validator", "codex_executor"] = "executor"
    acceptance_criteria: list[str] = Field(default_factory=list)


class Plan(BaseModel):
    task_id: str
    summary: str
    steps: list[PlanStep]


class CommandRunReport(BaseModel):
    """One command the executor claims to have run."""

    command: str
    exit_code: int | None = None
    status: Literal["passed", "failed", "skipped"]
    summary: str = ""

    @field_validator("command")
    @classmethod
    def command_not_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("command must not be empty")
        return value


class TestRunReport(BaseModel):
    """One test command/result the executor claims to have run."""

    name: str = "tests"
    command: str
    status: Literal["passed", "failed", "skipped", "not_run"]
    total: int | None = None
    passed: int | None = None
    failed: int | None = None
    output: str = ""

    @field_validator("command")
    @classmethod
    def command_not_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("test command must not be empty")
        return value


class StructuredExecutionReport(BaseModel):
    """Machine-readable execution report produced by executor backends.

    The report is intentionally small and conservative. It does not prove that
    the executor is truthful; it gives the deterministic validator a typed
    contract to inspect before falling back to text matching.
    """

    schema_version: Literal["1.0"] = "1.0"
    status: Literal["completed", "failed", "partial"]
    summary: str
    changed_files: list[str] = Field(default_factory=list)
    commands_run: list[CommandRunReport] = Field(default_factory=list)
    tests: list[TestRunReport] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    validation_notes: list[str] = Field(default_factory=list)

    @field_validator("summary")
    @classmethod
    def summary_not_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("summary must not be empty")
        return value

    @field_validator("changed_files", "risks", "assumptions", "validation_notes")
    @classmethod
    def strip_string_lists(cls, value: list[str]) -> list[str]:
        return [item.strip() for item in value if item.strip()]


class ExecutionResult(BaseModel):
    step_id: str
    attempt: int
    status: Literal["completed", "failed"]
    content: str
    artifact_paths: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class ValidationResult(BaseModel):
    step_id: str
    attempt: int
    approved: bool
    score: float = Field(ge=0.0, le=1.0)
    failed_criteria: list[str] = Field(default_factory=list)
    feedback: list[str] = Field(default_factory=list)


class RunState(BaseModel):
    run_id: str = Field(default_factory=lambda: f"run_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:6]}")
    task: TaskSpec
    backend_name: str | None = None
    human_review_decision: str | None = None
    human_review_decided_at: datetime | None = None
    human_review_feedback: str | None = None
    human_review_feedback_path: str | None = None
    human_review_decision_path: str | None = None
    review_findings_path: str | None = None
    review_findings_decision: str | None = None
    review_findings_blocking_count: int | None = Field(default=None, ge=0)
    review_findings_created_at: datetime | None = None
    review_findings_source_profile: str | None = None
    review_findings_source_kind: str | None = None
    review_arbitration_path: str | None = None
    review_arbitration_decision: str | None = None
    review_arbitration_final_blocking_count: int | None = Field(default=None, ge=0)
    review_arbitration_human_escalation_required: bool = False
    review_arbitration_source_findings_sha256: str | None = None
    review_arbitration_stale: bool = False
    review_arbitration_created_at: datetime | None = None
    findings_feedback_path: str | None = None
    findings_feedback_created_at: datetime | None = None
    findings_feedback_source_path: str | None = None
    findings_feedback_count: int = Field(default=0, ge=0)
    risk_classification_path: str | None = None
    risk_level: str | None = None
    change_type: str | None = None
    required_review_profiles: list[str] = Field(default_factory=list)
    optional_review_profiles: list[str] = Field(default_factory=list)
    apply_status: str | None = None
    applied_at: datetime | None = None
    apply_report_path: str | None = None
    apply_target_workspace: str | None = None
    applied_files: list[str] = Field(default_factory=list)
    deleted_files: list[str] = Field(default_factory=list)
    skipped_files: list[str] = Field(default_factory=list)
    plan: Plan | None = None
    executions: list[ExecutionResult] = Field(default_factory=list)
    validations: list[ValidationResult] = Field(default_factory=list)
    final_status: Literal["created", "planned", "running", "approved", "failed"] = "created"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator(
        "backend_name",
        "human_review_feedback",
        "human_review_feedback_path",
        "human_review_decision_path",
        "review_findings_path",
        "review_findings_decision",
        "review_findings_source_profile",
        "review_findings_source_kind",
        "review_arbitration_path",
        "review_arbitration_decision",
        "review_arbitration_source_findings_sha256",
        "findings_feedback_path",
        "findings_feedback_source_path",
        "risk_classification_path",
        "risk_level",
        "change_type",
        "apply_status",
        "apply_report_path",
        "apply_target_workspace",
    )
    @classmethod
    def optional_strings_blank_to_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None

    @field_validator("human_review_decision")
    @classmethod
    def human_review_decision_is_allowed(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip().lower()
        if not value:
            return None
        if value not in {"approved", "rejected"}:
            raise ValueError("human_review_decision must be one of: approved, rejected")
        return value

    @field_validator("review_findings_decision")
    @classmethod
    def review_findings_decision_is_allowed(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip().lower()
        if not value:
            return None
        if value not in {"pass", "needs_rework", "blocked"}:
            raise ValueError("review_findings_decision must be one of: pass, needs_rework, blocked")
        return value

    @field_validator("review_findings_source_kind")
    @classmethod
    def review_findings_source_kind_is_allowed(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip().lower()
        if not value:
            return None
        if value not in REVIEW_FINDINGS_SOURCE_KINDS:
            allowed = ", ".join(sorted(REVIEW_FINDINGS_SOURCE_KINDS))
            raise ValueError(f"review_findings_source_kind must be one of: {allowed}")
        return value

    @field_validator("review_arbitration_decision")
    @classmethod
    def review_arbitration_decision_is_allowed(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip().lower()
        if not value:
            return None
        if value not in {"pass", "needs_rework", "blocked", "human_escalation"}:
            raise ValueError("review_arbitration_decision must be one of: pass, needs_rework, blocked, human_escalation")
        return value

    @field_validator("risk_level")
    @classmethod
    def risk_level_is_allowed(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip().lower()
        if not value:
            return None
        if value not in RISK_CLASSIFICATION_LEVELS:
            allowed = ", ".join(sorted(RISK_CLASSIFICATION_LEVELS))
            raise ValueError(f"risk_level must be one of: {allowed}")
        return value

    @field_validator("change_type")
    @classmethod
    def change_type_is_allowed(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip().lower()
        if not value:
            return None
        if value not in RISK_CHANGE_TYPES:
            allowed = ", ".join(sorted(RISK_CHANGE_TYPES))
            raise ValueError(f"change_type must be one of: {allowed}")
        return value

    @field_validator("required_review_profiles", "optional_review_profiles")
    @classmethod
    def run_state_profile_lists_are_known(cls, value: list[str]) -> list[str]:
        from ai_orchestrator.review_profiles import is_known_review_profile

        normalized: list[str] = []
        seen: set[str] = set()
        for raw in value:
            profile = raw.strip()
            if not profile or profile in seen:
                continue
            if not is_known_review_profile(profile):
                raise ValueError(f"unknown review profile: {profile}")
            seen.add(profile)
            normalized.append(profile)
        return normalized

    @field_validator("apply_status")
    @classmethod
    def apply_status_is_allowed(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip().lower()
        if not value:
            return None
        if value not in {"applied"}:
            raise ValueError("apply_status must be one of: applied")
        return value

    @field_validator("applied_files", "deleted_files", "skipped_files")
    @classmethod
    def strip_optional_file_lists(cls, value: list[str]) -> list[str]:
        return [item.strip() for item in value if item.strip()]

    def touch(self) -> None:
        self.updated_at = datetime.now(timezone.utc)

    def save_json(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.model_dump_json(indent=2), encoding="utf-8")
