from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path, PurePosixPath
import re

from ai_orchestrator.apply import load_run_state
from ai_orchestrator.risk_schemas import RiskClassification, RiskReason
from ai_orchestrator.schema_utils import normalize_safe_relative_path
from ai_orchestrator.schemas import RunState, StructuredExecutionReport
from ai_orchestrator.validation import _normalize_manifest_path, load_structured_report

_RISK_LEVEL_PRIORITY = {"low": 1, "medium": 2, "high": 3, "critical": 4}
_SAFETY_CRITICAL_FILES = {
    "src/ai_orchestrator/apply.py",
    "src/ai_orchestrator/review.py",
    "src/ai_orchestrator/review_decision.py",
    "src/ai_orchestrator/rework.py",
    "src/ai_orchestrator/validation.py",
    "src/ai_orchestrator/engine.py",
    "src/ai_orchestrator/backends/codex_cli.py",
    "src/ai_orchestrator/schemas.py",
    "src/ai_orchestrator/deterministic_review.py",
    "src/ai_orchestrator/review_findings.py",
    "src/ai_orchestrator/findings_feedback.py",
    "src/ai_orchestrator/reviewer_prompts.py",
    "src/ai_orchestrator/review_profiles.py",
}
_MAINTAINABILITY_SENSITIVE_FILES = {
    "src/ai_orchestrator/task_drafts.py",
    "src/ai_orchestrator/task_draft_validation.py",
    "src/ai_orchestrator/task_draft_promotion.py",
    "src/ai_orchestrator/risk_classification.py",
    "src/ai_orchestrator/reviewer_prompts.py",
    "src/ai_orchestrator/review_profiles.py",
}
_WEB_JOB_ACTION_FILES = {"src/ai_orchestrator_web/jobs/actions.py"}
_JOB_RUNNER_FILES = {"src/ai_orchestrator_web/jobs/runner.py"}
_APPLY_LOGIC_FILES = {"src/ai_orchestrator/apply.py"}
_REVIEW_DECISION_FILES = {"src/ai_orchestrator/review_decision.py"}
_REVIEW_FINDINGS_FILES = {"src/ai_orchestrator/review_findings.py"}
_REVIEW_ARBITRATION_FILES = {"src/ai_orchestrator/review_arbitration.py"}
_RISK_CLASSIFIER_FILES = {"src/ai_orchestrator/risk_classification.py", "src/ai_orchestrator/risk_schemas.py"}
_VALIDATOR_FILES = {"src/ai_orchestrator/validation.py"}
_POLICY_FILES = {"src/ai_orchestrator/policy.py"}
_CONFIG_FILES = {"src/ai_orchestrator/config.py"}
_DEPENDENCY_MANIFEST_FILES = {"pyproject.toml", "package.json"}
_LOCKFILE_NAMES = {"poetry.lock", "uv.lock", "package-lock.json"}
_WINDOWS_DRIVE_PATH_RE = re.compile(r"^[a-zA-Z]:[\\/]")


@dataclass(frozen=True)
class RiskClassificationResult:
    run_id: str
    classification: RiskClassification
    risk_classification_path: Path
    risk_classification_markdown_path: Path
    state_path: Path


@dataclass(frozen=True)
class _RiskReasonDraft:
    severity: str
    category: str
    message: str
    reviewer_profiles: tuple[str, ...]
    file: str | None = None


def _is_unsafe_changed_path(raw_path: str) -> str | None:
    stripped = raw_path.strip()
    if not stripped:
        return "path is empty"
    if _WINDOWS_DRIVE_PATH_RE.match(stripped):
        return "path uses an absolute drive-qualified location"
    if stripped.startswith(("/", "\\")):
        return "path starts with a root slash"
    normalized = stripped.replace("\\", "/")
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute():
        return "path is absolute"
    if ".." in path.parts:
        return "path escapes the workspace via '..'"
    return None


def _is_execution_report(path: str) -> bool:
    return Path(path).name == "EXECUTION_REPORT.json"


def _is_source_python(path: str) -> bool:
    return path.startswith("src/") and path.endswith(".py")


def _is_test_python(path: str) -> bool:
    return path.startswith("tests/") and path.endswith(".py")


def _is_root_markdown(path: str) -> bool:
    pure = PurePosixPath(path)
    return len(pure.parts) == 1 and pure.suffix.lower() == ".md"


