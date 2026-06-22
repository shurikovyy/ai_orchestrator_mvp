from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from ai_orchestrator.schema_utils import normalize_safe_relative_path


RISK_CLASSIFICATION_LEVELS = {"low", "medium", "high", "critical"}
RISK_CHANGE_TYPES = {
    "docs_only",
    "tests_only",
    "source_code",
    "source_and_tests",
    "safety_critical",
    "data_logic",
    "mixed",
    "unknown",
}


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
    reasons: list[str] = Field(default_factory=list)
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

    @field_validator("reasons")
    @classmethod
    def reason_codes_must_be_deduped(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for raw in value:
            reason = raw.strip()
            if not reason or reason in seen:
                continue
            seen.add(reason)
            normalized.append(reason)
        return normalized

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
