from __future__ import annotations

from pathlib import PurePosixPath
import re


_WINDOWS_DRIVE_PATH_RE = re.compile(r"^[a-zA-Z]:[\\/]")


def normalize_safe_relative_path(value: str, *, field_name: str = "path") -> str:
    raw_value = value.strip()
    if not raw_value:
        raise ValueError(f"{field_name} must not be empty when provided")
    if _WINDOWS_DRIVE_PATH_RE.match(raw_value):
        raise ValueError(f"{field_name} must be a relative path, not a drive-qualified path")
    normalized = raw_value.replace("\\", "/")
    if normalized.startswith("/"):
        raise ValueError(f"{field_name} must be a relative path, not a rooted path")
    normalized_path = PurePosixPath(normalized)
    if normalized_path.is_absolute():
        raise ValueError(f"{field_name} must be a relative path")
    if ".." in normalized_path.parts:
        raise ValueError(f"{field_name} path must not escape the workspace")
    return "/".join(normalized_path.parts)
