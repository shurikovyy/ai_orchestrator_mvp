from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ai_orchestrator.task_drafts import (
    TaskDraft,
    TaskDraftManifest,
    _load_yaml_module,
    load_task_draft,
    load_task_draft_manifest,
    save_task_draft_manifest,
)
from ai_orchestrator.task_draft_validation import TaskDraftValidationReport
from ai_orchestrator.task_queue import get_task_definition, load_task_queue_config

_DEFAULT_TASKS_FILE_PAYLOAD = {
    "project": "ai_orchestrator_mvp",
    "defaults": {
        "backend": "mock",
        "max_retries": 2,
        "require_structured_report": False,
        "rerun_report_test_commands": False,
        "validate_workspace_manifest": False,
        "validation_command_timeout": 60,
        "stream_codex_output": False,
        "verbose": True,
    },
    "tasks": [],
}


@dataclass(frozen=True)
class TaskDraftPromotionResult:
    draft_id: str
    task_id: str
    tasks_file_path: Path
    enabled: bool
    mode: str
    manifest_path: Path


def _load_validation_report(path: Path) -> TaskDraftValidationReport:
    return TaskDraftValidationReport.model_validate_json(path.read_text(encoding="utf-8"))


def _ensure_promotable(
    *,
    manifest: TaskDraftManifest,
    report: TaskDraftValidationReport,
    report_path: Path,
) -> None:
    if report.validation_status != "valid":
        raise ValueError(f"task draft validation_status is not valid: {report.validation_status}")
    if not report.valid_for_promotion:
        raise ValueError("task draft validator report does not allow promotion")
    if manifest.validation_status != "valid":
        raise ValueError(f"task draft manifest validation_status is not valid: {manifest.validation_status or 'missing'}")
    if manifest.valid_for_promotion is not True:
        raise ValueError("task draft manifest does not allow promotion")
    if manifest.validation_stale_reason:
        raise ValueError("task draft validation is stale; rerun validate-task-draft")
    if manifest.validator_report is None:
        raise ValueError("task draft manifest is missing validator_report")
    if Path(manifest.validator_report).resolve() != report_path.resolve():
        raise ValueError("task draft manifest validator_report does not match the current validator report path")
    if manifest.revised_at is not None and (manifest.validated_at is None or manifest.revised_at > manifest.validated_at):
        raise ValueError("task draft validation is stale; rerun validate-task-draft")


