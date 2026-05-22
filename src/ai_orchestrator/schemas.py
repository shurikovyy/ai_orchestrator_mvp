from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator, model_validator


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


class ReviewFinding(BaseModel):
    id: str
    reviewer: str
    category: Literal[
        "architecture",
        "qa",
        "business",
        "ops",
        "data",
        "security",
        "maintainability",
        "documentation",
        "correctness",
        "other",
    ]
    severity: Literal["critical", "major", "minor", "nit"]
    title: str
    evidence: str
    required_action: str | None = None
    file: str | None = None
    line: int | None = Field(default=None, ge=1)
    blocking: bool | None = None
    status: Literal["open", "resolved", "accepted_risk"] = "open"

    @field_validator("id", "reviewer", "title", "evidence")
    @classmethod
    def required_strings_not_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("required finding field must not be empty")
        return value

    @field_validator("required_action", "file")
    @classmethod
    def optional_strings_blank_to_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None

    @model_validator(mode="after")
    def compute_and_validate_blocking(self) -> "ReviewFinding":
        expected_blocking = self.severity in {"critical", "major"}
        if self.required_action is None and expected_blocking:
            raise ValueError("required_action is required for critical/major findings")
        if self.blocking is None:
            self.blocking = expected_blocking
        elif self.blocking is not expected_blocking:
            raise ValueError("blocking must match severity policy for this finding")
        return self


class ReviewFindingsCounts(BaseModel):
    total: int = Field(default=0, ge=0)
    critical: int = Field(default=0, ge=0)
    major: int = Field(default=0, ge=0)
    minor: int = Field(default=0, ge=0)
    nit: int = Field(default=0, ge=0)
    blocking_open: int = Field(default=0, ge=0)
    accepted_risk: int = Field(default=0, ge=0)
    resolved: int = Field(default=0, ge=0)


def _compute_review_findings_counts(findings: list[ReviewFinding]) -> ReviewFindingsCounts:
    return ReviewFindingsCounts(
        total=len(findings),
        critical=sum(1 for finding in findings if finding.severity == "critical"),
        major=sum(1 for finding in findings if finding.severity == "major"),
        minor=sum(1 for finding in findings if finding.severity == "minor"),
        nit=sum(1 for finding in findings if finding.severity == "nit"),
        blocking_open=sum(1 for finding in findings if finding.blocking and finding.status == "open"),
        accepted_risk=sum(1 for finding in findings if finding.status == "accepted_risk"),
        resolved=sum(1 for finding in findings if finding.status == "resolved"),
    )


class ReviewFindingsReport(BaseModel):
    schema_version: Literal["1.0"] = "1.0"
    run_id: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    summary: str
    findings: list[ReviewFinding] = Field(default_factory=list)
    overall_decision: Literal["pass", "needs_rework", "blocked"]
    counts: ReviewFindingsCounts | None = None

    @field_validator("run_id", "summary")
    @classmethod
    def required_report_strings_not_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("required findings report field must not be empty")
        return value

    @model_validator(mode="after")
    def compute_counts_and_validate_decision(self) -> "ReviewFindingsReport":
        seen_ids: set[str] = set()
        for finding in self.findings:
            if finding.id in seen_ids:
                raise ValueError(f"duplicate finding id: {finding.id}")
            seen_ids.add(finding.id)

        self.counts = _compute_review_findings_counts(self.findings)
        blocking_open = self.counts.blocking_open
        has_critical_open = any(
            finding.severity == "critical" and finding.status == "open" for finding in self.findings
        )
        has_major_open = any(
            finding.severity == "major" and finding.status == "open" for finding in self.findings
        )

        if blocking_open > 0 and self.overall_decision == "pass":
            raise ValueError("overall_decision cannot be pass when blocking_open > 0")
        if has_critical_open and self.overall_decision != "blocked":
            raise ValueError("overall_decision must be blocked when critical open findings exist")
        if has_major_open and not has_critical_open and self.overall_decision != "needs_rework":
            raise ValueError("overall_decision must be needs_rework when major open findings exist")
        return self


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
    findings_feedback_path: str | None = None
    findings_feedback_created_at: datetime | None = None
    findings_feedback_source_path: str | None = None
    findings_feedback_count: int = Field(default=0, ge=0)
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
        "findings_feedback_path",
        "findings_feedback_source_path",
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
