from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import re
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ai_orchestrator.schemas import normalize_safe_relative_path

_TASK_DRAFT_RISK_LEVELS = {"low", "medium", "high", "critical", "unknown"}
_DANGEROUS_COMMAND_SNIPPETS = (
    "rm -rf /",
    "del /s",
    "format",
    "git push",
    "git reset --hard",
    "git clean -fdx",
)
_DEFAULT_FORBIDDEN_PATHS = (
    "src/ai_orchestrator/apply.py",
    "src/ai_orchestrator/review.py",
    "src/ai_orchestrator/review_decision.py",
    "src/ai_orchestrator/validation.py",
    "src/ai_orchestrator/backends/codex_cli.py",
)


def _load_yaml_module():
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - runtime environment guard.
        raise RuntimeError(
            "PyYAML is required to create task drafts. Install project dependencies with `python -m pip install -e .`."
        ) from exc
    return yaml


class TargetTaskDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    enabled: bool = False
    backend: Literal["mock", "codex_cli", "codex"] = "codex_cli"
    seed_workspace: str = "."
    require_structured_report: bool = True
    rerun_report_test_commands: bool = True
    validate_workspace_manifest: bool = True
    validation_command_timeout: int = Field(default=60, ge=1, le=600)
    stream_codex_output: bool = True
    verbose: bool = True
    commit_message: str | None = None

    @field_validator("id", "title")
    @classmethod
    def required_strings_not_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("target_task required field must not be empty")
        return value

    @field_validator("seed_workspace")
    @classmethod
    def seed_workspace_not_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("target_task.seed_workspace must not be empty")
        return value

    @field_validator("commit_message")
    @classmethod
    def blank_commit_message_to_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None


class TaskDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    draft_id: str
    title: str
    objective: str
    context: str | None = None
    non_goals: list[str] = Field(default_factory=list)
    files_allowed: list[str] = Field(default_factory=list)
    files_forbidden: list[str] = Field(default_factory=list)
    invariants: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    risk_level: Literal["low", "medium", "high", "critical", "unknown"] = "unknown"
    required_review_profiles: list[str] = Field(default_factory=list)
    optional_review_profiles: list[str] = Field(default_factory=list)
    tests_required: list[str] = Field(default_factory=list)
    commands_to_run: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    validation_requirements: list[str] = Field(default_factory=list)
    rollback_notes: list[str] = Field(default_factory=list)
    prompt_language: str = "ru"
    target_task: TargetTaskDraft

    @field_validator("draft_id", "title", "objective")
    @classmethod
    def required_strings_not_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("task draft required field must not be empty")
        return value

    @field_validator("context")
    @classmethod
    def optional_string_blank_to_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None

    @field_validator("prompt_language")
    @classmethod
    def prompt_language_not_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("prompt_language must not be empty")
        return value

    @field_validator(
        "non_goals",
        "files_forbidden",
        "invariants",
        "tests_required",
        "commands_to_run",
        "acceptance_criteria",
        "validation_requirements",
    )
    @classmethod
    def required_string_lists_not_empty(cls, value: list[str]) -> list[str]:
        items = [item.strip() for item in value if item.strip()]
        if not items:
            raise ValueError("task draft required list field must not be empty")
        return items

    @field_validator("assumptions", "open_questions", "rollback_notes")
    @classmethod
    def optional_string_lists_strip(cls, value: list[str]) -> list[str]:
        return [item.strip() for item in value if item.strip()]

    @field_validator("files_allowed")
    @classmethod
    def files_allowed_must_be_safe_relative_or_broad_placeholder(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for raw in value:
            candidate = raw.strip()
            if not candidate:
                continue
            if candidate in {".", "*"}:
                if candidate not in seen:
                    seen.add(candidate)
                    normalized.append(candidate)
                continue
            path = normalize_safe_relative_path(candidate, field_name="task draft file path")
            if path in seen:
                continue
            seen.add(path)
            normalized.append(path)
        return normalized

    @field_validator("files_forbidden")
    @classmethod
    def files_forbidden_must_be_safe_relative(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for raw in value:
            path = normalize_safe_relative_path(raw, field_name="task draft file path")
            if path in seen:
                continue
            seen.add(path)
            normalized.append(path)
        return normalized

    @field_validator("required_review_profiles", "optional_review_profiles")
    @classmethod
    def review_profiles_must_be_known(cls, value: list[str]) -> list[str]:
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

    @field_validator("commands_to_run")
    @classmethod
    def commands_must_not_be_dangerous(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        for raw in value:
            command = raw.strip()
            if not command:
                continue
            command_lower = command.lower()
            for snippet in _DANGEROUS_COMMAND_SNIPPETS:
                if snippet in command_lower:
                    raise ValueError(f"commands_to_run contains dangerous command snippet: {snippet}")
            normalized.append(command)
        if not normalized:
            raise ValueError("commands_to_run must not be empty")
        return normalized


class TaskDraftManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    draft_id: str
    created_at: datetime
    request_source: str
    draft_dir: str
    raw_request: str
    task_draft: str
    codex_prompt: str
    task_review: str
    validator_report: str | None = None
    validator_report_md: str | None = None
    validation_status: str | None = None
    valid_for_promotion: bool | None = None
    validated_at: datetime | None = None
    revised_at: datetime | None = None
    revision_count: int = 0
    last_revision_summary: str | None = None
    validation_stale_reason: str | None = None

    @field_validator("request_source", "draft_dir", "raw_request", "task_draft", "codex_prompt", "task_review")
    @classmethod
    def required_manifest_fields_not_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("manifest required field must not be empty")
        return value

    @field_validator(
        "validator_report",
        "validator_report_md",
        "validation_status",
        "last_revision_summary",
        "validation_stale_reason",
    )
    @classmethod
    def optional_manifest_strings_blank_to_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None

    @field_validator("revision_count")
    @classmethod
    def revision_count_non_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("revision_count must be non-negative")
        return value


@dataclass(frozen=True)
class TaskDraftScaffoldResult:
    draft: TaskDraft
    draft_dir: Path
    raw_request_path: Path
    task_draft_path: Path
    codex_prompt_path: Path
    task_review_path: Path
    manifest_path: Path


@dataclass(frozen=True)
class TaskDraftRevisionResult:
    draft: TaskDraft
    draft_dir: Path
    task_draft_path: Path
    codex_prompt_path: Path
    task_review_path: Path
    manifest_path: Path
    revision_count: int
    validation_status: str
    revision_summary: str


def _build_draft_id() -> str:
    return f"draft_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:6]}"


def _derive_title(raw_request_text: str, draft_id: str) -> str:
    for line in raw_request_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        stripped = stripped.lstrip("#").strip()
        if stripped:
            return stripped
    return f"Draft task {draft_id}"


def _derive_objective(raw_request_text: str, fallback_title: str) -> str:
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", raw_request_text) if part.strip()]
    if not paragraphs:
        return fallback_title
    objective = paragraphs[0]
    if "\n" not in objective:
        objective = objective.lstrip("#").strip()
    return objective[:1000].strip()


def _slugify_task_id(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    return slug[:80]


def _derive_task_id(title: str, draft_id: str) -> str:
    slug = _slugify_task_id(title)
    if slug:
        return slug
    return "draft-task-" + draft_id.replace("_", "-")


def _suggest_files_allowed(raw_request_text: str, title: str) -> list[str]:
    probe = f"{title}\n{raw_request_text}".lower()
    if any(token in probe for token in ("docs/", "documentation", "readme", ".md", "markdown", "doc ")):
        return ["docs"]
    if any(token in probe for token in ("tests/", "pytest", "unittest", "test ")) and not any(
        token in probe for token in ("src/", "source", "python", "code", "module", "cli")
    ):
        return ["tests"]
    if any(token in probe for token in ("src/", "source", "python", "code", "module", "cli")):
        return ["src", "tests", "docs"]
    return []


def _review_profile_defaults(risk_level: str) -> tuple[list[str], list[str]]:
    mapping = {
        "low": ([], ["business", "qa"]),
        "medium": (["qa"], ["architecture"]),
        "high": (["qa", "architecture"], ["ops"]),
        "critical": (["security", "qa", "architecture", "ops"], []),
        "unknown": ([], ["qa", "architecture"]),
    }
    required, optional = mapping[risk_level]
    return list(required), list(optional)


def _build_task_draft(
    *,
    draft_id: str,
    raw_request_text: str,
    title: str | None,
    task_id: str | None,
    risk_level: str,
    prompt_language: str,
) -> TaskDraft:
    parsing_request_text = raw_request_text.lstrip("\ufeff")
    normalized_title = (title or "").strip() or _derive_title(parsing_request_text, draft_id)
    normalized_objective = _derive_objective(parsing_request_text, normalized_title)
    normalized_task_id = (task_id or "").strip() or _derive_task_id(normalized_title, draft_id)
    required_profiles, optional_profiles = _review_profile_defaults(risk_level)
    files_allowed = _suggest_files_allowed(parsing_request_text, normalized_title)

    return TaskDraft(
        draft_id=draft_id,
        title=normalized_title,
        objective=normalized_objective,
        context="Generated from raw_request.md by deterministic scaffold. Requires human review before promotion.",
        non_goals=[
            "Do not make unrelated cleanup changes.",
            "Do not change files outside files_allowed without updating this draft and re-validating.",
            "Do not weaken validation, review, apply, or safety gates.",
        ],
        files_allowed=files_allowed,
        files_forbidden=list(_DEFAULT_FORBIDDEN_PATHS),
        invariants=[
            "Existing tests must continue to pass.",
            "The task must not apply, accept, or commit changes automatically.",
        ],
        assumptions=[
            "This is a deterministic scaffold and may require task-authoring refinement.",
        ],
        open_questions=[
            "Confirm exact files_allowed before promotion.",
            "Confirm whether additional reviewer profiles must be required before promotion.",
        ],
        risk_level=risk_level,
        required_review_profiles=required_profiles,
        optional_review_profiles=optional_profiles,
        tests_required=[
            "Unit tests must be added or updated if source code changes.",
        ],
        commands_to_run=[
            "python -m unittest discover -s tests",
        ],
        acceptance_criteria=[
            "report.status=completed",
            "tests.status=passed",
            "changed_files stay within the reviewed task scope",
        ],
        validation_requirements=[
            "EXECUTION_REPORT.json must be valid.",
            "changed_files must not include files_forbidden.",
            "target_task.enabled must remain false until explicit promotion.",
        ],
        rollback_notes=[
            "Revert files changed by this task.",
        ],
        prompt_language=prompt_language,
        target_task=TargetTaskDraft(
            id=normalized_task_id,
            title=normalized_title,
            enabled=False,
            backend="codex_cli",
            seed_workspace=".",
            require_structured_report=True,
            rerun_report_test_commands=True,
            validate_workspace_manifest=True,
            validation_command_timeout=60,
            stream_codex_output=True,
            verbose=True,
            commit_message=None,
        ),
    )


def render_codex_prompt_markdown(draft: TaskDraft, raw_request_text: str) -> str:
    allowed = ", ".join(draft.files_allowed) if draft.files_allowed else "(not confirmed yet)"
    forbidden = ", ".join(draft.files_forbidden)
    required_profiles = ", ".join(draft.required_review_profiles) if draft.required_review_profiles else "(none)"
    optional_profiles = ", ".join(draft.optional_review_profiles) if draft.optional_review_profiles else "(none)"
    return "\n".join(
        [
            "# Prompt For Future Draft Improvement",
            "",
            "You are helping improve a task draft for ai_orchestrator.",
            "",
            "## Important constraints",
            "",
            "- Do not run the pipeline.",
            "- Do not modify tasks.yaml.",
            "- Do not promote this draft automatically.",
            "- Do not apply or commit changes.",
            "- Improve the draft only, based on evidence from the raw request.",
            "",
            "## Draft summary",
            "",
            f"- Draft id: `{draft.draft_id}`",
            f"- Target task id: `{draft.target_task.id}`",
            f"- Title: `{draft.title}`",
            f"- Risk level: `{draft.risk_level}`",
            f"- Required reviewer profiles: `{required_profiles}`",
            f"- Optional reviewer profiles: `{optional_profiles}`",
            f"- Files allowed: `{allowed}`",
            f"- Files forbidden: `{forbidden}`",
            "",
            "## Objective",
            "",
            draft.objective,
            "",
            "## Non-goals",
            "",
            *[f"- {item}" for item in draft.non_goals],
            "",
            "## Files allowed",
            "",
            *([f"- `{item}`" for item in draft.files_allowed] if draft.files_allowed else ["- (not confirmed yet)"]),
            "",
            "## Files forbidden",
            "",
            *[f"- `{item}`" for item in draft.files_forbidden],
            "",
            "## Tests required",
            "",
            *[f"- {item}" for item in draft.tests_required],
            "",
            "## Commands to run",
            "",
            *[f"- `{item}`" for item in draft.commands_to_run],
            "",
            "## Acceptance criteria",
            "",
            *[f"- `{item}`" for item in draft.acceptance_criteria],
            "",
            "## Validation requirements",
            "",
            *[f"- {item}" for item in draft.validation_requirements],
            "",
            "## Rollback notes",
            "",
            *[f"- {item}" for item in draft.rollback_notes],
            "",
            "## Open questions",
            "",
            *([f"- {item}" for item in draft.open_questions] if draft.open_questions else ["- (none)"]),
            "",
            "## Raw request",
            "",
            "```text",
            raw_request_text.rstrip(),
            "```",
            "",
            "## Improvement tasks",
            "",
            "- Refine files_allowed conservatively.",
            "- Tighten acceptance_criteria and validation_requirements.",
            "- Clarify tests_required and commands_to_run if the request implies more checks.",
            "- Preserve safety invariants and forbidden files unless the human reviewer explicitly changes them.",
            "- Create EXECUTION_REPORT.json in the final execution stage.",
            "- Do not commit changes automatically.",
            "",
            "## Output expectation",
            "",
            "Return improved draft content only. Do not approve or promote it. Keep the draft compatible with task_draft.yaml.",
        ]
    ) + "\n"


def render_task_review_markdown(draft: TaskDraft) -> str:
    required_profiles = ", ".join(draft.required_review_profiles) if draft.required_review_profiles else "(none)"
    optional_profiles = ", ".join(draft.optional_review_profiles) if draft.optional_review_profiles else "(none)"
    allowed = ", ".join(draft.files_allowed) if draft.files_allowed else "(not confirmed yet)"
    forbidden = ", ".join(draft.files_forbidden)
    return "\n".join(
        [
            "# Task Draft Review Checklist",
            "",
            f"Draft id: `{draft.draft_id}`",
            f"Target task id: `{draft.target_task.id}`",
            "",
            "## Human review checklist",
            "",
            f"- [ ] Title matches the request: `{draft.title}`",
            f"- [ ] Objective is precise enough for promotion.",
            f"- [ ] Files allowed are conservative: `{allowed}`",
            "- [ ] Files forbidden still protect review/apply/validation safety boundaries.",
            f"- [ ] Risk level is appropriate: `{draft.risk_level}`",
            f"- [ ] Required reviewer profiles are appropriate: `{required_profiles}`",
            f"- [ ] Optional reviewer profiles are appropriate: `{optional_profiles}`",
            "- [ ] non_goals still reflect the intended boundaries.",
            "- [ ] tests_required and commands_to_run are sufficient.",
            "- [ ] acceptance_criteria are specific enough for validation.",
            "- [ ] rollback_notes are sufficient if the task must be reverted.",
            "- [ ] target_task.enabled remains false until explicit promotion.",
            "",
            "## Notes",
            "",
            f"- Files allowed: `{allowed}`",
            f"- Files forbidden: `{forbidden}`",
            f"- Risk level: `{draft.risk_level}`",
            "- Non-goals:",
            *([f"  - {item}" for item in draft.non_goals] if draft.non_goals else ["  - (none)"]),
            "- Tests required:",
            *([f"  - {item}" for item in draft.tests_required] if draft.tests_required else ["  - (none)"]),
            "- Commands to run:",
            *([f"  - `{item}`" for item in draft.commands_to_run] if draft.commands_to_run else ["  - (none)"]),
            "- Acceptance criteria:",
            *([f"  - `{item}`" for item in draft.acceptance_criteria] if draft.acceptance_criteria else ["  - (none)"]),
            "- Open questions:",
            *([f"  - {item}" for item in draft.open_questions] if draft.open_questions else ["  - (none)"]),
            "- Rollback notes:",
            *([f"  - {item}" for item in draft.rollback_notes] if draft.rollback_notes else ["  - (none)"]),
            "- This scaffold is deterministic and intentionally conservative.",
            "- Promotion to tasks.yaml is a later step and is out of scope for this stage.",
            "- If files_allowed or risk assumptions change, update task_draft.yaml before any validation/promotion step.",
        ]
    ) + "\n"


def _manifest_payload(
    *,
    draft_id: str,
    request_source: Path,
    draft_dir: Path,
    raw_request_path: Path,
    task_draft_path: Path,
    codex_prompt_path: Path,
    task_review_path: Path,
) -> TaskDraftManifest:
    return TaskDraftManifest(
        draft_id=draft_id,
        created_at=datetime.now(),
        request_source=str(request_source.resolve()),
        draft_dir=str(draft_dir.resolve()),
        raw_request=str(raw_request_path.resolve()),
        task_draft=str(task_draft_path.resolve()),
        codex_prompt=str(codex_prompt_path.resolve()),
        task_review=str(task_review_path.resolve()),
    )


def _write_task_draft_yaml(path: Path, draft: TaskDraft) -> None:
    yaml = _load_yaml_module()
    with path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(
            draft.model_dump(mode="python"),
            handle,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        )


def _write_derived_draft_artifacts(
    *,
    draft: TaskDraft,
    raw_request_text: str,
    task_draft_path: Path,
    codex_prompt_path: Path,
    task_review_path: Path,
) -> None:
    _write_task_draft_yaml(task_draft_path, draft)
    codex_prompt_path.write_text(render_codex_prompt_markdown(draft, raw_request_text), encoding="utf-8")
    task_review_path.write_text(render_task_review_markdown(draft), encoding="utf-8")


def _append_unique(values: list[str], additions: list[str]) -> list[str]:
    combined = list(values)
    for candidate in additions:
        if candidate not in combined:
            combined.append(candidate)
    return combined


def _remove_exact(values: list[str], removals: list[str]) -> list[str]:
    if not removals:
        return list(values)
    removal_set = {item for item in removals}
    return [item for item in values if item not in removal_set]


def _build_revision_summary(updated_fields: list[str]) -> str:
    if not updated_fields:
        return "No explicit fields changed."
    return "Updated fields: " + ", ".join(updated_fields)


def revise_task_draft(
    *,
    draft_id: str,
    drafts_dir: str | Path = ".task_drafts",
    title: str | None = None,
    objective: str | None = None,
    context: str | None = None,
    risk_level: str | None = None,
    task_id: str | None = None,
    commit_message: str | None = None,
    allow_files: list[str] | None = None,
    clear_files_allowed: bool = False,
    forbid_files: list[str] | None = None,
    remove_forbidden_files: list[str] | None = None,
    add_non_goals: list[str] | None = None,
    remove_non_goals: list[str] | None = None,
    add_invariants: list[str] | None = None,
    add_assumptions: list[str] | None = None,
    clear_assumptions: bool = False,
    add_open_questions: list[str] | None = None,
    resolve_open_questions: list[str] | None = None,
    clear_open_questions: bool = False,
    add_tests_required: list[str] | None = None,
    add_commands: list[str] | None = None,
    remove_commands: list[str] | None = None,
    add_acceptance_criteria: list[str] | None = None,
    add_validation_requirements: list[str] | None = None,
    add_rollback_notes: list[str] | None = None,
    require_profiles: list[str] | None = None,
    remove_required_profiles: list[str] | None = None,
    optional_profiles: list[str] | None = None,
    remove_optional_profiles: list[str] | None = None,
    prompt_language: str | None = None,
) -> TaskDraftRevisionResult:
    draft_dir = Path(drafts_dir) / draft_id
    if not draft_dir.exists():
        raise FileNotFoundError(f"task draft not found: {draft_id}")

    manifest_path = draft_dir / "MANIFEST.json"
    if not manifest_path.exists():
        raise FileNotFoundError("required draft artifact missing: MANIFEST.json")
    manifest = load_task_draft_manifest(manifest_path)

    raw_request_path = draft_dir / "raw_request.md"
    if not raw_request_path.exists():
        raise FileNotFoundError("required draft artifact missing: raw_request.md")
    task_draft_path = draft_dir / "task_draft.yaml"
    if not task_draft_path.exists():
        raise FileNotFoundError("required draft artifact missing: task_draft.yaml")

    codex_prompt_path = draft_dir / "codex_prompt.md"
    task_review_path = draft_dir / "task_review.md"
    raw_request_text = raw_request_path.read_text(encoding="utf-8")
    draft = load_task_draft(task_draft_path)
    payload = deepcopy(draft.model_dump(mode="python"))
    updated_fields: list[str] = []

    if title is not None:
        payload["title"] = title
        payload["target_task"]["title"] = title
        updated_fields.append("title")
    if objective is not None:
        payload["objective"] = objective
        updated_fields.append("objective")
    if context is not None:
        payload["context"] = context
        updated_fields.append("context")
    if risk_level is not None:
        payload["risk_level"] = risk_level
        updated_fields.append("risk_level")
    if task_id is not None:
        payload["target_task"]["id"] = task_id
        updated_fields.append("target_task.id")
    if commit_message is not None:
        payload["target_task"]["commit_message"] = commit_message
        updated_fields.append("target_task.commit_message")
    if prompt_language is not None:
        payload["prompt_language"] = prompt_language
        updated_fields.append("prompt_language")

    files_allowed = [] if clear_files_allowed else list(payload["files_allowed"])
    if clear_files_allowed or allow_files:
        files_allowed = _append_unique(files_allowed, allow_files or [])
        payload["files_allowed"] = files_allowed
        updated_fields.append("files_allowed")

    files_forbidden = list(payload["files_forbidden"])
    if remove_forbidden_files:
        files_forbidden = _remove_exact(files_forbidden, remove_forbidden_files)
    if forbid_files:
        files_forbidden = _append_unique(files_forbidden, forbid_files)
    if remove_forbidden_files or forbid_files:
        payload["files_forbidden"] = files_forbidden
        updated_fields.append("files_forbidden")

    non_goals = list(payload["non_goals"])
    if remove_non_goals:
        non_goals = _remove_exact(non_goals, remove_non_goals)
    if add_non_goals:
        non_goals = _append_unique(non_goals, add_non_goals)
    if remove_non_goals or add_non_goals:
        payload["non_goals"] = non_goals
        updated_fields.append("non_goals")

    if add_invariants:
        payload["invariants"] = _append_unique(list(payload["invariants"]), add_invariants)
        updated_fields.append("invariants")

    assumptions = [] if clear_assumptions else list(payload["assumptions"])
    if clear_assumptions or add_assumptions:
        assumptions = _append_unique(assumptions, add_assumptions or [])
        payload["assumptions"] = assumptions
        updated_fields.append("assumptions")

    open_questions = [] if clear_open_questions else list(payload["open_questions"])
    if resolve_open_questions:
        open_questions = _remove_exact(open_questions, resolve_open_questions)
    if add_open_questions:
        open_questions = _append_unique(open_questions, add_open_questions)
    if clear_open_questions or resolve_open_questions or add_open_questions:
        payload["open_questions"] = open_questions
        updated_fields.append("open_questions")

    if add_tests_required:
        payload["tests_required"] = _append_unique(list(payload["tests_required"]), add_tests_required)
        updated_fields.append("tests_required")

    commands_to_run = list(payload["commands_to_run"])
    if remove_commands:
        commands_to_run = _remove_exact(commands_to_run, remove_commands)
    if add_commands:
        commands_to_run = _append_unique(commands_to_run, add_commands)
    if remove_commands or add_commands:
        payload["commands_to_run"] = commands_to_run
        updated_fields.append("commands_to_run")

    if add_acceptance_criteria:
        payload["acceptance_criteria"] = _append_unique(list(payload["acceptance_criteria"]), add_acceptance_criteria)
        updated_fields.append("acceptance_criteria")

    if add_validation_requirements:
        payload["validation_requirements"] = _append_unique(
            list(payload["validation_requirements"]),
            add_validation_requirements,
        )
        updated_fields.append("validation_requirements")

    if add_rollback_notes:
        payload["rollback_notes"] = _append_unique(list(payload["rollback_notes"]), add_rollback_notes)
        updated_fields.append("rollback_notes")

    required_review_profiles = list(payload["required_review_profiles"])
    if remove_required_profiles:
        required_review_profiles = _remove_exact(required_review_profiles, remove_required_profiles)
    if require_profiles:
        required_review_profiles = _append_unique(required_review_profiles, require_profiles)
    if remove_required_profiles or require_profiles:
        payload["required_review_profiles"] = required_review_profiles
        updated_fields.append("required_review_profiles")

    optional_review_profiles = list(payload["optional_review_profiles"])
    if remove_optional_profiles:
        optional_review_profiles = _remove_exact(optional_review_profiles, remove_optional_profiles)
    if optional_profiles:
        optional_review_profiles = _append_unique(optional_review_profiles, optional_profiles)
    if remove_optional_profiles or optional_profiles:
        payload["optional_review_profiles"] = optional_review_profiles
        updated_fields.append("optional_review_profiles")

    revised_draft = TaskDraft.model_validate(payload)
    _write_derived_draft_artifacts(
        draft=revised_draft,
        raw_request_text=raw_request_text,
        task_draft_path=task_draft_path,
        codex_prompt_path=codex_prompt_path,
        task_review_path=task_review_path,
    )

    had_validation = any(
        value is not None
        for value in (
            manifest.validator_report,
            manifest.validator_report_md,
            manifest.validation_status,
            manifest.validated_at,
        )
    )
    new_validation_status = "stale" if had_validation else "not_validated"
    revision_summary = _build_revision_summary(updated_fields)
    updated_manifest = manifest.model_copy(
        update={
            "task_draft": str(task_draft_path.resolve()),
            "codex_prompt": str(codex_prompt_path.resolve()),
            "task_review": str(task_review_path.resolve()),
            "revised_at": datetime.now(),
            "revision_count": manifest.revision_count + 1,
            "last_revision_summary": revision_summary,
            "validation_status": new_validation_status,
            "valid_for_promotion": False,
            "validation_stale_reason": "task draft revised after last validation" if had_validation else None,
        }
    )
    save_task_draft_manifest(manifest_path, updated_manifest)

    return TaskDraftRevisionResult(
        draft=revised_draft,
        draft_dir=draft_dir.resolve(),
        task_draft_path=task_draft_path.resolve(),
        codex_prompt_path=codex_prompt_path.resolve(),
        task_review_path=task_review_path.resolve(),
        manifest_path=manifest_path.resolve(),
        revision_count=updated_manifest.revision_count,
        validation_status=new_validation_status,
        revision_summary=revision_summary,
    )


def create_task_draft_scaffold(
    *,
    request_path: str | Path,
    output_dir: str | Path = ".task_drafts",
    task_id: str | None = None,
    title: str | None = None,
    risk_level: str = "unknown",
    prompt_language: str = "ru",
) -> TaskDraftScaffoldResult:
    normalized_risk_level = risk_level.strip().lower()
    if normalized_risk_level not in _TASK_DRAFT_RISK_LEVELS:
        allowed = ", ".join(sorted(_TASK_DRAFT_RISK_LEVELS))
        raise ValueError(f"risk_level must be one of: {allowed}")

    request_source = Path(request_path)
    if not request_source.exists():
        raise FileNotFoundError(f"raw request file not found: {request_source}")
    raw_request_text = request_source.read_text(encoding="utf-8")
    if not raw_request_text.strip():
        raise ValueError("raw request file is empty")

    drafts_root = Path(output_dir)
    drafts_root.mkdir(parents=True, exist_ok=True)
    draft_id = _build_draft_id()
    draft_dir = drafts_root / draft_id
    while draft_dir.exists():
        draft_id = _build_draft_id()
        draft_dir = drafts_root / draft_id
    draft_dir.mkdir(parents=True, exist_ok=False)

    draft = _build_task_draft(
        draft_id=draft_id,
        raw_request_text=raw_request_text,
        title=title,
        task_id=task_id,
        risk_level=normalized_risk_level,
        prompt_language=prompt_language.strip() or "ru",
    )

    raw_request_copy_path = draft_dir / "raw_request.md"
    task_draft_path = draft_dir / "task_draft.yaml"
    codex_prompt_path = draft_dir / "codex_prompt.md"
    task_review_path = draft_dir / "task_review.md"
    manifest_path = draft_dir / "MANIFEST.json"

    raw_request_copy_path.write_text(raw_request_text, encoding="utf-8")

    _write_derived_draft_artifacts(
        draft=draft,
        raw_request_text=raw_request_text,
        task_draft_path=task_draft_path,
        codex_prompt_path=codex_prompt_path,
        task_review_path=task_review_path,
    )
    manifest = _manifest_payload(
        draft_id=draft.draft_id,
        request_source=request_source,
        draft_dir=draft_dir,
        raw_request_path=raw_request_copy_path,
        task_draft_path=task_draft_path,
        codex_prompt_path=codex_prompt_path,
        task_review_path=task_review_path,
    )
    manifest_path.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")

    return TaskDraftScaffoldResult(
        draft=draft,
        draft_dir=draft_dir.resolve(),
        raw_request_path=raw_request_copy_path.resolve(),
        task_draft_path=task_draft_path.resolve(),
        codex_prompt_path=codex_prompt_path.resolve(),
        task_review_path=task_review_path.resolve(),
        manifest_path=manifest_path.resolve(),
    )


def load_task_draft(path: str | Path) -> TaskDraft:
    yaml = _load_yaml_module()
    draft_path = Path(path)
    payload = yaml.safe_load(draft_path.read_text(encoding="utf-8"))
    return TaskDraft.model_validate(payload)


def load_task_draft_manifest(path: str | Path) -> TaskDraftManifest:
    manifest_path = Path(path)
    return TaskDraftManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))


def save_task_draft_manifest(path: str | Path, manifest: TaskDraftManifest) -> None:
    manifest_path = Path(path)
    manifest_path.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
