from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import re

from ai_orchestrator.apply import load_run_state
from ai_orchestrator.review_findings import RecordFindingsResult, persist_review_findings_report
from ai_orchestrator.schemas import ReviewFinding, ReviewFindingsReport, RunState, StructuredExecutionReport
from ai_orchestrator.validation import _normalize_manifest_path, load_structured_report

SUPPORTED_REVIEW_PROFILES = {"default", "docs-only", "code-safety"}
_HIGH_RISK_FILES = {
    "src/ai_orchestrator/apply.py",
    "src/ai_orchestrator/review.py",
    "src/ai_orchestrator/review_decision.py",
    "src/ai_orchestrator/rework.py",
    "src/ai_orchestrator/validation.py",
    "src/ai_orchestrator/engine.py",
    "src/ai_orchestrator/backends/codex_cli.py",
    "src/ai_orchestrator/schemas.py",
}
_RUNTIME_DIR_NAMES = {
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".runs",
    ".tmp_tests",
    "node_modules",
    ".venv",
}
_RUNTIME_SUFFIXES = {".pyc", ".pyo"}
_DRIVE_PREFIX_RE = re.compile(r"^[a-zA-Z]:[\\/]")


@dataclass(frozen=True)
class DeterministicReviewCheckResult:
    category: str
    severity: str
    title: str
    evidence: str
    required_action: str
    file: str | None = None
    line: int | None = None
    reviewer: str = "deterministic"
    status: str = "open"


@dataclass(frozen=True)
class UnsafeChangedPath:
    raw_path: str
    reason: str


@dataclass(frozen=True)
class DeterministicReviewContext:
    run_id: str
    run_dir: Path
    state: RunState
    report: StructuredExecutionReport
    workspace_dir: Path
    profiles: tuple[str, ...]
    raw_changed_files: list[str]
    normalized_changed_files: list[str]
    unsafe_paths: list[UnsafeChangedPath]
    docs_only_change: bool


@dataclass(frozen=True)
class DeterministicReviewRunResult:
    run_id: str
    profiles: tuple[str, ...]
    report: ReviewFindingsReport
    persisted: RecordFindingsResult


