from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from ai_orchestrator.schema_utils import normalize_safe_relative_path


REVIEW_FINDING_CATEGORIES = {
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
}
REVIEW_FINDINGS_SOURCE_KINDS = {"manual", "deterministic", "reviewer_profile", "external"}


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
        if value not in REVIEW_FINDINGS_SOURCE_KINDS:
            allowed = ", ".join(sorted(REVIEW_FINDINGS_SOURCE_KINDS))
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
