from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ai_orchestrator.task_drafts import (
    TaskDraft,
    TaskDraftManifest,
    _load_yaml_module,
    load_task_draft_manifest,
    save_task_draft_manifest,
)

_VALIDATION_STATUSES = {"valid", "needs_revision", "invalid"}
_DANGEROUS_COMMAND_SNIPPETS = (
    "git push",
    "git reset --hard",
    "git clean -fdx",
    "rm -rf /",
    "del /s",
    "format",
)
_SAFETY_CRITICAL_FORBIDDEN = (
    "src/ai_orchestrator/apply.py",
    "src/ai_orchestrator/review.py",
    "src/ai_orchestrator/review_decision.py",
    "src/ai_orchestrator/validation.py",
    "src/ai_orchestrator/backends/codex_cli.py",
)
_REQUIRED_ARTIFACTS = (
    "raw_request.md",
    "task_draft.yaml",
    "codex_prompt.md",
    "task_review.md",
    "MANIFEST.json",
)


class TaskDraftValidationFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    severity: str
    category: str
    message: str
    field: str | None = None
    required_action: str | None = None
    blocking: bool | None = None

    @field_validator("id", "severity", "category", "message")
    @classmethod
    def required_strings_not_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("task draft validation field must not be empty")
        return value

    @field_validator("field", "required_action")
    @classmethod
    def optional_strings_blank_to_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None

    @field_validator("severity")
    @classmethod
    def severity_must_be_allowed(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"error", "warning", "info"}:
            raise ValueError("severity must be one of: error, warning, info")
        return normalized

    @field_validator("blocking")
    @classmethod
    def blocking_default_from_severity(cls, value: bool | None, info) -> bool:
        if value is not None:
            return value
        severity = info.data.get("severity")
        return severity == "error"


class TaskDraftValidationCounts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total: int
    errors: int
    warnings: int
    info: int
    blocking: int


class TaskDraftValidationReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0"
    draft_id: str
    created_at: datetime
    validation_status: str
    valid_for_promotion: bool
    findings: list[TaskDraftValidationFinding] = Field(default_factory=list)
    counts: TaskDraftValidationCounts

    @field_validator("draft_id", "validation_status")
    @classmethod
    def required_strings_not_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("task draft validation report field must not be empty")
        return value

    @field_validator("validation_status")
    @classmethod
    def validation_status_is_allowed(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in _VALIDATION_STATUSES:
            allowed = ", ".join(sorted(_VALIDATION_STATUSES))
            raise ValueError(f"validation_status must be one of: {allowed}")
        return normalized


@dataclass(frozen=True)
class TaskDraftValidationResult:
    draft_id: str
    report: TaskDraftValidationReport
    report_path: Path
    report_markdown_path: Path
    manifest_path: Path


def _finding(
    findings: list[TaskDraftValidationFinding],
    *,
    counter: int,
    severity: str,
    category: str,
    message: str,
    field: str | None = None,
    required_action: str | None = None,
) -> int:
    findings.append(
        TaskDraftValidationFinding(
            id=f"TD{counter:03d}",
            severity=severity,
            category=category,
            message=message,
            field=field,
            required_action=required_action,
        )
    )
    return counter + 1


def _compute_counts(findings: list[TaskDraftValidationFinding]) -> TaskDraftValidationCounts:
    errors = sum(1 for finding in findings if finding.severity == "error")
    warnings = sum(1 for finding in findings if finding.severity == "warning")
    info = sum(1 for finding in findings if finding.severity == "info")
    blocking = sum(1 for finding in findings if finding.blocking)
    return TaskDraftValidationCounts(
        total=len(findings),
        errors=errors,
        warnings=warnings,
        info=info,
        blocking=blocking,
    )


def _status_from_counts(counts: TaskDraftValidationCounts) -> tuple[str, bool]:
    if counts.errors > 0:
        return "invalid", False
    if counts.warnings > 0:
        return "needs_revision", False
    return "valid", True


def _load_yaml_payload(path: Path) -> object:
    yaml = _load_yaml_module()
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _format_validation_error_location(loc: tuple[object, ...]) -> str:
    return ".".join(str(part) for part in loc if str(part)) or "task_draft"


def _category_for_field(field: str) -> str:
    if field.startswith("commands_to_run"):
        return "commands"
    if field.startswith("required_review_profiles") or field.startswith("optional_review_profiles"):
        return "reviewers"
    if field.startswith("risk_level"):
        return "risk"
    if field.startswith("target_task"):
        return "promotion"
    if field.startswith("files_allowed") or field.startswith("files_forbidden"):
        return "scope"
    return "schema"


def _contains_dangerous_command(command: str) -> str | None:
    command_lower = command.strip().lower()
    for snippet in _DANGEROUS_COMMAND_SNIPPETS:
        if snippet in command_lower:
            return snippet
    return None


def _validate_prompt_text(
    draft: TaskDraft,
    prompt_text: str,
    findings: list[TaskDraftValidationFinding],
    counter: int,
) -> int:
    checks = [
        ("objective", draft.objective, "codex_prompt.md must include the draft objective"),
        ("non_goals", "## Non-goals", "codex_prompt.md must include non_goals guidance"),
        ("files_forbidden", "## Files forbidden", "codex_prompt.md must include files_forbidden guidance"),
        ("tests_required", "## Tests required", "codex_prompt.md must include tests_required guidance"),
        ("commands_to_run", "## Commands to run", "codex_prompt.md must include commands_to_run guidance"),
        ("acceptance_criteria", "## Acceptance criteria", "codex_prompt.md must include acceptance_criteria guidance"),
        (
            "EXECUTION_REPORT.json",
            "EXECUTION_REPORT.json",
            "codex_prompt.md must instruct the future executor to create EXECUTION_REPORT.json",
        ),
        ("commit", "Do not commit", "codex_prompt.md must instruct the future executor not to commit"),
    ]
    for field_name, marker, message in checks:
        if marker not in prompt_text:
            counter = _finding(
                findings,
                counter=counter,
                severity="error",
                category="artifacts",
                message=message,
                field=field_name,
                required_action="Update codex_prompt.md so the missing execution constraint is explicit.",
            )
    return counter


def _validate_task_review_text(
    review_text: str,
    findings: list[TaskDraftValidationFinding],
    counter: int,
) -> int:
    checks = [
        ("risk_level", "Risk level", "task_review.md should mention risk_level explicitly"),
        ("open_questions", "Open questions", "task_review.md should mention open_questions explicitly"),
        ("files_allowed", "Files allowed", "task_review.md should mention files_allowed explicitly"),
        ("files_forbidden", "Files forbidden", "task_review.md should mention files_forbidden explicitly"),
    ]
    for field_name, marker, message in checks:
        if marker not in review_text:
            counter = _finding(
                findings,
                counter=counter,
                severity="warning",
                category="artifacts",
                message=message,
                field=field_name,
                required_action="Update task_review.md so the reviewer checklist covers this field directly.",
            )
    return counter


def _validate_parsed_draft(
    draft: TaskDraft,
    findings: list[TaskDraftValidationFinding],
    counter: int,
) -> int:
    required_list_fields = {
        "non_goals": draft.non_goals,
        "files_forbidden": draft.files_forbidden,
        "invariants": draft.invariants,
        "tests_required": draft.tests_required,
        "commands_to_run": draft.commands_to_run,
        "acceptance_criteria": draft.acceptance_criteria,
        "validation_requirements": draft.validation_requirements,
        "rollback_notes": draft.rollback_notes,
    }
    for field_name, values in required_list_fields.items():
        if not values:
            counter = _finding(
                findings,
                counter=counter,
                severity="error",
                category="schema",
                message=f"{field_name} must not be empty",
                field=field_name,
                required_action=f"Populate {field_name} with at least one concrete item before promotion.",
            )

    if not draft.title:
        counter = _finding(findings, counter=counter, severity="error", category="schema", message="title must not be empty", field="title")
    if not draft.objective:
        counter = _finding(findings, counter=counter, severity="error", category="schema", message="objective must not be empty", field="objective")

    if draft.target_task.enabled:
        counter = _finding(
            findings,
            counter=counter,
            severity="error",
            category="promotion",
            message="target_task.enabled must remain false during draft validation",
            field="target_task.enabled",
            required_action="Set target_task.enabled to false until a later promotion stage explicitly allows enabling it.",
        )
    if draft.target_task.backend not in {"mock", "codex_cli", "codex"}:
        counter = _finding(
            findings,
            counter=counter,
            severity="error",
            category="promotion",
            message=f"target_task.backend is not allowed: {draft.target_task.backend}",
            field="target_task.backend",
            required_action="Use one of the supported backends: mock, codex_cli, codex.",
        )
    if not draft.target_task.seed_workspace.strip():
        counter = _finding(findings, counter=counter, severity="error", category="promotion", message="target_task.seed_workspace must not be empty", field="target_task.seed_workspace")
    if not draft.target_task.require_structured_report:
        counter = _finding(findings, counter=counter, severity="error", category="promotion", message="target_task.require_structured_report must be true", field="target_task.require_structured_report")
    if not draft.target_task.rerun_report_test_commands:
        counter = _finding(findings, counter=counter, severity="error", category="promotion", message="target_task.rerun_report_test_commands must be true", field="target_task.rerun_report_test_commands")
    if not draft.target_task.validate_workspace_manifest:
        counter = _finding(findings, counter=counter, severity="error", category="promotion", message="target_task.validate_workspace_manifest must be true", field="target_task.validate_workspace_manifest")

    if not draft.files_allowed:
        counter = _finding(
            findings,
            counter=counter,
            severity="warning",
            category="scope",
            message="files_allowed is empty and should be narrowed before promotion",
            field="files_allowed",
            required_action="Replace the empty placeholder with one or more specific allowed paths or directories.",
        )
    elif any(item in {".", "*"} for item in draft.files_allowed):
        counter = _finding(
            findings,
            counter=counter,
            severity="warning",
            category="scope",
            message="files_allowed contains an overly broad placeholder",
            field="files_allowed",
            required_action="Replace broad values like '.' or '*' with a narrow reviewed scope.",
        )

    missing_forbidden = [path for path in _SAFETY_CRITICAL_FORBIDDEN if path not in draft.files_forbidden]
    if missing_forbidden:
        counter = _finding(
            findings,
            counter=counter,
            severity="warning",
            category="safety",
            message="files_forbidden does not include every safety-critical path",
            field="files_forbidden",
            required_action="Add the missing safety-critical paths to files_forbidden before promotion.",
        )

    if not any("python -m unittest discover -s tests" in command for command in draft.commands_to_run):
        counter = _finding(
            findings,
            counter=counter,
            severity="warning",
            category="commands",
            message="commands_to_run should include `python -m unittest discover -s tests`",
            field="commands_to_run",
            required_action="Add the standard unit test command or justify an equivalent deterministic test command.",
        )

    if not any(item == "report.status=completed" for item in draft.acceptance_criteria):
        counter = _finding(
            findings,
            counter=counter,
            severity="error",
            category="promotion",
            message="acceptance_criteria must include report.status=completed",
            field="acceptance_criteria",
            required_action="Add report.status=completed to acceptance_criteria.",
        )
    if not any(item == "tests.status=passed" for item in draft.acceptance_criteria):
        counter = _finding(
            findings,
            counter=counter,
            severity="error",
            category="promotion",
            message="acceptance_criteria must include tests.status=passed",
            field="acceptance_criteria",
            required_action="Add tests.status=passed to acceptance_criteria.",
        )
    if not any("changed_files" in item for item in draft.acceptance_criteria):
        counter = _finding(
            findings,
            counter=counter,
            severity="warning",
            category="promotion",
            message="acceptance_criteria should mention changed_files scope",
            field="acceptance_criteria",
            required_action="Add an acceptance criterion that constrains changed_files to the reviewed scope.",
        )

    if draft.risk_level == "unknown":
        counter = _finding(
            findings,
            counter=counter,
            severity="warning",
            category="risk",
            message="risk_level is unknown",
            field="risk_level",
            required_action="Refine risk_level before promotion so reviewer requirements are explicit.",
        )
    if draft.risk_level in {"high", "critical"} and not draft.required_review_profiles:
        counter = _finding(
            findings,
            counter=counter,
            severity="warning",
            category="reviewers",
            message="high/critical risk draft has no required_review_profiles",
            field="required_review_profiles",
            required_action="Add at least one required reviewer profile for high or critical risk drafts.",
        )

    if draft.open_questions:
        counter = _finding(
            findings,
            counter=counter,
            severity="warning",
            category="promotion",
            message="open_questions must be resolved before promotion",
            field="open_questions",
            required_action="Resolve or remove open_questions before the draft is considered promotable.",
        )

    return counter


def build_task_draft_validation_markdown(report: TaskDraftValidationReport) -> str:
    lines = [
        f"# Task Draft Validation Report: {report.draft_id}",
        "",
        f"- Validation status: `{report.validation_status}`",
        f"- Valid for promotion: `{str(report.valid_for_promotion).lower()}`",
        f"- Total findings: `{report.counts.total}`",
        f"- Errors: `{report.counts.errors}`",
        f"- Warnings: `{report.counts.warnings}`",
        f"- Info: `{report.counts.info}`",
        f"- Blocking: `{report.counts.blocking}`",
        "",
        "## Findings",
        "",
    ]
    if not report.findings:
        lines.extend(["No findings.", ""])
    else:
        for finding in report.findings:
            lines.extend(
                [
                    f"### {finding.id} — {finding.severity}",
                    "",
                    f"- Category: `{finding.category}`",
                    f"- Field: `{finding.field or '(none)'}`",
                    f"- Blocking: `{str(finding.blocking).lower()}`",
                    f"- Message: {finding.message}",
                    f"- Required action: {finding.required_action or '(none)'}",
                    "",
                ]
            )

    if report.valid_for_promotion:
        lines.extend(
            [
                "## Next action",
                "",
                "promote-task-draft may be allowed in a later stage",
            ]
        )
    else:
        lines.extend(
            [
                "## Next action",
                "",
                "revise draft before promotion",
            ]
        )
    return "\n".join(lines) + "\n"


def _load_manifest_or_fail(manifest_path: Path) -> TaskDraftManifest:
    if not manifest_path.exists():
        raise FileNotFoundError("required draft artifact missing: MANIFEST.json")
    return load_task_draft_manifest(manifest_path)


def validate_task_draft(
    *,
    draft_id: str,
    drafts_dir: str | Path = ".task_drafts",
    force: bool = False,
) -> TaskDraftValidationResult:
    drafts_root = Path(drafts_dir)
    draft_dir = drafts_root / draft_id
    if not draft_dir.exists():
        raise FileNotFoundError(f"task draft not found: {draft_id}")

    report_path = draft_dir / "task_draft_validator_report.json"
    report_markdown_path = draft_dir / "task_draft_validator_report.md"
    if (report_path.exists() or report_markdown_path.exists()) and not force:
        raise ValueError("task draft validator report already exists; pass --force to overwrite it")

    manifest_path = draft_dir / "MANIFEST.json"
    manifest = _load_manifest_or_fail(manifest_path)

    findings: list[TaskDraftValidationFinding] = []
    counter = 1
    artifact_paths = {name: draft_dir / name for name in _REQUIRED_ARTIFACTS}
    for artifact_name in _REQUIRED_ARTIFACTS:
        if not artifact_paths[artifact_name].exists():
            counter = _finding(
                findings,
                counter=counter,
                severity="error",
                category="artifacts",
                message=f"required draft artifact missing: {artifact_name}",
                field=artifact_name,
                required_action=f"Restore or regenerate {artifact_name} before promotion.",
            )

    raw_payload: object | None = None
    draft: TaskDraft | None = None
    if artifact_paths["task_draft.yaml"].exists():
        try:
            raw_payload = _load_yaml_payload(artifact_paths["task_draft.yaml"])
        except Exception as exc:  # noqa: BLE001 - surfaced as deterministic validation finding.
            counter = _finding(
                findings,
                counter=counter,
                severity="error",
                category="schema",
                message=f"task_draft.yaml could not be parsed: {exc}",
                field="task_draft.yaml",
                required_action="Fix YAML syntax so the draft can be parsed deterministically.",
            )
        else:
            try:
                draft = TaskDraft.model_validate(raw_payload)
            except ValidationError as exc:
                for error in exc.errors():
                    field_name = _format_validation_error_location(error.get("loc", ()))
                    counter = _finding(
                        findings,
                        counter=counter,
                        severity="error",
                        category=_category_for_field(field_name),
                        message=f"task_draft.yaml validation error for {field_name}: {error.get('msg', 'validation error')}",
                        field=field_name,
                        required_action="Fix the invalid draft field and rerun validate-task-draft.",
                    )

    if draft is not None:
        counter = _validate_parsed_draft(draft, findings, counter)

    if artifact_paths["codex_prompt.md"].exists() and draft is not None:
        prompt_text = artifact_paths["codex_prompt.md"].read_text(encoding="utf-8", errors="replace")
        counter = _validate_prompt_text(draft, prompt_text, findings, counter)

    if artifact_paths["task_review.md"].exists():
        review_text = artifact_paths["task_review.md"].read_text(encoding="utf-8", errors="replace")
        counter = _validate_task_review_text(review_text, findings, counter)

    counts = _compute_counts(findings)
    validation_status, valid_for_promotion = _status_from_counts(counts)
    report = TaskDraftValidationReport(
        draft_id=draft_id,
        created_at=datetime.now(),
        validation_status=validation_status,
        valid_for_promotion=valid_for_promotion,
        findings=findings,
        counts=counts,
    )

    report_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    report_markdown_path.write_text(build_task_draft_validation_markdown(report), encoding="utf-8")

    updated_manifest = manifest.model_copy(
        update={
            "validator_report": str(report_path.resolve()),
            "validator_report_md": str(report_markdown_path.resolve()),
            "validation_status": report.validation_status,
            "valid_for_promotion": report.valid_for_promotion,
            "validated_at": report.created_at,
            "validation_stale_reason": None,
        }
    )
    save_task_draft_manifest(manifest_path, updated_manifest)

    return TaskDraftValidationResult(
        draft_id=draft_id,
        report=report,
        report_path=report_path.resolve(),
        report_markdown_path=report_markdown_path.resolve(),
        manifest_path=manifest_path.resolve(),
    )
