from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


FINDING_SEVERITY_ORDER = {"nit": 1, "minor": 2, "major": 3, "critical": 4}


def finding_severity_rank(value: str) -> int:
    return FINDING_SEVERITY_ORDER[value]


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
