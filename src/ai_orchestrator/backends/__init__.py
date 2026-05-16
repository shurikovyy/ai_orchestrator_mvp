from ai_orchestrator.backends.base import Backend
from ai_orchestrator.backends.codex_cli import CodexCliBackend
from ai_orchestrator.backends.mock import MockBackend


def get_backend(name: str) -> Backend:
    normalized = name.strip().lower()
    if normalized == "mock":
        return MockBackend()
    if normalized in {"codex", "codex_cli"}:
        return CodexCliBackend()
    raise ValueError(f"Unknown backend: {name}. Supported backends: mock, codex_cli")


__all__ = ["Backend", "MockBackend", "CodexCliBackend", "get_backend"]
