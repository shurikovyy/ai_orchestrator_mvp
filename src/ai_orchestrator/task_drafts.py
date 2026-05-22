from __future__ import annotations

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

    @field_validator("request_source", "draft_dir", "raw_request", "task_draft", "codex_prompt", "task_review")
    @classmethod
    def required_manifest_fields_not_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("manifest required field must not be empty")
        return value

    @field_validator("validator_report", "validator_report_md", "validation_status")
    @classmethod
    def optional_manifest_strings_blank_to_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None


@dataclass(frozen=True)
class TaskDraftScaffoldResult:
    draft: TaskDraft
    draft_dir: Path
    raw_request_path: Path
    task_draft_path: Path
    codex_prompt_path: Path
    task_review_path: Path
    manifest_path: Path


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
            "- [ ] tests_required and commands_to_run are sufficient.",
            "- [ ] acceptance_criteria are specific enough for validation.",
            "- [ ] target_task.enabled remains false until explicit promotion.",
            "",
            "## Notes",
            "",
            f"- Files allowed: `{allowed}`",
            f"- Files forbidden: `{forbidden}`",
            f"- Risk level: `{draft.risk_level}`",
            "- Open questions:",
            *([f"  - {item}" for item in draft.open_questions] if draft.open_questions else ["  - (none)"]),
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

    yaml = _load_yaml_module()
    with task_draft_path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(
            draft.model_dump(mode="python"),
            handle,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        )
    codex_prompt_path.write_text(render_codex_prompt_markdown(draft, raw_request_text), encoding="utf-8")
    task_review_path.write_text(render_task_review_markdown(draft), encoding="utf-8")
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
