from __future__ import annotations

import json
from typing import Literal, get_args

from pydantic import BaseModel, Field, field_validator

from ai_orchestrator.schemas import ReviewFinding

_CATEGORY_FIELD = ReviewFinding.model_fields["category"]
VALID_REVIEW_FINDING_CATEGORIES = tuple(get_args(_CATEGORY_FIELD.annotation))


class ReviewProfile(BaseModel):
    id: str
    title: str
    description: str
    reviewer_type: Literal["deterministic", "human", "llm_future"]
    focus_areas: list[str] = Field(default_factory=list)
    finding_categories: list[str] = Field(default_factory=list)
    default_severity_guidance: list[str] = Field(default_factory=list)
    required_evidence: list[str] = Field(default_factory=list)
    non_goals: list[str] = Field(default_factory=list)
    output_contract: str
    prompt_template: str

    @field_validator("id", "title", "description", "output_contract", "prompt_template")
    @classmethod
    def non_empty_string(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("review profile field must not be empty")
        return value

    @field_validator(
        "focus_areas",
        "finding_categories",
        "default_severity_guidance",
        "required_evidence",
        "non_goals",
    )
    @classmethod
    def non_empty_string_list(cls, value: list[str]) -> list[str]:
        items = [item.strip() for item in value if item.strip()]
        if not items:
            raise ValueError("review profile list field must not be empty")
        return items

    @field_validator("finding_categories")
    @classmethod
    def categories_must_be_valid(cls, value: list[str]) -> list[str]:
        invalid = [item for item in value if item not in VALID_REVIEW_FINDING_CATEGORIES]
        if invalid:
            allowed = ", ".join(VALID_REVIEW_FINDING_CATEGORIES)
            raise ValueError(f"unknown review finding categories: {', '.join(invalid)}. Allowed: {allowed}")
        return value


def _profile(**kwargs: object) -> ReviewProfile:
    return ReviewProfile.model_validate(kwargs)


def _build_builtin_registry() -> tuple[ReviewProfile, ...]:
    profiles = (
        _profile(
            id="deterministic",
            title="Deterministic Policy Reviewer",
            description="Applies hard policy checks to run artifacts without using LLM reasoning.",
            reviewer_type="deterministic",
            focus_areas=[
                "runtime/generated files in changed_files",
                "unsafe file paths and path traversal risks",
                "missing EXECUTION_REPORT.json declarations",
                "high-risk orchestration files",
                "broad diffs and source-without-tests policy",
            ],
            finding_categories=["ops", "security", "qa", "correctness", "maintainability"],
            default_severity_guidance=[
                "Use critical for unsafe paths, runtime/generated artifacts, or review/apply safety boundary violations.",
                "Use major for source changes without tests, missing EXECUTION_REPORT.json declarations, and high-risk file changes.",
                "Use minor for broad-but-non-blocking change surfaces or weaker hygiene issues.",
            ],
            required_evidence=[
                "changed file path",
                "observed artifact mismatch or policy violation",
                "relevant report/workspace evidence",
                "the exact deterministic rule that was violated",
            ],
            non_goals=[
                "Do not approve or reject the run.",
                "Do not apply or commit changes.",
                "Do not invent findings without concrete artifact evidence.",
            ],
            output_contract=(
                "Produce ReviewFinding-compatible findings only. The reviewer must emit evidence-backed findings that can be "
                "serialized into REVIEW_FINDINGS.json and REVIEW_FINDINGS.md."
            ),
            prompt_template=(
                "Inspect run artifacts and output ReviewFinding-compatible JSON findings only. Use evidence from changed_files, "
                "EXECUTION_REPORT.json, and deterministic policy rules."
            ),
        ),
        _profile(
            id="qa",
            title="QA Reviewer",
            description="Evaluates whether tests and validation evidence actually prove the requested behavior.",
            reviewer_type="llm_future",
            focus_areas=[
                "test adequacy and regression coverage",
                "negative cases and edge cases",
                "flaky/no-op fixtures",
                "whether tests prove the requirement rather than only the happy path",
            ],
            finding_categories=["qa", "correctness", "maintainability"],
            default_severity_guidance=[
                "Use major when source behavior changes without regression tests.",
                "Use major when tests are superficial or only cover the happy path around risky logic.",
                "Use critical if tests hide failure or bypass validator expectations.",
            ],
            required_evidence=[
                "file path or affected behavior",
                "missing, failing, or weak test coverage",
                "observed edge case or regression gap",
                "the requirement that remains unproven",
            ],
            non_goals=[
                "Do not approve or reject the run.",
                "Do not rewrite product requirements.",
                "Do not invent failures without test or behavior evidence.",
            ],
            output_contract=(
                "Produce ReviewFinding-compatible findings with concrete QA evidence and required_action guidance suitable for "
                "REVIEW_FINDINGS.json."
            ),
            prompt_template=(
                "Review the run for QA adequacy only and emit ReviewFinding-compatible JSON. Focus on regression coverage, edge "
                "cases, flaky fixtures, and proof of behavior."
            ),
        ),
        _profile(
            id="architecture",
            title="Architecture Reviewer",
            description="Reviews module boundaries, ownership, coupling, and long-term maintainability of the design.",
            reviewer_type="llm_future",
            focus_areas=[
                "module boundaries and ownership",
                "cohesion and coupling",
                "API compatibility",
                "unnecessary complexity or duplicated logic",
                "long-term maintainability in safety-critical code",
            ],
            finding_categories=["architecture", "maintainability", "correctness"],
            default_severity_guidance=[
                "Use major for wrong ownership or module boundaries in safety-critical code.",
                "Use minor for local readability or organization issues.",
                "Use critical for architecture that bypasses review, validation, or apply gates.",
            ],
            required_evidence=[
                "changed module or API surface",
                "observed ownership/coupling problem",
                "why the design violates an established boundary or contract",
                "the safer target architecture or direction",
            ],
            non_goals=[
                "Do not approve or reject the run.",
                "Do not require large rewrites without evidence.",
                "Do not invent architecture findings without referencing the changed design.",
            ],
            output_contract=(
                "Produce ReviewFinding-compatible findings that explain the architecture concern, the evidence, and the required "
                "rework direction."
            ),
            prompt_template=(
                "Review architecture and maintainability concerns only. Output ReviewFinding-compatible JSON with evidence for "
                "module boundaries, coupling, compatibility, and duplicated logic."
            ),
        ),
        _profile(
            id="ops",
            title="Operations Reviewer",
            description="Focuses on local workflow safety, filesystem behavior, reproducibility, and platform-specific risks.",
            reviewer_type="llm_future",
            focus_areas=[
                "local workflow safety",
                "filesystem and workspace safety",
                "runtime artifacts and cleanup expectations",
                "Windows/Git Bash compatibility",
                "command reproducibility",
            ],
            finding_categories=["ops", "security", "maintainability"],
            default_severity_guidance=[
                "Use critical for unsafe path or apply behavior.",
                "Use major for non-reproducible command flow or dirty-repo policy gaps.",
                "Use major for workflow steps that are unsafe on the supported local platform mix.",
            ],
            required_evidence=[
                "command or path involved",
                "observed reproducibility or safety problem",
                "affected platform or shell context",
                "the policy or operational assumption being violated",
            ],
            non_goals=[
                "Do not approve or reject the run.",
                "Do not perform destructive cleanup yourself.",
                "Do not invent environment issues without concrete command or path evidence.",
            ],
            output_contract=(
                "Produce ReviewFinding-compatible findings for operational safety and reproducibility issues only."
            ),
            prompt_template=(
                "Inspect operational safety, reproducibility, and filesystem handling. Emit ReviewFinding-compatible JSON findings "
                "with evidence tied to commands, paths, or platform constraints."
            ),
        ),
        _profile(
            id="security",
            title="Security Reviewer",
            description="Focuses on review/apply safety boundaries, path traversal, secrets, and unsafe command execution.",
            reviewer_type="llm_future",
            focus_areas=[
                "human review gate integrity",
                "apply/accept safety",
                "path traversal and workspace escape",
                "secrets exposure",
                "unsafe command execution or privilege assumptions",
            ],
            finding_categories=["security", "ops", "correctness"],
            default_severity_guidance=[
                "Use critical for bypassing human approval or writing outside the target workspace.",
                "Use critical for path traversal or secret exposure risk.",
                "Use major for weak validation around apply/accept and sandbox assumptions.",
            ],
            required_evidence=[
                "affected safety boundary or path",
                "observed bypass, exposure, or unsafe execution pattern",
                "the violated security policy or invariant",
                "the required mitigation or hardening action",
            ],
            non_goals=[
                "Do not approve or reject the run.",
                "Do not treat hypothetical risk as fact without evidence.",
                "Do not weaken existing review or apply gates.",
            ],
            output_contract=(
                "Produce ReviewFinding-compatible findings focused on security and gate integrity, with explicit evidence and "
                "required mitigations."
            ),
            prompt_template=(
                "Review the run for security and gate-integrity risks only. Output ReviewFinding-compatible JSON findings with "
                "evidence about approval gates, path safety, secrets, and command execution."
            ),
        ),
        _profile(
            id="business",
            title="Business Reviewer",
            description="Checks whether the implementation actually solves the requested operator or product problem.",
            reviewer_type="llm_future",
            focus_areas=[
                "task intent and problem fit",
                "operator usability",
                "user-facing workflow clarity",
                "documentation usefulness",
                "whether the output solves the requested problem",
            ],
            finding_categories=["business", "documentation", "correctness"],
            default_severity_guidance=[
                "Use major if the implementation solves the wrong problem.",
                "Use major if the operator workflow is misleading or incomplete.",
                "Use minor for wording or clarity issues that do not block execution.",
            ],
            required_evidence=[
                "the original request or operator need",
                "the observed mismatch in the produced output",
                "the missing workflow step, explanation, or user outcome",
                "the expected correction",
            ],
            non_goals=[
                "Do not approve or reject the run.",
                "Do not invent business requirements that were not requested.",
                "Do not treat style preference as a blocking issue without workflow impact.",
            ],
            output_contract=(
                "Produce ReviewFinding-compatible findings that tie product or operator problems to concrete evidence from the run."
            ),
            prompt_template=(
                "Review the run for operator and business fit only. Output ReviewFinding-compatible JSON findings with evidence "
                "about problem fit, workflow clarity, and missing user value."
            ),
        ),
        _profile(
            id="data",
            title="Data Reviewer",
            description="Evaluates correctness of data handling, timestamps, idempotency, joins, nulls, and analytical invariants.",
            reviewer_type="llm_future",
            focus_areas=[
                "data correctness",
                "null/NaT handling",
                "joins and key assumptions",
                "idempotency",
                "timestamps, timezones, and analytical invariants",
            ],
            finding_categories=["data", "correctness", "qa"],
            default_severity_guidance=[
                "Use critical for data corruption risk.",
                "Use major for missing edge cases around nulls, deduplication, joins, or keys.",
                "Use major for non-idempotent data processing or timezone mistakes.",
            ],
            required_evidence=[
                "affected dataset, field, or invariant",
                "observed null, deduplication, key, or timestamp risk",
                "the incorrect assumption or missing guard",
                "the required correction or validation",
            ],
            non_goals=[
                "Do not approve or reject the run.",
                "Do not invent data bugs without pointing to a concrete invariant or path.",
                "Do not treat unrelated code-style issues as data findings.",
            ],
            output_contract=(
                "Produce ReviewFinding-compatible findings for data correctness and invariants, with explicit evidence and "
                "required actions."
            ),
            prompt_template=(
                "Review data correctness, idempotency, timestamps, keys, and invariants only. Output ReviewFinding-compatible "
                "JSON findings with evidence and required fixes."
            ),
        ),
    )

    seen_ids: set[str] = set()
    for profile in profiles:
        if profile.id in seen_ids:
            raise ValueError(f"duplicate review profile id: {profile.id}")
        seen_ids.add(profile.id)
    return profiles


BUILTIN_REVIEW_PROFILES = _build_builtin_registry()
_BUILTIN_REVIEW_PROFILE_MAP = {profile.id: profile for profile in BUILTIN_REVIEW_PROFILES}


def list_review_profiles() -> tuple[ReviewProfile, ...]:
    return BUILTIN_REVIEW_PROFILES


def get_review_profile(profile_id: str) -> ReviewProfile | None:
    return _BUILTIN_REVIEW_PROFILE_MAP.get(profile_id.strip())


def is_known_review_profile(profile_id: str) -> bool:
    return get_review_profile(profile_id) is not None


def format_review_profiles_text(profiles: tuple[ReviewProfile, ...] | list[ReviewProfile]) -> str:
    lines = [f"profiles_total={len(profiles)}"]
    for profile in profiles:
        lines.append(
            " ".join(
                [
                    f"profile_id={profile.id}",
                    f"reviewer_type={profile.reviewer_type}",
                    f"categories={','.join(profile.finding_categories)}",
                    f"title={json.dumps(profile.title, ensure_ascii=False)}",
                ]
            )
        )
    return "\n".join(lines)


def format_review_profiles_json(profiles: tuple[ReviewProfile, ...] | list[ReviewProfile]) -> str:
    payload = {
        "profiles_total": len(profiles),
        "profiles": [profile.model_dump(mode="json") for profile in profiles],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


def format_review_profile_text(profile: ReviewProfile) -> str:
    lines = [
        f"profile_id={profile.id}",
        f"title={profile.title}",
        f"description={profile.description}",
        f"reviewer_type={profile.reviewer_type}",
        f"categories={','.join(profile.finding_categories)}",
    ]
    lines.extend(f"focus_area={item}" for item in profile.focus_areas)
    lines.extend(f"severity_guidance={item}" for item in profile.default_severity_guidance)
    lines.extend(f"required_evidence={item}" for item in profile.required_evidence)
    lines.extend(f"non_goal={item}" for item in profile.non_goals)
    lines.append(f"output_contract={profile.output_contract}")
    lines.append(f"prompt_template={profile.prompt_template}")
    return "\n".join(lines)


def format_review_profile_json(profile: ReviewProfile) -> str:
    return json.dumps(profile.model_dump(mode="json"), indent=2, ensure_ascii=False)
