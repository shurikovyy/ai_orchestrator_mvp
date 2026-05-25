from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ai_orchestrator.task_drafts import (
    TaskDraft,
    _write_task_draft_yaml,
    load_task_draft,
    load_task_draft_manifest,
    render_codex_prompt_markdown,
    render_task_review_markdown,
    save_task_draft_manifest,
)
from ai_orchestrator.task_draft_validation import TaskDraftValidationReport

_REQUIRED_ARTIFACTS = (
    "raw_request.md",
    "task_draft.yaml",
    "codex_prompt.md",
    "task_review.md",
    "MANIFEST.json",
)


@dataclass(frozen=True)
class TaskDraftImprovementPromptResult:
    draft_id: str
    prompt_path: Path
    manifest_path: Path


@dataclass(frozen=True)
class TaskDraftImprovementImportResult:
    draft_id: str
    task_draft_path: Path
    backup_path: Path
    codex_prompt_path: Path
    task_review_path: Path
    manifest_path: Path


def _require_artifacts(draft_dir: Path) -> dict[str, Path]:
    paths = {name: draft_dir / name for name in _REQUIRED_ARTIFACTS}
    for name, path in paths.items():
        if not path.exists():
            raise FileNotFoundError(f"required draft artifact missing: {name}")
        if not path.is_file():
            raise ValueError(f"required draft artifact is not a file: {name}")
    return paths


def _load_validation_report(path: Path) -> TaskDraftValidationReport | None:
    if not path.exists():
        return None
    return TaskDraftValidationReport.model_validate_json(path.read_text(encoding="utf-8"))


def _backup_path_for_task_draft(task_draft_path: Path) -> Path:
    default_backup = task_draft_path.with_name("task_draft.before_improvement.yaml")
    if not default_backup.exists():
        return default_backup
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    return task_draft_path.with_name(f"task_draft.before_improvement.{timestamp}.yaml")


def _ensure_improved_draft_is_safe(*, draft_id: str, current: TaskDraft, improved: TaskDraft) -> None:
    if improved.draft_id != draft_id:
        raise ValueError(f"improved draft_id does not match selected draft: expected {draft_id}, got {improved.draft_id}")
    if current.draft_id != draft_id:
        raise ValueError(f"current task_draft.yaml draft_id does not match selected draft: {current.draft_id}")
    if not improved.target_task.id.strip():
        raise ValueError("improved target_task.id must not be empty")
    if improved.target_task.enabled:
        raise ValueError("improved target_task.enabled must be false")

    required_lists = (
        ("non_goals", improved.non_goals),
        ("files_forbidden", improved.files_forbidden),
        ("invariants", improved.invariants),
        ("tests_required", improved.tests_required),
        ("acceptance_criteria", improved.acceptance_criteria),
    )
    for field_name, values in required_lists:
        if not values:
            raise ValueError(f"improved draft must not remove all {field_name}")


def _load_improved_task_draft(path: str | Path) -> TaskDraft:
    improved_path = Path(path)
    if not improved_path.exists():
        raise FileNotFoundError(f"improved draft file not found: {improved_path}")
    if not improved_path.is_file():
        raise ValueError(f"improved draft path is not a file: {improved_path}")
    return load_task_draft(improved_path)


def _load_notes(path: str | Path | None) -> tuple[str | None, Path | None]:
    if path is None:
        return None, None
    notes_path = Path(path)
    if not notes_path.exists():
        raise FileNotFoundError(f"improvement notes file not found: {notes_path}")
    if not notes_path.is_file():
        raise ValueError(f"improvement notes path is not a file: {notes_path}")
    text = notes_path.read_text(encoding="utf-8-sig")
    if not text.strip():
        raise ValueError("improvement notes file is empty")
    return text, notes_path


def _format_validator_report_section(report: TaskDraftValidationReport | None) -> list[str]:
    if report is None:
        return [
            "Validator report отсутствует.",
            "",
            "Сначала улучшите draft консервативно, затем пользователь должен запустить `validate-task-draft`.",
        ]

    lines = [
        f"- validation_status: `{report.validation_status}`",
        f"- valid_for_promotion: `{str(report.valid_for_promotion).lower()}`",
        f"- total findings: `{report.counts.total}`",
        f"- errors: `{report.counts.errors}`",
        f"- warnings: `{report.counts.warnings}`",
        f"- info: `{report.counts.info}`",
        f"- blocking: `{report.counts.blocking}`",
        "",
        "Сначала исправь validator findings. Не игнорируй warnings, потому что warnings блокируют promotion.",
    ]
    if not report.findings:
        lines.extend(["", "- (findings отсутствуют)"])
        return lines

    lines.extend(["", "Findings:"])
    for finding in report.findings:
        lines.extend(
            [
                f"- id: `{finding.id}`",
                f"  severity: `{finding.severity}`",
                f"  category: `{finding.category}`",
                f"  field: `{finding.field or ''}`",
                f"  message: {finding.message}",
                f"  required_action: {finding.required_action or ''}",
            ]
        )
    return lines


