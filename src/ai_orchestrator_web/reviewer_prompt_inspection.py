"""Read-only inspection helpers for prepared reviewer prompt packets."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path, PurePath
import re
from typing import Any


PROMPT_SUFFIX = "_review_prompt.md"
SAFE_PROFILE_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")


@dataclass(frozen=True)
class ReviewerPromptIndexEntry:
    profile: str
    filename: str
    path: Path | None
    exists: bool
    source: str
    safe: bool = True


@dataclass(frozen=True)
class ReviewerPromptIndex:
    run_id: str
    run_dir: Path
    prompts_dir: Path
    manifest_path: Path
    manifest_exists: bool
    manifest_schema_version: str | None
    manifest_profiles: tuple[str, ...]
    entries: tuple[ReviewerPromptIndexEntry, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class ReviewerPromptDetail:
    run_id: str
    profile: str
    filename: str
    path: Path
    content: str
    source: str


class ReviewerPromptNotFound(FileNotFoundError):
    """Raised when a prepared reviewer prompt packet cannot be safely found."""


def build_reviewer_prompt_index(*, run_id: str, runs_dir: str | Path) -> ReviewerPromptIndex:
    run_dir = Path(runs_dir) / run_id
    prompts_dir = (run_dir / "reviewer_prompts").resolve()
    manifest_path = prompts_dir / "MANIFEST.json"
    warnings: list[str] = []
    entries: dict[str, ReviewerPromptIndexEntry] = {}

    manifest = _load_manifest(manifest_path, warnings=warnings)
    manifest_profiles = _manifest_profiles(manifest)
    if manifest is not None:
        for prompt in _manifest_prompts(manifest):
            profile = _safe_text(prompt.get("profile"))
            if not _is_safe_profile(profile):
                warnings.append(f"ignored unsafe manifest profile: {profile or '(empty)'}")
                continue
            path_value = _safe_text(prompt.get("path"))
            resolved_path = _resolve_manifest_prompt_path(path_value, prompts_dir)
            if resolved_path is None:
                warnings.append(f"ignored unsafe manifest path for profile: {profile}")
                entries[profile] = ReviewerPromptIndexEntry(
                    profile=profile,
                    filename="(unsafe manifest path)",
                    path=None,
                    exists=False,
                    source="manifest",
                    safe=False,
                )
                continue
            if not _is_prompt_packet_path(resolved_path):
                warnings.append(f"ignored non-prompt manifest path for profile: {profile}")
                entries[profile] = ReviewerPromptIndexEntry(
                    profile=profile,
                    filename=resolved_path.name,
                    path=None,
                    exists=False,
                    source="manifest",
                    safe=False,
                )
                continue
            entries[profile] = ReviewerPromptIndexEntry(
                profile=profile,
                filename=resolved_path.name,
                path=resolved_path,
                exists=resolved_path.is_file(),
                source="manifest",
            )

    if prompts_dir.is_dir():
        for prompt_path in sorted(prompts_dir.glob(f"*{PROMPT_SUFFIX}"), key=lambda path: path.name):
            profile = prompt_path.name[: -len(PROMPT_SUFFIX)]
            if not _is_safe_profile(profile) or profile in entries:
                continue
            resolved_path = prompt_path.resolve()
            if not _is_relative_to(resolved_path, prompts_dir):
                warnings.append(f"ignored fallback prompt outside reviewer_prompts: {prompt_path.name}")
                continue
            if not _is_prompt_packet_path(resolved_path):
                warnings.append(f"ignored fallback non-prompt file: {prompt_path.name}")
                continue
            entries[profile] = ReviewerPromptIndexEntry(
                profile=profile,
                filename=resolved_path.name,
                path=resolved_path,
                exists=resolved_path.is_file(),
                source="fallback",
            )

    return ReviewerPromptIndex(
        run_id=run_id,
        run_dir=run_dir.resolve(),
        prompts_dir=prompts_dir,
        manifest_path=manifest_path,
        manifest_exists=manifest_path.is_file(),
        manifest_schema_version=_safe_text(manifest.get("schema_version")) if manifest is not None else None,
        manifest_profiles=manifest_profiles,
        entries=tuple(entries[profile] for profile in sorted(entries)),
        warnings=tuple(warnings),
    )


def build_reviewer_prompt_detail(*, run_id: str, runs_dir: str | Path, profile: str) -> ReviewerPromptDetail:
    if not _is_safe_profile(profile):
        raise ReviewerPromptNotFound(f"reviewer prompt not found: {profile}")
    index = build_reviewer_prompt_index(run_id=run_id, runs_dir=runs_dir)
    entry_by_profile = {entry.profile: entry for entry in index.entries}
    entry = entry_by_profile.get(profile)
    if entry is None:
        entry = _fallback_entry(profile=profile, prompts_dir=index.prompts_dir)
    if entry is None or not entry.safe or entry.path is None or not entry.exists:
        raise ReviewerPromptNotFound(f"reviewer prompt not found: {profile}")

    prompt_path = entry.path.resolve()
    if not _is_relative_to(prompt_path, index.prompts_dir):
        raise ReviewerPromptNotFound(f"reviewer prompt not found: {profile}")
    if not _is_prompt_packet_path(prompt_path):
        raise ReviewerPromptNotFound(f"reviewer prompt not found: {profile}")
    return ReviewerPromptDetail(
        run_id=run_id,
        profile=profile,
        filename=prompt_path.name,
        path=prompt_path,
        content=prompt_path.read_text(encoding="utf-8-sig", errors="replace"),
        source=entry.source,
    )


def _fallback_entry(*, profile: str, prompts_dir: Path) -> ReviewerPromptIndexEntry | None:
    prompt_path = (prompts_dir / f"{profile}{PROMPT_SUFFIX}").resolve()
    if not _is_relative_to(prompt_path, prompts_dir):
        return None
    if not _is_prompt_packet_path(prompt_path):
        return None
    return ReviewerPromptIndexEntry(
        profile=profile,
        filename=prompt_path.name,
        path=prompt_path,
        exists=prompt_path.is_file(),
        source="fallback",
    )


def _load_manifest(path: Path, *, warnings: list[str]) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        warnings.append(f"MANIFEST.json could not be parsed: {exc}")
        return None
    if not isinstance(payload, dict):
        warnings.append("MANIFEST.json root must be an object")
        return None
    return payload


def _manifest_profiles(manifest: dict[str, Any] | None) -> tuple[str, ...]:
    if manifest is None:
        return tuple()
    profiles = manifest.get("profiles", [])
    if not isinstance(profiles, list):
        return tuple()
    return tuple(_safe_text(profile) for profile in profiles if _safe_text(profile))


def _manifest_prompts(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    prompts = manifest.get("prompts", [])
    if not isinstance(prompts, list):
        return []
    return [prompt for prompt in prompts if isinstance(prompt, dict)]


def _resolve_manifest_prompt_path(value: str, prompts_dir: Path) -> Path | None:
    if not value:
        return None
    path = Path(value)
    resolved_path = path.resolve() if path.is_absolute() else (prompts_dir / path).resolve()
    if not _is_relative_to(resolved_path, prompts_dir):
        return None
    return resolved_path


def _safe_text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _is_safe_profile(value: str) -> bool:
    if not value or value in {".", ".."}:
        return False
    if "/" in value or "\\" in value:
        return False
    if Path(value).is_absolute() or ".." in PurePath(value).parts:
        return False
    return bool(SAFE_PROFILE_PATTERN.fullmatch(value))


def _is_prompt_packet_path(path: Path) -> bool:
    return path.name.endswith(PROMPT_SUFFIX)


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True
