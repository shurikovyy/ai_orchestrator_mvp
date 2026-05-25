from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ai_orchestrator.task_drafts import (
    TaskDraft,
    load_task_draft,
    load_task_draft_manifest,
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