def _fenced_block(language: str, content: str) -> list[str]:
    return [f"```{language}", content.rstrip(), "```"]


def build_task_draft_improvement_prompt(
    *,
    draft: TaskDraft,
    raw_request_text: str,
    task_draft_yaml: str,
    codex_prompt_text: str,
    task_review_text: str,
    validation_report: TaskDraftValidationReport | None,
) -> str:
    lines: list[str] = [
        "# Prompt для улучшения task draft",
        "",
        "## Роль",
        "",
        "Ты task-authoring agent. Твоя задача — улучшить task draft, но НЕ запускать задачу.",
        "",
        "Работай с текущим draft как с безопасным промежуточным артефактом. Твоя цель — предложить более точный, проверяемый и пригодный для последующей deterministic validation draft.",
        "",
        "## Запреты",
        "",
        "- Не запускать Codex executor.",
        "- Не запускать pipeline.",
        "- Не менять tasks.yaml.",
        "- Не создавать .runs.",
        "- Не делать apply/accept/commit.",
        "- Не расширять scope без явной причины.",
        "- Не удалять non_goals/files_forbidden.",
        "- Не ослаблять validation/review/apply gates.",
        "- Не заявлять, что draft готов к promotion без повторного `validate-task-draft`.",
        "",
        "## Входные данные",
        "",
        "### Raw request",
        "",
        *_fenced_block("md", raw_request_text),
        "",
        "### Current task_draft.yaml",
        "",
        *_fenced_block("yaml", task_draft_yaml),
        "",
        "### Current codex_prompt.md",
        "",
        *_fenced_block("md", codex_prompt_text),
        "",
        "### Current task_review.md",
        "",
        *_fenced_block("md", task_review_text),
        "",
        "### Validator report",
        "",
        *_format_validator_report_section(validation_report),
        "",
        "## Что нужно улучшить",
        "",
        "Предложи revised task draft целиком, уделив внимание:",
        "",
        "- objective",
        "- non_goals",
        "- files_allowed",
        "- files_forbidden",
        "- invariants",
        "- assumptions",
        "- open_questions",
        "- risk_level",
        "- required_review_profiles",
        "- optional_review_profiles",
        "- tests_required",
        "- commands_to_run",
        "- acceptance_criteria",
        "- validation_requirements",
        "- rollback_notes",
        "- target_task metadata",
        "",
        "Сохраняй `draft_id` и не включай изменения, которые требуют запуска pipeline, Codex executor, apply/accept или commit.",
        f"Текущий draft_id: `{draft.draft_id}`.",
        "",
        "## Output contract",
        "",
        "Верни только два раздела:",
        "",
        "1. `task_draft.yaml` целиком в YAML block.",
        "2. `TASK_DRAFT_IMPROVEMENT_NOTES.md` в markdown block.",
        "",
        "`TASK_DRAFT_IMPROVEMENT_NOTES.md` должен содержать:",
        "",
        "- что изменено",
        "- какие assumptions сделаны",
        "- какие open_questions остались",
        "- почему scope не расширен",
        "- какие risks/reviewer profiles выбраны",
        "",
        "Важно:",
        "",
        "- Не возвращать patch.",
        "- Не возвращать только diff.",
        "- Не запускать команды.",
        "- Не заявлять, что task готов к promotion без `validate-task-draft`.",
        "- После улучшения пользователь должен сохранить revised draft и снова запустить `validate-task-draft`.",
        "",
        "Формат ответа:",
        "",
        "```yaml",
        "# task_draft.yaml",
        "# верни полный YAML документа, не diff",
        "```",
        "",
        "```md",
        "# TASK_DRAFT_IMPROVEMENT_NOTES.md",
        "# опиши изменения и оставшиеся риски",
        "```",
    ]
    return "\n".join(lines).rstrip() + "\n"


