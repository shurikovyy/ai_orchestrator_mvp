from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

from ai_orchestrator.task_drafts import TaskDraftManifest, load_task_draft, load_task_draft_manifest


@dataclass(frozen=True)
class TaskDraftInspectionSummary:
    draft_id: str
    draft_dir: Path
    paths: dict[str, Path]
    exists: dict[str, bool]
    title: str
    task_id: str
    risk_level: str
    target_enabled: bool | None
    validation_status: str
    valid_for_promotion: bool | None
    open_questions_count: int
    files_allowed_count: int
    required_review_profiles: list[str]
    optional_review_profiles: list[str]
    next_action: str


def _draft_artifact_paths(draft_dir: Path) -> dict[str, Path]:
    return {
        "raw_request": draft_dir / "raw_request.md",
        "task_draft": draft_dir / "task_draft.yaml",
        "codex_prompt": draft_dir / "codex_prompt.md",
        "task_review": draft_dir / "task_review.md",
        "manifest": draft_dir / "MANIFEST.json",
        "validator_report": draft_dir / "task_draft_validator_report.json",
        "validator_report_md": draft_dir / "task_draft_validator_report.md",
    }


def _load_validator_report_summary(path: Path) -> tuple[str, bool]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:  # noqa: BLE001 - surfaced as deterministic operator error.
        raise ValueError(f"task_draft_validator_report.json could not be parsed: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("task_draft_validator_report.json must contain a JSON object")
    validation_status = payload.get("validation_status")
    if not isinstance(validation_status, str) or not validation_status.strip():
        raise ValueError("task_draft_validator_report.json is missing validation_status")
    valid_for_promotion = payload.get("valid_for_promotion")
    if not isinstance(valid_for_promotion, bool):
        raise ValueError("task_draft_validator_report.json is missing boolean valid_for_promotion")
    return validation_status.strip(), valid_for_promotion


def _compute_task_draft_next_action(
    *,
    exists: dict[str, bool],
    manifest: TaskDraftManifest,
    validation_status: str,
    valid_for_promotion: bool | None,
) -> str:
    missing_review_artifacts = [
        name
        for name in ("raw_request", "codex_prompt", "task_review")
        if not exists[name]
    ]
    if missing_review_artifacts:
        return "inspect_missing_files"
    if not exists["validator_report"] or validation_status in {"missing", "stale", "unknown"}:
        return "validate_task_draft"
    if validation_status in {"invalid", "needs_revision"}:
        return "revise_task_draft"
    if validation_status == "valid" and valid_for_promotion is True:
        if manifest.promotion_status or manifest.promoted_task_id or manifest.promoted_at:
            return "inspect_promoted_task"
        return "promote_task_draft"
    return "revise_task_draft"


def build_task_draft_inspection_summary(
    *,
    draft_id: str,
    drafts_dir: str | Path = ".task_drafts",
) -> TaskDraftInspectionSummary:
    draft_dir = Path(drafts_dir) / draft_id
    if not draft_dir.exists():
        raise FileNotFoundError(f"task draft not found: {draft_id}")
    if not draft_dir.is_dir():
        raise ValueError(f"task draft path is not a directory: {draft_dir}")

    paths = _draft_artifact_paths(draft_dir)
    if not paths["task_draft"].exists():
        raise FileNotFoundError("required draft artifact missing: task_draft.yaml")
    if not paths["manifest"].exists():
        raise FileNotFoundError("required draft artifact missing: MANIFEST.json")

    try:
        manifest = load_task_draft_manifest(paths["manifest"])
    except Exception as exc:  # noqa: BLE001 - surfaced as deterministic operator error.
        raise ValueError(f"MANIFEST.json could not be parsed: {exc}") from exc
    try:
        draft = load_task_draft(paths["task_draft"])
    except Exception as exc:  # noqa: BLE001 - surfaced as deterministic operator error.
        raise ValueError(f"task_draft.yaml could not be parsed: {exc}") from exc

    exists = {name: path.exists() for name, path in paths.items()}
    validation_status = "missing"
    valid_for_promotion: bool | None = None
    if exists["validator_report"]:
        report_status, report_valid_for_promotion = _load_validator_report_summary(paths["validator_report"])
        validation_status = manifest.validation_status or report_status
        valid_for_promotion = (
            manifest.valid_for_promotion
            if manifest.valid_for_promotion is not None
            else report_valid_for_promotion
        )
        if manifest.validation_stale_reason or manifest.validation_status == "stale":
            validation_status = "stale"
            valid_for_promotion = False

    next_action = _compute_task_draft_next_action(
        exists=exists,
        manifest=manifest,
        validation_status=validation_status,
        valid_for_promotion=valid_for_promotion,
    )
    return TaskDraftInspectionSummary(
        draft_id=draft_id,
        draft_dir=draft_dir,
        paths=paths,
        exists=exists,
        title=draft.title,
        task_id=draft.target_task.id,
        risk_level=draft.risk_level,
        target_enabled=draft.target_task.enabled,
        validation_status=validation_status,
        valid_for_promotion=valid_for_promotion,
        open_questions_count=len(draft.open_questions),
        files_allowed_count=len(draft.files_allowed),
        required_review_profiles=list(draft.required_review_profiles),
        optional_review_profiles=list(draft.optional_review_profiles),
        next_action=next_action,
    )


def _display_path(path: Path, *, exists: bool, show_paths: bool) -> str:
    if not exists:
        return "missing"
    return str(path.resolve() if show_paths else path)


def _display_bool_or_unknown(value: bool | None) -> str:
    if value is None:
        return "unknown"
    return str(value).lower()


def format_task_draft_inspection_text(summary: TaskDraftInspectionSummary, *, show_paths: bool = False) -> str:
    lines = [
        f"draft_id={summary.draft_id}",
        f"draft_dir={summary.draft_dir.resolve() if show_paths else summary.draft_dir}",
    ]
    for name in (
        "raw_request",
        "task_draft",
        "codex_prompt",
        "task_review",
        "manifest",
        "validator_report",
        "validator_report_md",
    ):
        lines.append(
            f"{name}="
            + _display_path(
                summary.paths[name],
                exists=summary.exists[name],
                show_paths=show_paths,
            )
        )
    lines.extend(
        [
            f"title={summary.title}",
            f"task_id={summary.task_id}",
            f"risk_level={summary.risk_level}",
            f"target_enabled={_display_bool_or_unknown(summary.target_enabled)}",
            f"validation_status={summary.validation_status}",
            f"valid_for_promotion={_display_bool_or_unknown(summary.valid_for_promotion)}",
            f"open_questions={summary.open_questions_count}",
            f"files_allowed={summary.files_allowed_count}",
            "required_review_profiles=" + ",".join(summary.required_review_profiles),
            "optional_review_profiles=" + ",".join(summary.optional_review_profiles),
            f"next_action={summary.next_action}",
        ]
    )
    return "\n".join(lines)


def format_task_draft_inspection_json(summary: TaskDraftInspectionSummary, *, show_paths: bool = False) -> str:
    payload = {
        "draft_id": summary.draft_id,
        "draft_dir": str(summary.draft_dir.resolve() if show_paths else summary.draft_dir),
        "paths": {
            name: _display_path(path, exists=summary.exists[name], show_paths=show_paths)
            for name, path in summary.paths.items()
        },
        "target_task": {
            "id": summary.task_id,
            "title": summary.title,
            "enabled": summary.target_enabled,
        },
        "risk_level": summary.risk_level,
        "validation_status": summary.validation_status,
        "valid_for_promotion": summary.valid_for_promotion,
        "open_questions_count": summary.open_questions_count,
        "files_allowed_count": summary.files_allowed_count,
        "required_review_profiles": summary.required_review_profiles,
        "optional_review_profiles": summary.optional_review_profiles,
        "next_action": summary.next_action,
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)