def render_promoted_task_prompt(draft: TaskDraft) -> str:
    lines = [
        draft.objective,
        "",
    ]
    if draft.context:
        lines.extend(
            [
                "Context:",
                draft.context,
                "",
            ]
        )

    lines.extend(
        [
            "Non-goals:",
            *[f"- {item}" for item in draft.non_goals],
            "",
            "Files allowed:",
            *([f"- {item}" for item in draft.files_allowed] if draft.files_allowed else ["- (not explicitly constrained; keep the reviewed scope narrow)"]),
            "",
            "Files forbidden:",
            *[f"- {item}" for item in draft.files_forbidden],
            "",
            "Invariants:",
            *[f"- {item}" for item in draft.invariants],
            "",
            "Assumptions:",
            *([f"- {item}" for item in draft.assumptions] if draft.assumptions else ["- (none)"]),
            "",
            "Required reviewer profiles:",
            *([f"- {item}" for item in draft.required_review_profiles] if draft.required_review_profiles else ["- (none)"]),
            "",
            "Optional reviewer profiles:",
            *([f"- {item}" for item in draft.optional_review_profiles] if draft.optional_review_profiles else ["- (none)"]),
            "",
            "Tests required:",
            *[f"- {item}" for item in draft.tests_required],
            "",
            "Commands to run:",
            *[f"- {item}" for item in draft.commands_to_run],
            "",
            "Acceptance criteria:",
            *[f"- {item}" for item in draft.acceptance_criteria],
            "",
            "Validation requirements:",
            *[f"- {item}" for item in draft.validation_requirements],
            "",
            "Rollback notes:",
            *[f"- {item}" for item in draft.rollback_notes],
            "",
            "Execution rules:",
            "- Create EXECUTION_REPORT.json using the required structured schema.",
            "- Do not create EXECUTION_REPORT.md.",
            "- Do not commit changes.",
            "- Do not modify files_forbidden.",
        ]
    )
    if draft.files_allowed:
        lines.append("- Keep changed_files within files_allowed.")
    lines.extend(
        [
            "- Do not weaken validation, review, apply, or safety gates.",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def _task_entry_from_draft(draft: TaskDraft, *, enabled: bool) -> dict[str, object]:
    target = draft.target_task
    task_entry: dict[str, object] = {
        "id": target.id,
        "title": target.title,
        "enabled": bool(enabled),
        "prompt": render_promoted_task_prompt(draft),
        "criteria": list(draft.acceptance_criteria),
        "backend": target.backend,
        "seed_workspace": target.seed_workspace,
        "require_structured_report": target.require_structured_report,
        "rerun_report_test_commands": target.rerun_report_test_commands,
        "validate_workspace_manifest": target.validate_workspace_manifest,
        "validation_command_timeout": target.validation_command_timeout,
        "stream_codex_output": target.stream_codex_output,
        "verbose": target.verbose,
    }
    if target.commit_message is not None:
        task_entry["commit_message"] = target.commit_message
    return task_entry


def _load_existing_tasks_payload(tasks_path: Path) -> dict[str, object]:
    yaml = _load_yaml_module()
    payload = yaml.safe_load(tasks_path.read_text(encoding="utf-8"))
    if payload is None:
        raise ValueError(f"tasks file is empty: {tasks_path}")
    if not isinstance(payload, dict):
        raise ValueError("tasks.yaml must contain a YAML mapping at the top level")
    return payload


def _write_tasks_payload(tasks_path: Path, payload: dict[str, object]) -> None:
    yaml = _load_yaml_module()
    with tasks_path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(
            payload,
            handle,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        )


def promote_task_draft(
    *,
    draft_id: str,
    drafts_dir: str | Path = ".task_drafts",
    tasks_file: str | Path = "tasks.yaml",
    enable: bool = False,
    replace: bool = False,
) -> TaskDraftPromotionResult:
    draft_dir = Path(drafts_dir) / draft_id
    if not draft_dir.exists():
        raise FileNotFoundError(f"task draft not found: {draft_id}")

    task_draft_path = draft_dir / "task_draft.yaml"
    manifest_path = draft_dir / "MANIFEST.json"
    report_path = draft_dir / "task_draft_validator_report.json"
    if not task_draft_path.exists():
        raise FileNotFoundError("required draft artifact missing: task_draft.yaml")
    if not manifest_path.exists():
        raise FileNotFoundError("required draft artifact missing: MANIFEST.json")
    if not report_path.exists():
        raise FileNotFoundError("required draft artifact missing: task_draft_validator_report.json")

    manifest = load_task_draft_manifest(manifest_path)
    report = _load_validation_report(report_path)
    _ensure_promotable(manifest=manifest, report=report, report_path=report_path)
    draft = load_task_draft(task_draft_path)

    tasks_path = Path(tasks_file)
    existing_text: str | None = None
    created_file = False
    if tasks_path.exists():
        existing_text = tasks_path.read_text(encoding="utf-8")
        load_task_queue_config(tasks_path)
        payload = _load_existing_tasks_payload(tasks_path)
        mode = "append"
    else:
        tasks_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "project": _DEFAULT_TASKS_FILE_PAYLOAD["project"],
            "defaults": dict(_DEFAULT_TASKS_FILE_PAYLOAD["defaults"]),
            "tasks": [],
        }
        mode = "append"
        created_file = True

    tasks_list = payload.get("tasks")
    if tasks_list is None:
        tasks_list = []
        payload["tasks"] = tasks_list
    if not isinstance(tasks_list, list):
        raise ValueError("tasks.yaml field `tasks` must be a list")

    promoted_task = _task_entry_from_draft(draft, enabled=enable)
    existing_index = next((index for index, task in enumerate(tasks_list) if isinstance(task, dict) and task.get("id") == draft.target_task.id), None)
    if existing_index is not None:
        if not replace:
            raise ValueError(f"task id already exists in tasks.yaml: {draft.target_task.id}; pass --replace to overwrite it")
        tasks_list[existing_index] = promoted_task
        mode = "replace"
    else:
        tasks_list.append(promoted_task)

    try:
        _write_tasks_payload(tasks_path, payload)
        config = load_task_queue_config(tasks_path)
        resolved_task = get_task_definition(config, draft.target_task.id)
        if resolved_task.enabled is not bool(enable):
            raise ValueError("promoted task enabled state does not match requested --enable setting")
        if resolved_task.backend != draft.target_task.backend:
            raise ValueError("promoted task backend does not match task draft")
        expected_seed_workspace = draft.target_task.seed_workspace
        if (resolved_task.seed_workspace or "") != expected_seed_workspace:
            raise ValueError("promoted task seed_workspace does not match task draft")
        if resolved_task.criteria != draft.acceptance_criteria:
            raise ValueError("promoted task criteria do not match task draft acceptance_criteria")
    except Exception:
        if created_file:
            if tasks_path.exists():
                tasks_path.unlink()
        elif existing_text is not None:
            tasks_path.write_text(existing_text, encoding="utf-8")
        raise

    updated_manifest = manifest.model_copy(
        update={
            "promoted_at": datetime.now(),
            "promoted_tasks_file": str(tasks_path.resolve()),
            "promoted_task_id": draft.target_task.id,
            "promoted_enabled": bool(enable),
            "promotion_status": "promoted",
        }
    )
    save_task_draft_manifest(manifest_path, updated_manifest)

    return TaskDraftPromotionResult(
        draft_id=draft.draft_id,
        task_id=draft.target_task.id,
        tasks_file_path=tasks_path.resolve(),
        enabled=bool(enable),
        mode=mode,
        manifest_path=manifest_path.resolve(),
    )