def prepare_task_draft_improvement_prompt(
    *,
    draft_id: str,
    drafts_dir: str | Path = ".task_drafts",
    output: str | Path | None = None,
    force: bool = False,
) -> TaskDraftImprovementPromptResult:
    draft_dir = Path(drafts_dir) / draft_id
    if not draft_dir.exists():
        raise FileNotFoundError(f"task draft not found: {draft_id}")
    if not draft_dir.is_dir():
        raise ValueError(f"task draft path is not a directory: {draft_id}")

    paths = _require_artifacts(draft_dir)
    manifest_path = paths["MANIFEST.json"]
    manifest = load_task_draft_manifest(manifest_path)
    draft = load_task_draft(paths["task_draft.yaml"])

    output_path = Path(output) if output is not None else draft_dir / "TASK_DRAFT_IMPROVEMENT_PROMPT.md"
    if output_path.exists() and not force:
        raise FileExistsError(f"task draft improvement prompt already exists: {output_path}")

    raw_request_text = paths["raw_request.md"].read_text(encoding="utf-8")
    task_draft_yaml = paths["task_draft.yaml"].read_text(encoding="utf-8")
    codex_prompt_text = paths["codex_prompt.md"].read_text(encoding="utf-8")
    task_review_text = paths["task_review.md"].read_text(encoding="utf-8")
    validation_report = _load_validation_report(draft_dir / "task_draft_validator_report.json")

    prompt = build_task_draft_improvement_prompt(
        draft=draft,
        raw_request_text=raw_request_text,
        task_draft_yaml=task_draft_yaml,
        codex_prompt_text=codex_prompt_text,
        task_review_text=task_review_text,
        validation_report=validation_report,
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(prompt, encoding="utf-8")

    updated_manifest = manifest.model_copy(
        update={
            "task_draft_improvement_prompt": str(output_path.resolve()),
            "task_draft_improvement_prompt_created_at": datetime.now(),
            "improvement_prompt_status": "prepared",
        }
    )
    save_task_draft_manifest(manifest_path, updated_manifest)

    return TaskDraftImprovementPromptResult(
        draft_id=draft.draft_id,
        prompt_path=output_path.resolve(),
        manifest_path=manifest_path.resolve(),
    )


def import_task_draft_improvement(
    *,
    draft_id: str,
    drafts_dir: str | Path = ".task_drafts",
    improved_draft: str | Path,
    notes: str | Path | None = None,
    force: bool = False,
) -> TaskDraftImprovementImportResult:
    draft_dir = Path(drafts_dir) / draft_id
    if not draft_dir.exists():
        raise FileNotFoundError(f"task draft not found: {draft_id}")
    if not draft_dir.is_dir():
        raise ValueError(f"task draft path is not a directory: {draft_id}")

    paths = _require_artifacts(draft_dir)
    raw_request_path = paths["raw_request.md"]
    task_draft_path = paths["task_draft.yaml"]
    codex_prompt_path = paths["codex_prompt.md"]
    task_review_path = paths["task_review.md"]
    manifest_path = paths["MANIFEST.json"]

    manifest = load_task_draft_manifest(manifest_path)
    current_draft = load_task_draft(task_draft_path)
    improved_draft_path = Path(improved_draft)
    improved = _load_improved_task_draft(improved_draft_path)
    notes_text, notes_source_path = _load_notes(notes)

    notes_destination = draft_dir / "TASK_DRAFT_IMPROVEMENT_NOTES.md"
    if notes_text is not None and notes_destination.exists() and not force:
        raise FileExistsError(f"task draft improvement notes already exist: {notes_destination}")

    _ensure_improved_draft_is_safe(draft_id=draft_id, current=current_draft, improved=improved)

    raw_request_text = raw_request_path.read_text(encoding="utf-8")
    new_codex_prompt = render_codex_prompt_markdown(improved, raw_request_text)
    new_task_review = render_task_review_markdown(improved)
    original_task_draft = task_draft_path.read_text(encoding="utf-8")
    original_codex_prompt = codex_prompt_path.read_text(encoding="utf-8")
    original_task_review = task_review_path.read_text(encoding="utf-8")
    original_manifest = manifest_path.read_text(encoding="utf-8")
    original_notes = notes_destination.read_text(encoding="utf-8") if notes_destination.exists() else None
    backup_path = _backup_path_for_task_draft(task_draft_path)

    try:
        backup_path.write_text(original_task_draft, encoding="utf-8")
        _write_task_draft_yaml(task_draft_path, improved)
        codex_prompt_path.write_text(new_codex_prompt, encoding="utf-8")
        task_review_path.write_text(new_task_review, encoding="utf-8")
        if notes_text is not None:
            notes_destination.write_text(notes_text, encoding="utf-8")

        updated_manifest = manifest.model_copy(
            update={
                "task_draft": str(task_draft_path.resolve()),
                "codex_prompt": str(codex_prompt_path.resolve()),
                "task_review": str(task_review_path.resolve()),
                "imported_improvement_at": datetime.now(),
                "imported_improvement_source": str(improved_draft_path.resolve()),
                "improvement_notes": str(notes_destination.resolve()) if notes_text is not None else manifest.improvement_notes,
                "revision_count": manifest.revision_count + 1,
                "last_revision_summary": "Imported task draft from external improvement.",
                "validation_status": "stale",
                "valid_for_promotion": False,
                "validation_stale_reason": "task draft imported from external improvement",
            }
        )
        save_task_draft_manifest(manifest_path, updated_manifest)
    except Exception:
        task_draft_path.write_text(original_task_draft, encoding="utf-8")
        codex_prompt_path.write_text(original_codex_prompt, encoding="utf-8")
        task_review_path.write_text(original_task_review, encoding="utf-8")
        manifest_path.write_text(original_manifest, encoding="utf-8")
        if notes_text is not None:
            if original_notes is None:
                notes_destination.unlink(missing_ok=True)
            else:
                notes_destination.write_text(original_notes, encoding="utf-8")
        raise

    return TaskDraftImprovementImportResult(
        draft_id=improved.draft_id,
        task_draft_path=task_draft_path.resolve(),
        backup_path=backup_path.resolve(),
        codex_prompt_path=codex_prompt_path.resolve(),
        task_review_path=task_review_path.resolve(),
        manifest_path=manifest_path.resolve(),
    )