def _normalize_profiles(profiles: list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
    requested = list(profiles or ["default"])
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in requested:
        value = raw.strip().lower()
        if not value:
            continue
        if value not in SUPPORTED_REVIEW_PROFILES:
            supported = ", ".join(sorted(SUPPORTED_REVIEW_PROFILES))
            raise ValueError(f"unsupported review profile: {raw}. Supported profiles: {supported}")
        if value not in seen:
            seen.add(value)
            normalized.append(value)
    return tuple(normalized or ["default"])


def _profile_checks(profiles: tuple[str, ...]) -> set[str]:
    enabled: set[str] = set()
    mapping = {
        "default": {"runtime", "report_missing", "source_without_tests", "tests_without_source_or_docs", "high_risk", "breadth", "path_safety"},
        "docs-only": {"runtime", "report_missing", "tests_without_source_or_docs", "breadth", "path_safety"},
        "code-safety": {"runtime", "report_missing", "source_without_tests", "high_risk", "breadth", "path_safety"},
    }
    for profile in profiles:
        enabled.update(mapping[profile])
    return enabled


def _is_unsafe_changed_path(raw_path: str) -> str | None:
    stripped = raw_path.strip()
    if not stripped:
        return "path is empty"
    if _DRIVE_PREFIX_RE.match(stripped):
        return "path uses an absolute drive-qualified location"
    if stripped.startswith(("/", "\\")):
        return "path starts with a root slash"
    normalized = stripped.replace("\\", "/")
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    path = Path(normalized)
    if path.is_absolute():
        return "path is absolute"
    if ".." in path.parts:
        return "path escapes the workspace via '..'"
    return None


def _is_runtime_or_generated_path(path: str) -> bool:
    rel = Path(path)
    if rel.suffix.lower() in _RUNTIME_SUFFIXES:
        return True
    for part in rel.parts:
        lowered = part.lower()
        if lowered in _RUNTIME_DIR_NAMES or lowered.endswith(".egg-info"):
            return True
    return False


def _is_source_python(path: str) -> bool:
    return path.startswith("src/") and path.endswith(".py")


def _is_test_python(path: str) -> bool:
    return path.startswith("tests/") and path.endswith(".py")


def _is_docs_markdown(path: str) -> bool:
    return path.startswith("docs/") and path.endswith(".md")


def _is_execution_report(path: str) -> bool:
    return Path(path).name == "EXECUTION_REPORT.json"


def _build_context(
    *,
    run_id: str,
    runs_dir: str | Path,
    profiles: tuple[str, ...],
) -> DeterministicReviewContext:
    run_dir = Path(runs_dir) / run_id
    if not run_dir.exists():
        raise FileNotFoundError(f"run does not exist: {run_id}")

    state = load_run_state(run_dir)
    if not state.executions:
        raise ValueError("run has no executions")
    loaded_report = load_structured_report(state.executions[-1])
    if loaded_report.report is None:
        if loaded_report.error:
            raise ValueError(loaded_report.error)
        raise ValueError("cannot run deterministic review checks without a valid EXECUTION_REPORT.json")
    if loaded_report.source_path is None:
        raise ValueError("cannot infer run workspace from EXECUTION_REPORT.json")

    workspace_dir = loaded_report.source_path.parent
    raw_changed_files = list(loaded_report.report.changed_files)
    unsafe_paths: list[UnsafeChangedPath] = []
    normalized_changed_files: list[str] = []
    for raw_path in raw_changed_files:
        issue = _is_unsafe_changed_path(raw_path)
        if issue is not None:
            unsafe_paths.append(UnsafeChangedPath(raw_path=raw_path, reason=issue))
            continue
        normalized = _normalize_manifest_path(raw_path)
        if normalized:
            normalized_changed_files.append(normalized)

    non_report_changed_files = [path for path in normalized_changed_files if not _is_execution_report(path)]
    docs_only_change = bool(non_report_changed_files) and all(_is_docs_markdown(path) for path in non_report_changed_files)
    return DeterministicReviewContext(
        run_id=run_id,
        run_dir=run_dir,
        state=state,
        report=loaded_report.report,
        workspace_dir=workspace_dir,
        profiles=profiles,
        raw_changed_files=raw_changed_files,
        normalized_changed_files=normalized_changed_files,
        unsafe_paths=unsafe_paths,
        docs_only_change=docs_only_change,
    )


def _check_runtime_generated_files(context: DeterministicReviewContext) -> list[DeterministicReviewCheckResult]:
    findings: list[DeterministicReviewCheckResult] = []
    for path in context.normalized_changed_files:
        if not _is_runtime_or_generated_path(path):
            continue
        findings.append(
            DeterministicReviewCheckResult(
                category="ops",
                severity="critical",
                title="Runtime/generated file listed as changed",
                evidence=f"`{path}` is a runtime/generated path and should not be reported as a durable change.",
                required_action="Remove generated/runtime files from changed_files and ensure they are not applied.",
                file=path,
            )
        )
    return findings


def _check_execution_report_missing(context: DeterministicReviewContext) -> list[DeterministicReviewCheckResult]:
    report_path = context.workspace_dir / "EXECUTION_REPORT.json"
    if not report_path.exists():
        return []
    normalized_set = {path.lower() for path in context.normalized_changed_files}
    if "execution_report.json" in normalized_set:
        return []
    return [
        DeterministicReviewCheckResult(
            category="correctness",
            severity="major",
            title="EXECUTION_REPORT.json missing from changed_files",
            evidence="The workspace contains EXECUTION_REPORT.json, but changed_files does not list it.",
            required_action="Include EXECUTION_REPORT.json in changed_files whenever the report is created or updated.",
            file="EXECUTION_REPORT.json",
        )
    ]


def _check_source_without_tests(context: DeterministicReviewContext) -> list[DeterministicReviewCheckResult]:
    source_files = [path for path in context.normalized_changed_files if _is_source_python(path)]
    has_tests = any(_is_test_python(path) for path in context.normalized_changed_files)
    if not source_files or has_tests:
        return []
    evidence = ", ".join(f"`{path}`" for path in source_files[:5])
    if len(source_files) > 5:
        evidence += ", ..."
    return [
        DeterministicReviewCheckResult(
            category="qa",
            severity="major",
            title="Source code changed without test changes",
            evidence=f"Source files changed without any tests/**/*.py updates: {evidence}.",
            required_action="Add or update tests, or explicitly justify why existing tests cover the change.",
            file=source_files[0],
        )
    ]


def _check_tests_without_source_or_docs(context: DeterministicReviewContext) -> list[DeterministicReviewCheckResult]:
    test_files = [path for path in context.normalized_changed_files if _is_test_python(path)]
    has_source = any(_is_source_python(path) for path in context.normalized_changed_files)
    has_docs = any(_is_docs_markdown(path) for path in context.normalized_changed_files)
    if not test_files or has_source or has_docs:
        return []
    evidence = ", ".join(f"`{path}`" for path in test_files[:5])
    if len(test_files) > 5:
        evidence += ", ..."
    return [
        DeterministicReviewCheckResult(
            category="qa",
            severity="minor",
            title="Tests changed without source or documentation changes",
            evidence=f"Tests changed, but no source or docs files changed: {evidence}.",
            required_action="Confirm the tests are intentional or add the corresponding source/docs context.",
            file=test_files[0],
        )
    ]


def _check_high_risk_files(context: DeterministicReviewContext) -> list[DeterministicReviewCheckResult]:
    findings: list[DeterministicReviewCheckResult] = []
    for path in context.normalized_changed_files:
        if path.lower() not in _HIGH_RISK_FILES:
            continue
        findings.append(
            DeterministicReviewCheckResult(
                category="security",
                severity="major",
                title="High-risk orchestration/safety file changed",
                evidence=f"`{path}` is part of the orchestration/apply/review safety boundary.",
                required_action="Require focused architecture/safety review before approval.",
                file=path,
            )
        )
    return findings


def _check_broad_change(context: DeterministicReviewContext) -> list[DeterministicReviewCheckResult]:
    count = len(context.report.changed_files)
    if count > 10:
        return [
            DeterministicReviewCheckResult(
                category="maintainability",
                severity="major",
                title="Broad change surface",
                evidence=f"The report lists {count} changed files, which exceeds the hard-validation threshold.",
                required_action="Split into smaller tasks or justify the broad change surface.",
            )
        ]
    if count > 5:
        return [
            DeterministicReviewCheckResult(
                category="maintainability",
                severity="minor",
                title="Moderately broad change surface",
                evidence=f"The report lists {count} changed files, which is larger than a typical focused change.",
                required_action="Confirm the broader scope is necessary or split follow-up work into smaller tasks.",
            )
        ]
    return []


def _check_unsafe_paths(context: DeterministicReviewContext) -> list[DeterministicReviewCheckResult]:
    findings: list[DeterministicReviewCheckResult] = []
    for item in context.unsafe_paths:
        findings.append(
            DeterministicReviewCheckResult(
                category="security",
                severity="critical",
                title="Unsafe changed file path",
                evidence=f"`{item.raw_path}` is unsafe because {item.reason}.",
                required_action="Normalize changed_files to workspace-relative safe paths only.",
            )
        )
    return findings


def _run_checks(context: DeterministicReviewContext) -> list[DeterministicReviewCheckResult]:
    enabled_checks = _profile_checks(context.profiles)
    findings: list[DeterministicReviewCheckResult] = []
    if "path_safety" in enabled_checks:
        findings.extend(_check_unsafe_paths(context))
    if "runtime" in enabled_checks:
        findings.extend(_check_runtime_generated_files(context))
    if "report_missing" in enabled_checks:
        findings.extend(_check_execution_report_missing(context))
    if "source_without_tests" in enabled_checks:
        findings.extend(_check_source_without_tests(context))
    if "tests_without_source_or_docs" in enabled_checks:
        findings.extend(_check_tests_without_source_or_docs(context))
    if "high_risk" in enabled_checks:
        findings.extend(_check_high_risk_files(context))
    if "breadth" in enabled_checks:
        findings.extend(_check_broad_change(context))
    return findings


def build_findings_report_from_checks(
    *,
    context: DeterministicReviewContext,
    checks: list[DeterministicReviewCheckResult],
) -> ReviewFindingsReport:
    findings = [
        ReviewFinding(
            id=f"F{index:03d}",
            reviewer=check.reviewer,
            category=check.category,
            severity=check.severity,
            title=check.title,
            evidence=check.evidence,
            required_action=check.required_action,
            file=check.file,
            line=check.line,
            status=check.status,
        )
        for index, check in enumerate(checks, start=1)
    ]

    if any(finding.severity == "critical" and finding.status == "open" for finding in findings):
        overall_decision = "blocked"
    elif any(finding.severity == "major" and finding.status == "open" for finding in findings):
        overall_decision = "needs_rework"
    else:
        overall_decision = "pass"

    if findings:
        summary = (
            f"Deterministic review checks recorded {len(findings)} finding(s) "
            f"for profiles: {', '.join(context.profiles)}."
        )
    elif context.docs_only_change:
        summary = (
            "No deterministic review findings. Classified as a docs-only change "
            f"for profiles: {', '.join(context.profiles)}."
        )
    else:
        summary = f"No deterministic review findings for profiles: {', '.join(context.profiles)}."

    return ReviewFindingsReport(
        run_id=context.run_id,
        created_at=datetime.now(timezone.utc),
        summary=summary,
        findings=findings,
        overall_decision=overall_decision,
        source_profile="deterministic",
        source_kind="deterministic",
    )


def run_deterministic_review_checks(
    *,
    run_id: str,
    runs_dir: str | Path,
    profiles: list[str] | tuple[str, ...] | None = None,
    force: bool = False,
) -> DeterministicReviewRunResult:
    normalized_profiles = _normalize_profiles(profiles)
    context = _build_context(run_id=run_id, runs_dir=runs_dir, profiles=normalized_profiles)
    checks = _run_checks(context)
    report = build_findings_report_from_checks(context=context, checks=checks)
    persisted = persist_review_findings_report(
        run_id=run_id,
        runs_dir=runs_dir,
        report=report,
        force=force,
    )
    return DeterministicReviewRunResult(
        run_id=run_id,
        profiles=normalized_profiles,
        report=report,
        persisted=persisted,
    )
