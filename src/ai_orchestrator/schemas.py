from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
import re
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator, model_validator

_WINDOWS_DRIVE_PATH_RE = re.compile(r"^[a-zA-Z]:[\\/]")
_REVIEW_FINDINGS_SOURCE_KINDS = {"manual", "deterministic", "reviewer_profile", "external"}
_RISK_CLASSIFICATION_LEVELS = {"low", "medium", "high", "critical"}
_FINDING_SEVERITY_ORDER = {"nit": 1, "minor": 2, "major": 3, "critical": 4}
_RISK_CHANGE_TYPES = {
    "docs_only",
    "tests_only",
    "source_code",
    "source_and_tests",
    "safety_critical",
    "data_logic",
    "mixed",
    "unknown",
}


def normalize_safe_relative_path(value: str, *, field_name: str = "path") -> str:
    raw_value = value.strip()
    if not raw_value:
        raise ValueError(f"{field_name} must not be empty when provided")
    if _WINDOWS_DRIVE_PATH_RE.match(raw_value):
        raise ValueError(f"{field_name} must be a relative path, not a drive-qualified path")
    normalized = raw_value.replace("\\", "/")
    if normalized.startswith("/"):
        raise ValueError(f"{field_name} must be a relative path, not a rooted path")
    normalized_path = PurePosixPath(normalized)
    if normalized_path.is_absolute():
        raise ValueError(f"{field_name} must be a relative path")
    if ".." in normalized_path.parts:
        raise ValueError(f"{field_name} path must not escape the workspace")
    return "/".join(normalized_path.parts)