def _is_docs_markdown(path: str) -> bool:
    return (path.startswith("docs/") and path.endswith(".md")) or _is_root_markdown(path)


def _is_web_route(path: str) -> bool:
    return path.startswith("src/ai_orchestrator_web/routes/")


def _is_web_template(path: str) -> bool:
    return path.startswith("src/ai_orchestrator_web/templates/")


def _is_ci_workflow(path: str) -> bool:
    return path.startswith(".github/workflows/") or path.startswith("github/workflows/")


def _is_dependency_manifest(path: str) -> bool:
    pure = PurePosixPath(path)
    name = pure.name
    return name in _DEPENDENCY_MANIFEST_FILES or name.startswith("requirements") and name.endswith(".txt")


def _is_lockfile(path: str) -> bool:
    return PurePosixPath(path).name in _LOCKFILE_NAMES


def _is_data_logic_path(path: str) -> bool:
    pure = PurePosixPath(path)
    name = pure.name.lower()
    if pure.suffix.lower() == ".sql":
        return True
    if not path.startswith("src/") or pure.suffix.lower() != ".py":
        return False
    return (
        name.startswith("data")
        or name.startswith("etl")
        or name.startswith("analytics")
        or name.startswith("sql")
    )


def _set_at_least(current: str, candidate: str) -> str:
    return candidate if _RISK_LEVEL_PRIORITY[candidate] > _RISK_LEVEL_PRIORITY[current] else current


