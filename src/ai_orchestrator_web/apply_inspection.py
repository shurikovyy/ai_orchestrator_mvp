"""Read-only inspection helpers for apply report artifacts."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path


@dataclass(frozen=True)
class ApplyReportSummary:
    run_id: str
    run_dir: Path
    json_path: Path
    markdown_path: Path
    json_exists: bool
    markdown_exists: bool
    load_error: str | None
    schema_version: str | None
    status: str | None
    applied_at: str | None
    target_workspace: str | None
    review_gate: str | None
    applied_files: tuple[str, ...]
    deleted_files: tuple[str, ...]
    skipped_files: tuple[str, ...]
    commit_created: bool | None
    git_add_performed: bool | None
    next_step: str | None


def build_apply_report_summary(*, run_id: str, runs_dir: str | Path) -> ApplyReportSummary:
    run_dir = (Path(runs_dir) / run_id).resolve()
    json_path = run_dir / "APPLY_REPORT.json"
    markdown_path = run_dir / "APPLY_REPORT.md"
    payload: dict[str, object] | None = None
    load_error: str | None = None

    if json_path.is_file():
        try:
            loaded = json.loads(json_path.read_text(encoding="utf-8-sig"))
            if not isinstance(loaded, dict):
                raise ValueError("APPLY_REPORT.json must contain a JSON object")
            payload = loaded
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            load_error = f"APPLY_REPORT.json could not be loaded: {exc}"

    return ApplyReportSummary(
        run_id=run_id,
        run_dir=run_dir,
        json_path=json_path,
        markdown_path=markdown_path,
        json_exists=json_path.is_file(),
        markdown_exists=markdown_path.is_file(),
        load_error=load_error,
        schema_version=_optional_str(payload, "schema_version"),
        status=_optional_str(payload, "status"),
        applied_at=_optional_str(payload, "applied_at"),
        target_workspace=_optional_str(payload, "target_workspace"),
        review_gate=_optional_str(payload, "review_gate"),
        applied_files=_string_tuple(payload, "applied_files"),
        deleted_files=_string_tuple(payload, "deleted_files"),
        skipped_files=_string_tuple(payload, "skipped_files"),
        commit_created=_optional_bool(payload, "commit_created"),
        git_add_performed=_optional_bool(payload, "git_add_performed"),
        next_step=_optional_str(payload, "next_step"),
    )


def _optional_str(payload: dict[str, object] | None, key: str) -> str | None:
    if payload is None or payload.get(key) is None:
        return None
    return str(payload[key])


def _optional_bool(payload: dict[str, object] | None, key: str) -> bool | None:
    if payload is None or payload.get(key) is None:
        return None
    value = payload[key]
    if isinstance(value, bool):
        return value
    return None


def _string_tuple(payload: dict[str, object] | None, key: str) -> tuple[str, ...]:
    if payload is None:
        return tuple()
    value = payload.get(key)
    if not isinstance(value, list):
        return tuple()
    return tuple(str(item) for item in value)