def finding_severity_rank(value: str) -> int:
    return _FINDING_SEVERITY_ORDER[value]


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

    @field_validator("required_action")
    @classmethod
    def optional_strings_blank_to_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None

    @field_validator("file")
    @classmethod
    def file_must_be_safe_relative_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return normalize_safe_relative_path(value, field_name="file")

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
    source_profile: str | None = None
    source_kind: Literal["manual", "deterministic", "reviewer_profile", "external"] | None = None
    counts: ReviewFindingsCounts | None = None

    @field_validator("run_id", "summary")
    @classmethod
    def required_report_strings_not_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("required findings report field must not be empty")
        return value

    @field_validator("source_profile")
    @classmethod
    def source_profile_blank_to_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None

    @field_validator("source_kind")
    @classmethod
    def source_kind_is_allowed(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip().lower()
        if not value:
            return None
        if value not in _REVIEW_FINDINGS_SOURCE_KINDS:
            allowed = ", ".join(sorted(_REVIEW_FINDINGS_SOURCE_KINDS))
            raise ValueError(f"source_kind must be one of: {allowed}")
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
        if self.source_kind == "reviewer_profile" and not self.source_profile:
            raise ValueError("source_profile is required when source_kind is reviewer_profile")
        if self.source_kind == "deterministic" and self.source_profile not in {None, "deterministic"}:
            raise ValueError("source_profile must be deterministic when source_kind is deterministic")
        return self


class ArbitratedFinding(BaseModel):
    finding_id: str
    source_reviewer: str
    original_severity: Literal["critical", "major", "minor", "nit"]
    final_severity: Literal["critical", "major", "minor", "nit"]
    original_blocking: bool
    final_blocking: bool
    status: Literal[
        "upheld",
        "downgraded",
        "upgraded",
        "dismissed",
        "needs_evidence",
        "conflict",
        "accepted_risk",
    ]
    reason: str
    final_required_action: str | None = None
    human_escalation_required: bool = False
    deterministic_hard_gate: bool = False

    @field_validator("finding_id", "source_reviewer", "reason")
    @classmethod
    def arbitrated_required_strings_not_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("required arbitrated finding field must not be empty")
        return value

    @field_validator("final_required_action")
    @classmethod
    def final_required_action_blank_to_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None

    @model_validator(mode="after")
    def validate_arbitration_rules(self) -> "ArbitratedFinding":
        original_rank = finding_severity_rank(self.original_severity)
        final_rank = finding_severity_rank(self.final_severity)

        if self.final_blocking and self.final_required_action is None:
            raise ValueError("final_required_action is required when final_blocking is true")
        if self.status == "downgraded" and final_rank >= original_rank:
            raise ValueError("status=downgraded requires final_severity lower than original_severity")
        if self.status == "upgraded" and final_rank <= original_rank:
            raise ValueError("status=upgraded requires final_severity higher than original_severity")
        if self.status == "dismissed" and self.final_blocking:
            raise ValueError("status=dismissed implies final_blocking=false")
        if self.status == "accepted_risk":
            if self.final_blocking:
                raise ValueError("status=accepted_risk implies final_blocking=false")
            if self.original_severity in {"critical", "major"} and not self.human_escalation_required:
                raise ValueError(
                    "accepted_risk for critical/major original severity requires human_escalation_required=true"
                )
        if self.deterministic_hard_gate:
            if self.status == "dismissed":
                raise ValueError("deterministic_hard_gate findings cannot be dismissed")
            if not self.final_blocking:
                raise ValueError("deterministic_hard_gate findings must remain final_blocking=true")
            if self.original_severity in {"critical", "major"} and final_rank < original_rank:
                raise ValueError(
                    "deterministic_hard_gate findings cannot be downgraded below the original critical/major severity"
                )
        return self


class ReviewArbitrationCounts(BaseModel):
    total: int = Field(default=0, ge=0)
    upheld: int = Field(default=0, ge=0)
    downgraded: int = Field(default=0, ge=0)
    upgraded: int = Field(default=0, ge=0)
    dismissed: int = Field(default=0, ge=0)
    needs_evidence: int = Field(default=0, ge=0)
    conflict: int = Field(default=0, ge=0)
    accepted_risk: int = Field(default=0, ge=0)
    final_blocking: int = Field(default=0, ge=0)
    human_escalation_required: int = Field(default=0, ge=0)


def _compute_review_arbitration_counts(findings: list[ArbitratedFinding]) -> ReviewArbitrationCounts:
    return ReviewArbitrationCounts(
        total=len(findings),
        upheld=sum(1 for finding in findings if finding.status == "upheld"),
        downgraded=sum(1 for finding in findings if finding.status == "downgraded"),
        upgraded=sum(1 for finding in findings if finding.status == "upgraded"),
        dismissed=sum(1 for finding in findings if finding.status == "dismissed"),
        needs_evidence=sum(1 for finding in findings if finding.status == "needs_evidence"),
        conflict=sum(1 for finding in findings if finding.status == "conflict"),
        accepted_risk=sum(1 for finding in findings if finding.status == "accepted_risk"),
        final_blocking=sum(1 for finding in findings if finding.final_blocking),
        human_escalation_required=sum(1 for finding in findings if finding.human_escalation_required),
    )


class ReviewArbitrationReport(BaseModel):
    schema_version: Literal["1.0"] = "1.0"
    run_id: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    source_findings_path: str | None = None
    source_findings_sha256: str | None = None
    source_findings_updated_at: str | None = None
    arbitration_stale: bool = False
    arbiter: Literal["manual", "deterministic", "llm_future", "human"] = "manual"
    summary: str
    overall_decision: Literal["pass", "needs_rework", "blocked", "human_escalation"]
    arbitrated_findings: list[ArbitratedFinding] = Field(default_factory=list)
    counts: ReviewArbitrationCounts | None = None

    @field_validator("run_id", "summary")
    @classmethod
    def arbitration_required_strings_not_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("required arbitration report field must not be empty")
        return value

    @field_validator("source_findings_path", "source_findings_sha256", "source_findings_updated_at")
    @classmethod
    def source_findings_fields_blank_to_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None

    @model_validator(mode="after")
    def compute_counts_and_validate_decision(self) -> "ReviewArbitrationReport":
        seen_ids: set[str] = set()
        for finding in self.arbitrated_findings:
            if finding.finding_id in seen_ids:
                raise ValueError(f"duplicate arbitrated finding_id: {finding.finding_id}")
            seen_ids.add(finding.finding_id)

        self.counts = _compute_review_arbitration_counts(self.arbitrated_findings)
        if self.counts.final_blocking > 0 and self.overall_decision == "pass":
            raise ValueError("overall_decision cannot be pass when final_blocking > 0")
        if self.counts.human_escalation_required > 0 and self.overall_decision == "pass":
            raise ValueError("overall_decision cannot be pass when human escalation is required")
        return self


class RiskReason(BaseModel):
    id: str
    severity: Literal["info", "warning", "high", "critical"]
    category: Literal["docs", "tests", "source", "safety", "data", "ops", "architecture", "qa", "other"]
    message: str
    file: str | None = None
    reviewer_profiles: list[str] = Field(default_factory=list)

    @field_validator("id", "message")
    @classmethod
    def risk_reason_required_strings_not_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("risk reason field must not be empty")
        return value

    @field_validator("file")
    @classmethod
    def risk_reason_file_must_be_safe_relative_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return normalize_safe_relative_path(value, field_name="file")

    @field_validator("reviewer_profiles")
    @classmethod
    def reviewer_profiles_must_be_known_and_deduped(cls, value: list[str]) -> list[str]:
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


class RiskClassification(BaseModel):
    schema_version: Literal["1.0"] = "1.0"
    run_id: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    risk_level: Literal["low", "medium", "high", "critical"]
    change_type: Literal[
        "docs_only",
        "tests_only",
        "source_code",
        "source_and_tests",
        "safety_critical",
        "data_logic",
        "mixed",
        "unknown",
    ]
    changed_files: list[str] = Field(default_factory=list)
    risk_reasons: list[RiskReason] = Field(default_factory=list)
    required_review_profiles: list[str] = Field(default_factory=list)
    optional_review_profiles: list[str] = Field(default_factory=list)
    policy_notes: list[str] = Field(default_factory=list)

    @field_validator("run_id")
    @classmethod
    def risk_run_id_not_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("risk classification run_id must not be empty")
        return value

    @field_validator("changed_files")
    @classmethod
    def changed_files_must_be_safe_relative_paths(cls, value: list[str]) -> list[str]:
        return [normalize_safe_relative_path(item, field_name="changed_files") for item in value]

    @field_validator("policy_notes")
    @classmethod
    def strip_policy_notes(cls, value: list[str]) -> list[str]:
        return [item.strip() for item in value if item.strip()]

    @field_validator("required_review_profiles", "optional_review_profiles")
    @classmethod
    def profile_lists_must_be_known_and_deduped(cls, value: list[str]) -> list[str]:
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

    @model_validator(mode="after")
    def dedupe_optional_profiles_against_required(self) -> "RiskClassification":
        required_set = set(self.required_review_profiles)
        self.optional_review_profiles = [
            profile for profile in self.optional_review_profiles if profile not in required_set
        ]
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
        if value not in _REVIEW_FINDINGS_SOURCE_KINDS:
            allowed = ", ".join(sorted(_REVIEW_FINDINGS_SOURCE_KINDS))
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
        if value not in _RISK_CLASSIFICATION_LEVELS:
            allowed = ", ".join(sorted(_RISK_CLASSIFICATION_LEVELS))
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
        if value not in _RISK_CHANGE_TYPES:
            allowed = ", ".join(sorted(_RISK_CHANGE_TYPES))
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