def _dedupe_profiles(items: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for item in items:
        if item not in seen:
            seen.add(item)
            normalized.append(item)
    return normalized


def _add_reason_code(reason_codes: list[str], code: str) -> None:
    if code not in reason_codes:
        reason_codes.append(code)


def _extend_reason_codes(reason_codes: list[str], codes: tuple[str, ...]) -> None:
    for code in codes:
        _add_reason_code(reason_codes, code)


def _prepend_profiles(existing: list[str], preferred: list[str]) -> list[str]:
    return _dedupe_profiles([*preferred, *existing])


def _dedupe_reasons(reasons: list[_RiskReasonDraft]) -> list[_RiskReasonDraft]:
    normalized: list[_RiskReasonDraft] = []
    seen: set[tuple[str, str, str, tuple[str, ...], str | None]] = set()
    for reason in reasons:
        key = (reason.severity, reason.category, reason.message, reason.reviewer_profiles, reason.file)
        if key in seen:
            continue
        seen.add(key)
        normalized.append(reason)
    return normalized


def classify_changed_files(
    *,
    run_id: str,
    changed_files: list[str],
    task: RunState | None = None,
) -> RiskClassification:
    safe_changed_files: list[str] = []
    unsafe_paths: list[tuple[str, str]] = []
    for raw_path in changed_files:
        issue = _is_unsafe_changed_path(raw_path)
        if issue is not None:
            unsafe_paths.append((raw_path, issue))
            continue
        normalized = _normalize_manifest_path(raw_path)
        if normalized:
            safe_changed_files.append(normalized)

    non_report_files = [path for path in safe_changed_files if not _is_execution_report(path)]
    source_files = [path for path in non_report_files if _is_source_python(path)]
    test_files = [path for path in non_report_files if _is_test_python(path)]
    docs_files = [path for path in non_report_files if _is_docs_markdown(path)]
    safety_files = [path for path in non_report_files if path.lower() in _SAFETY_CRITICAL_FILES]
    data_files = [path for path in non_report_files if _is_data_logic_path(path)]
    maintainability_sensitive_files = [
        path for path in non_report_files if path.lower() in _MAINTAINABILITY_SENSITIVE_FILES
    ]
    web_route_files = [path for path in non_report_files if _is_web_route(path)]
    web_template_files = [path for path in non_report_files if _is_web_template(path)]
    web_job_action_files = [path for path in non_report_files if path.lower() in _WEB_JOB_ACTION_FILES]
    job_runner_files = [path for path in non_report_files if path.lower() in _JOB_RUNNER_FILES]
    apply_logic_files = [path for path in non_report_files if path.lower() in _APPLY_LOGIC_FILES]
    review_decision_files = [path for path in non_report_files if path.lower() in _REVIEW_DECISION_FILES]
    review_findings_files = [path for path in non_report_files if path.lower() in _REVIEW_FINDINGS_FILES]
    review_arbitration_files = [path for path in non_report_files if path.lower() in _REVIEW_ARBITRATION_FILES]
    risk_classifier_files = [path for path in non_report_files if path.lower() in _RISK_CLASSIFIER_FILES]
    validator_files = [path for path in non_report_files if path.lower() in _VALIDATOR_FILES]
    policy_files = [path for path in non_report_files if path.lower() in _POLICY_FILES]
    config_files = [path for path in non_report_files if path.lower() in _CONFIG_FILES]
    cli_files = [path for path in non_report_files if path == "src/ai_orchestrator/cli.py"]
    ci_workflow_files = [path for path in non_report_files if _is_ci_workflow(path)]
    dependency_manifest_files = [path for path in non_report_files if _is_dependency_manifest(path)]
    lock_files = [path for path in non_report_files if _is_lockfile(path)]

    risk_level = "low"
    change_type = "unknown"
    required_profiles: list[str] = []
    optional_profiles: list[str] = []
    reason_codes: list[str] = []
    reasons: list[_RiskReasonDraft] = []
    policy_notes = [
        "Risk classification is deterministic and does not approve or reject the run.",
        "Required reviewer profiles should be prepared before human review when the run is still awaiting review.",
    ]

    if non_report_files and all(_is_docs_markdown(path) for path in non_report_files):
        risk_level = "low"
        change_type = "docs_only"
        optional_profiles.extend(["business", "qa"])
        _add_reason_code(reason_codes, "docs_only_change")
        reasons.append(
            _RiskReasonDraft(
                severity="info",
                category="docs",
                message="Docs-only change detected; no mandatory reviewer profiles are required by policy.",
                reviewer_profiles=("business", "qa"),
            )
        )
    elif non_report_files and all(_is_test_python(path) for path in non_report_files):
        risk_level = "low"
        change_type = "tests_only"
        required_profiles.append("qa")
        _add_reason_code(reason_codes, "tests_only_change")
        reasons.append(
            _RiskReasonDraft(
                severity="warning",
                category="tests",
                message="Tests-only change detected; QA review is required because quality signal changed without source edits.",
                reviewer_profiles=("qa", "architecture"),
                file=test_files[0] if test_files else None,
            )
        )
    elif source_files and test_files:
        risk_level = "medium"
        change_type = "source_and_tests"
        required_profiles.extend(["qa", "architecture", "maintainability"])
        _add_reason_code(reason_codes, "source_code_change")
        reasons.append(
            _RiskReasonDraft(
                severity="warning",
                category="source",
                message="Source and tests changed together; QA and architecture review are required, and maintainability review is recommended.",
                reviewer_profiles=("qa", "architecture", "maintainability"),
                file=source_files[0],
            )
        )
    elif source_files:
        risk_level = "high"
        change_type = "source_code"
        required_profiles.extend(["qa", "architecture", "maintainability"])
        optional_profiles.append("ops")
        _extend_reason_codes(reason_codes, ("source_code_change", "missing_tests_for_code_change"))
        reasons.append(
            _RiskReasonDraft(
                severity="high",
                category="source",
                message="Source code changed without test changes; QA and architecture review are required, with ops and maintainability review recommended.",
                reviewer_profiles=("qa", "architecture", "ops", "maintainability"),
                file=source_files[0],
            )
        )

    if web_route_files:
        risk_level = _set_at_least(risk_level, "medium")
        required_profiles.extend(["qa", "maintainability"])
        _add_reason_code(reason_codes, "web_route_change")
        reasons.append(
            _RiskReasonDraft(
                severity="warning",
                category="source",
                message="Web route changed; request handling and user-input flow need focused review.",
                reviewer_profiles=("qa", "maintainability"),
                file=web_route_files[0],
            )
        )

    if web_template_files:
        risk_level = _set_at_least(risk_level, "medium")
        required_profiles.extend(["qa", "maintainability"])
        _add_reason_code(reason_codes, "web_template_change")
        reasons.append(
            _RiskReasonDraft(
                severity="warning",
                category="source",
                message="Web template changed; operator-facing workflow and form safety need review.",
                reviewer_profiles=("qa", "maintainability"),
                file=web_template_files[0],
            )
        )

    if web_job_action_files:
        risk_level = _set_at_least(risk_level, "high")
        required_profiles.extend(["security", "qa", "maintainability"])
        _extend_reason_codes(reason_codes, ("web_job_action_change", "subprocess_command_construction_change"))
        reasons.append(
            _RiskReasonDraft(
                severity="high",
                category="safety",
                message="Web job action allowlist changed; command construction and action exposure need security review.",
                reviewer_profiles=("security", "qa", "maintainability"),
                file=web_job_action_files[0],
            )
        )

    if job_runner_files:
        risk_level = _set_at_least(risk_level, "high")
        required_profiles.extend(["security", "qa", "maintainability"])
        _extend_reason_codes(reason_codes, ("job_runner_change", "subprocess_command_construction_change"))
        reasons.append(
            _RiskReasonDraft(
                severity="high",
                category="safety",
                message="Web job runner changed; subprocess execution and job isolation need security review.",
                reviewer_profiles=("security", "qa", "maintainability"),
                file=job_runner_files[0],
            )
        )

    if apply_logic_files:
        risk_level = _set_at_least(risk_level, "high")
        required_profiles.extend(["security", "architecture", "qa", "maintainability"])
        _extend_reason_codes(reason_codes, ("apply_logic_change", "accept_logic_change", "security_sensitive_path"))
        reasons.append(
            _RiskReasonDraft(
                severity="high",
                category="safety",
                message="Apply logic changed; target working-tree write behavior requires security and architecture review.",
                reviewer_profiles=("security", "architecture", "qa", "maintainability"),
                file=apply_logic_files[0],
            )
        )

    if review_decision_files:
        risk_level = _set_at_least(risk_level, "high")
        required_profiles.extend(["architecture", "qa", "maintainability"])
        _add_reason_code(reason_codes, "review_decision_logic_change")
        reasons.append(
            _RiskReasonDraft(
                severity="high",
                category="safety",
                message="Review decision gate changed; approval/rejection semantics require architecture review.",
                reviewer_profiles=("architecture", "qa", "maintainability"),
                file=review_decision_files[0],
            )
        )

    if review_findings_files:
        risk_level = _set_at_least(risk_level, "high")
        required_profiles.extend(["architecture", "qa", "maintainability"])
        _add_reason_code(reason_codes, "review_findings_logic_change")
        reasons.append(
            _RiskReasonDraft(
                severity="high",
                category="safety",
                message="Review findings logic changed; structured reviewer state can affect approval flow.",
                reviewer_profiles=("architecture", "qa", "maintainability"),
                file=review_findings_files[0],
            )
        )

    if review_arbitration_files:
        risk_level = _set_at_least(risk_level, "high")
        required_profiles.extend(["architecture", "qa", "maintainability"])
        _add_reason_code(reason_codes, "review_arbitration_logic_change")
        reasons.append(
            _RiskReasonDraft(
                severity="high",
                category="safety",
                message="Review arbitration logic changed; finding resolution can affect review gate outcomes.",
                reviewer_profiles=("architecture", "qa", "maintainability"),
                file=review_arbitration_files[0],
            )
        )

    if cli_files:
        risk_level = _set_at_least(risk_level, "medium")
        required_profiles.extend(["qa", "maintainability"])
        _add_reason_code(reason_codes, "cli_command_surface_change")
        reasons.append(
            _RiskReasonDraft(
                severity="warning",
                category="ops",
                message="CLI command surface changed; operator-facing command behavior needs review.",
                reviewer_profiles=("qa", "maintainability"),
                file=cli_files[0],
            )
        )

    if validator_files:
        risk_level = _set_at_least(risk_level, "high")
        required_profiles.extend(["architecture", "qa", "maintainability"])
        _add_reason_code(reason_codes, "validator_change")
        reasons.append(
            _RiskReasonDraft(
                severity="high",
                category="safety",
                message="Validator logic changed; deterministic approval behavior requires careful review.",
                reviewer_profiles=("architecture", "qa", "maintainability"),
                file=validator_files[0],
            )
        )

    if risk_classifier_files:
        risk_level = _set_at_least(risk_level, "high")
        required_profiles.extend(["architecture", "qa", "maintainability"])
        _add_reason_code(reason_codes, "risk_classifier_change")
        reasons.append(
            _RiskReasonDraft(
                severity="high",
                category="architecture",
                message="Risk classifier or schema changed; review routing semantics need architecture review.",
                reviewer_profiles=("architecture", "qa", "maintainability"),
                file=risk_classifier_files[0],
            )
        )

    if policy_files:
        risk_level = _set_at_least(risk_level, "high")
        required_profiles.extend(["security", "architecture", "qa", "maintainability"])
        _add_reason_code(reason_codes, "policy_logic_change")
        reasons.append(
            _RiskReasonDraft(
                severity="high",
                category="safety",
                message="Policy logic changed; autonomy and gate decisions require security review.",
                reviewer_profiles=("security", "architecture", "qa", "maintainability"),
                file=policy_files[0],
            )
        )

    if config_files:
        risk_level = _set_at_least(risk_level, "medium")
        required_profiles.extend(["architecture", "qa", "maintainability"])
        _add_reason_code(reason_codes, "config_logic_change")
        reasons.append(
            _RiskReasonDraft(
                severity="warning",
                category="architecture",
                message="Config resolver logic changed; default and override behavior need review.",
                reviewer_profiles=("architecture", "qa", "maintainability"),
                file=config_files[0],
            )
        )

    if ci_workflow_files:
        risk_level = _set_at_least(risk_level, "high")
        required_profiles.extend(["security", "ops", "qa"])
        _add_reason_code(reason_codes, "ci_workflow_change")
        reasons.append(
            _RiskReasonDraft(
                severity="high",
                category="ops",
                message="CI workflow changed; execution environment and supply-chain behavior need review.",
                reviewer_profiles=("security", "ops", "qa"),
                file=ci_workflow_files[0],
            )
        )

    if dependency_manifest_files:
        risk_level = _set_at_least(risk_level, "medium")
        required_profiles.extend(["security", "maintainability", "qa"])
        _add_reason_code(reason_codes, "dependency_manifest_change")
        reasons.append(
            _RiskReasonDraft(
                severity="warning",
                category="ops",
                message="Dependency manifest changed; dependency and packaging impact needs security review.",
                reviewer_profiles=("security", "maintainability", "qa"),
                file=dependency_manifest_files[0],
            )
        )

    if lock_files:
        risk_level = _set_at_least(risk_level, "medium")
        required_profiles.extend(["security", "maintainability", "qa"])
        _add_reason_code(reason_codes, "lockfile_change")
        reasons.append(
            _RiskReasonDraft(
                severity="warning",
                category="ops",
                message="Dependency lockfile changed; resolved dependency set needs review.",
                reviewer_profiles=("security", "maintainability", "qa"),
                file=lock_files[0],
            )
        )
    if maintainability_sensitive_files:
        risk_level = _set_at_least(risk_level, "high")
        _add_reason_code(reason_codes, "source_code_change")
        required_profiles = _prepend_profiles(required_profiles, ["architecture", "qa", "maintainability"])
        optional_profiles = [
            profile for profile in optional_profiles if profile not in {"architecture", "qa", "maintainability"}
        ]
        reasons.append(
            _RiskReasonDraft(
                severity="high",
                category="architecture",
                message="Maintainability-sensitive orchestration or task-intake module touched; focused maintainability review is required.",
                reviewer_profiles=("architecture", "qa", "maintainability"),
                file=maintainability_sensitive_files[0],
            )
        )

    if safety_files:
        risk_level = _set_at_least(risk_level, "critical")
        change_type = "safety_critical"
        _add_reason_code(reason_codes, "security_sensitive_path")
        required_profiles = _prepend_profiles(required_profiles, ["security", "architecture", "qa", "ops", "maintainability"])
        optional_profiles = [
            profile
            for profile in optional_profiles
            if profile not in {"security", "architecture", "qa", "ops", "maintainability"}
        ]
        reasons.append(
            _RiskReasonDraft(
                severity="critical",
                category="safety",
                message="Safety-critical orchestration/review/apply path touched; the code must also remain understandable to human reviewers.",
                reviewer_profiles=("security", "architecture", "qa", "ops", "maintainability"),
                file=safety_files[0],
            )
        )

    if data_files:
        risk_level = _set_at_least(risk_level, "high")
        _add_reason_code(reason_codes, "data_logic_change")
        if change_type != "safety_critical":
            change_type = "data_logic"
            required_profiles = [profile for profile in required_profiles if profile not in {"data", "qa", "architecture", "ops"}]
            required_profiles = _prepend_profiles(required_profiles, ["data", "qa"])
            optional_profiles = [profile for profile in optional_profiles if profile != "architecture"]
            optional_profiles.append("architecture")
        reasons.append(
            _RiskReasonDraft(
                severity="high",
                category="data",
                message="Data/analytics logic touched; data and QA review are required.",
                reviewer_profiles=("data", "qa", "architecture"),
                file=data_files[0],
            )
        )

    mixed_signal_count = sum(
        1
        for present in (
            bool(docs_files),
            bool(test_files),
            bool(source_files),
        )
        if present
    )
    if (
        mixed_signal_count > 1
        and change_type not in {"safety_critical", "data_logic", "docs_only", "tests_only"}
        and not (change_type == "source_and_tests" and not docs_files)
    ):
        change_type = "mixed"
        _add_reason_code(reason_codes, "mixed_docs_and_code")
        reasons.append(
            _RiskReasonDraft(
                severity="warning",
                category="other",
                message="Mixed change categories detected; the highest triggered risk level and reviewer profile union are applied.",
                reviewer_profiles=tuple(_dedupe_profiles(required_profiles + optional_profiles)),
            )
        )

    changed_count = len(non_report_files)
    if changed_count > 20:
        risk_level = _set_at_least(risk_level, "critical")
        _add_reason_code(reason_codes, "large_change_set")
        required_profiles.extend(["architecture", "qa", "ops", "maintainability"])
        reasons.append(
            _RiskReasonDraft(
                severity="critical",
                category="architecture",
                message=f"Broad change surface detected: {changed_count} changed files exceeds the critical threshold and is harder for humans to review safely.",
                reviewer_profiles=("architecture", "qa", "ops", "maintainability"),
            )
        )
    elif changed_count > 10:
        risk_level = _set_at_least(risk_level, "high")
        _add_reason_code(reason_codes, "large_change_set")
        required_profiles.extend(["architecture", "qa", "maintainability"])
        reasons.append(
            _RiskReasonDraft(
                severity="high",
                category="architecture",
                message=f"Broad change surface detected: {changed_count} changed files exceeds the high-risk threshold and reduces human reviewability.",
                reviewer_profiles=("architecture", "qa", "maintainability"),
            )
        )

    if unsafe_paths:
        risk_level = _set_at_least(risk_level, "critical")
        _extend_reason_codes(reason_codes, ("security_sensitive_path", "path_forbidden"))
        required_profiles.extend(["security", "ops"])
        for raw_path, issue in unsafe_paths:
            reasons.append(
                _RiskReasonDraft(
                    severity="critical",
                    category="ops",
                    message=f"Unsafe changed file path detected: `{raw_path}` ({issue}).",
                    reviewer_profiles=("security", "ops"),
                )
            )
        if change_type != "safety_critical":
            change_type = "mixed" if non_report_files else "unknown"

    if not non_report_files:
        if unsafe_paths:
            policy_notes.append("Only unsafe or report-only paths were observed; treat the run as critical until paths are normalized.")
        else:
            _add_reason_code(reason_codes, "missing_changed_files_context")
            reasons.append(
                _RiskReasonDraft(
                    severity="warning",
                    category="other",
                    message="No non-report changed files were observed; classification has limited path context.",
                    reviewer_profiles=(),
                )
            )
            policy_notes.append("No non-report changed files were observed; classification remains unknown.")

    required_profiles = _dedupe_profiles(required_profiles)
    optional_profiles = [profile for profile in _dedupe_profiles(optional_profiles) if profile not in set(required_profiles)]
    reasons = _dedupe_reasons(reasons)

    risk_reason_models = [
        RiskReason(
            id=f"R{index:03d}",
            severity=reason.severity,
            category=reason.category,
            message=reason.message,
            file=reason.file,
            reviewer_profiles=list(reason.reviewer_profiles),
        )
        for index, reason in enumerate(reasons, start=1)
    ]

    return RiskClassification(
        run_id=run_id,
        created_at=datetime.now(timezone.utc),
        risk_level=risk_level,
        change_type=change_type,
        changed_files=safe_changed_files,
        reasons=reason_codes,
        risk_reasons=risk_reason_models,
        required_review_profiles=required_profiles,
        optional_review_profiles=optional_profiles,
        policy_notes=policy_notes,
    )


def build_risk_classification_markdown(classification: RiskClassification) -> str:
    lines = [
        f"# Risk Classification: {classification.run_id}",
        "",
        f"Risk level: `{classification.risk_level}`",
        f"Change type: `{classification.change_type}`",
        f"Created at: `{classification.created_at.isoformat()}`",
        "",
        "## Required review profiles",
        "",
        *([f"- `{profile}`" for profile in classification.required_review_profiles] or ["- (none)"]),
        "",
        "## Optional review profiles",
        "",
        *([f"- `{profile}`" for profile in classification.optional_review_profiles] or ["- (none)"]),
        "",
        "## Changed files",
        "",
        *([f"- `{path}`" for path in classification.changed_files] or ["- (none)"]),
        "",
        "## Reason codes",
        "",
        *([f"- `{reason}`" for reason in classification.reasons] or ["- (none)"]),
        "",
        "## Risk reasons",
        "",
    ]
    if not classification.risk_reasons:
        lines.extend(["No explicit risk reasons recorded.", ""])
    else:
        for reason in classification.risk_reasons:
            lines.extend(
                [
                    f"### {reason.id} - {reason.message}",
                    "",
                    f"- severity: `{reason.severity}`",
                    f"- category: `{reason.category}`",
                    f"- file: `{reason.file or '(none)'}`",
                    f"- reviewer_profiles: `{','.join(reason.reviewer_profiles) or '(none)'}`",
                    "",
                ]
            )
    lines.extend(["## Policy notes", "", *[f"- {note}" for note in classification.policy_notes]])
    return "\n".join(lines).rstrip() + "\n"


def write_risk_classification_artifacts(
    *,
    run_id: str,
    runs_dir: str | Path,
    classification: RiskClassification,
) -> tuple[Path, Path]:
    if classification.run_id != run_id:
        raise ValueError(
            f"risk classification run_id does not match target run id: {classification.run_id} != {run_id}"
        )
    run_dir = Path(runs_dir) / run_id
    json_path = run_dir / "RISK_CLASSIFICATION.json"
    markdown_path = run_dir / "RISK_CLASSIFICATION.md"
    json_path.write_text(
        json.dumps(classification.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(build_risk_classification_markdown(classification), encoding="utf-8")
    return json_path.resolve(), markdown_path.resolve()


def load_run_risk_classification(run_dir: str | Path) -> RiskClassification | None:
    json_path = Path(run_dir) / "RISK_CLASSIFICATION.json"
    if not json_path.exists():
        return None
    return RiskClassification.model_validate_json(json_path.read_text(encoding="utf-8-sig"))


def classify_run_risk(
    *,
    run_id: str,
    runs_dir: str | Path,
    force: bool = False,
) -> RiskClassificationResult:
    runs_dir_path = Path(runs_dir)
    run_dir = runs_dir_path / run_id
    if not run_dir.exists():
        raise FileNotFoundError(f"run does not exist: {run_id}")

    state = load_run_state(run_dir)
    if not state.executions:
        raise ValueError("run has no executions")
    loaded_report = load_structured_report(state.executions[-1])
    if loaded_report.report is None:
        if loaded_report.error:
            raise ValueError(loaded_report.error)
        raise ValueError("cannot classify run risk without a valid EXECUTION_REPORT.json")

    json_path = run_dir / "RISK_CLASSIFICATION.json"
    markdown_path = run_dir / "RISK_CLASSIFICATION.md"
    if (json_path.exists() or markdown_path.exists()) and not force:
        raise ValueError("Risk classification already recorded. Pass --force to overwrite it.")

    classification = classify_changed_files(
        run_id=run_id,
        changed_files=list(loaded_report.report.changed_files),
        task=state,
    )
    written_json_path, written_markdown_path = write_risk_classification_artifacts(
        run_id=run_id,
        runs_dir=runs_dir_path,
        classification=classification,
    )

    state.risk_classification_path = str(written_json_path)
    state.risk_level = classification.risk_level
    state.change_type = classification.change_type
    state.required_review_profiles = list(classification.required_review_profiles)
    state.optional_review_profiles = list(classification.optional_review_profiles)
    state.touch()
    state.save_json(run_dir / "state.json")

    return RiskClassificationResult(
        run_id=run_id,
        classification=classification,
        risk_classification_path=written_json_path,
        risk_classification_markdown_path=written_markdown_path,
        state_path=(run_dir / "state.json").resolve(),
    )
