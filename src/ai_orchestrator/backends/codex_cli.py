from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

from ai_orchestrator.backends.mock import MockBackend
from ai_orchestrator.schemas import ExecutionResult, PlanStep, TaskSpec


class CodexCliBackend(MockBackend):
    """Codex CLI executor adapter.

    Planner and validator stay deterministic in this MVP. Execution is delegated
    to ``codex exec`` or to a compatible command such as ``npx @openai/codex exec``.
    """

    name = "codex_cli"
    _workspace_text_extensions = {
        ".txt",
        ".md",
        ".py",
        ".json",
        ".yaml",
        ".yml",
        ".toml",
        ".sql",
        ".csv",
    }

    def __init__(
        self,
        *,
        codex_cmd: str | Sequence[str] | None = None,
        timeout_seconds: int = 1800,
    ) -> None:
        # Priority:
        # 1. Explicit CLI argument passed by ai_orchestrator.cli
        # 2. AI_ORCHESTRATOR_CODEX_CMD env var
        # 3. CODEX_CMD env var
        # 4. Plain "codex" from PATH
        raw_cmd = codex_cmd or os.environ.get("AI_ORCHESTRATOR_CODEX_CMD") or os.environ.get("CODEX_CMD") or "codex"
        self.codex_cmd = self._normalize_command(raw_cmd)
        self.timeout_seconds = timeout_seconds

    @staticmethod
    def _strip_matching_quotes(value: str) -> str:
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            return value[1:-1]
        return value

    @staticmethod
    def _looks_like_bare_windows_path(command: str) -> bool:
        r"""Return True for a single Windows path such as C:\Tools\codex.cmd.

        ``shlex.split(..., posix=True)`` treats backslashes as escapes. That is
        correct for Unix shell snippets but destructive for bare Windows paths,
        and it breaks tests/CI that exercise Windows paths from a non-Windows
        host.
        """

        stripped = CodexCliBackend._strip_matching_quotes(command.strip())
        return bool(re.match(r"^[A-Za-z]:[\\/]", stripped)) and not re.search(r"\s--?", stripped)

    @staticmethod
    def _normalize_command(command: str | Sequence[str]) -> list[str]:
        if isinstance(command, str):
            command = command.strip()
            if CodexCliBackend._looks_like_bare_windows_path(command):
                parts = [CodexCliBackend._strip_matching_quotes(command)]
            else:
                parts = shlex.split(command, posix=os.name != "nt")
                if os.name == "nt":
                    parts = [CodexCliBackend._strip_matching_quotes(part) for part in parts]
        else:
            parts = [str(item) for item in command]
        if not parts:
            raise ValueError("Codex command must not be empty")
        return parts

    @staticmethod
    def _command_exists(command: str) -> bool:
        # Absolute/relative paths should be accepted even when they are not on PATH.
        candidate = Path(command)
        if candidate.parent != Path(".") and candidate.exists():
            return True
        return shutil.which(command) is not None

    @classmethod
    def _collect_workspace_text_files(cls, workspace_dir: Path) -> list[tuple[Path, str]]:
        collected: list[tuple[Path, str]] = []
        for path in sorted(workspace_dir.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in cls._workspace_text_extensions:
                continue
            collected.append((path, path.read_text(encoding="utf-8", errors="replace")))
        return collected

    def execute_step(
        self,
        *,
        task: TaskSpec,
        step: PlanStep,
        attempt: int,
        previous_feedback: list[str],
        artifacts_dir: Path,
    ) -> ExecutionResult:
        command_display = " ".join(shlex.quote(part) for part in self.codex_cmd)
        if not self._command_exists(self.codex_cmd[0]):
            return ExecutionResult(
                step_id=step.id,
                attempt=attempt,
                status="failed",
                content=(
                    f"Codex CLI command `{command_display}` could not be executed: "
                    f"binary `{self.codex_cmd[0]}` was not found."
                ),
                artifact_paths=[],
                notes=[
                    "Install Codex CLI, use a project-local npm install, or pass --codex-cmd / set AI_ORCHESTRATOR_CODEX_CMD.",
                    "Example: --codex-cmd './node_modules/.bin/codex'",
                    "Example: --codex-cmd 'npx --yes @openai/codex'",
                ],
            )

        artifacts_dir.mkdir(parents=True, exist_ok=True)
        workspace_dir = (artifacts_dir / "workspace").resolve()
        workspace_dir.mkdir(parents=True, exist_ok=True)
        output_path = (artifacts_dir / f"{step.id}_attempt_{attempt}_codex_final.md").resolve()

        feedback = "\n".join(f"- {item}" for item in previous_feedback) or "- none"
        criteria = "\n".join(f"- {item}" for item in step.acceptance_criteria) or "- no explicit criteria"
        prompt = f"""You are the executor in a deterministic orchestration loop.

Task:
{task.description}

Current step:
{step.description}

Acceptance criteria:
{criteria}

Previous validator feedback:
{feedback}

Produce the requested artifact in the workspace. Do not ask follow-up questions. If assumptions are needed, state them explicitly in the final response.
"""

        cmd = [
            *self.codex_cmd,
            "exec",
            "--cd",
            str(workspace_dir),
            "--sandbox",
            "workspace-write",
            "--output-last-message",
            str(output_path),
            "--skip-git-repo-check",
        ]
        # Pass the executor prompt through stdin instead of as a positional
        # command-line argument. This avoids Windows .cmd/newline quoting issues
        # where Codex receives only the first prompt line.
        completed = subprocess.run(
            cmd,
            input=prompt,
            text=True,
            capture_output=True,
            timeout=self.timeout_seconds,
            check=False,
        )
        content_parts = [
            "# Codex execution result",
            f"command: {command_display}",
            f"workspace: {workspace_dir}",
            f"exit_code: {completed.returncode}",
            "",
            "## stdout",
            completed.stdout.strip(),
            "",
            "## stderr",
            completed.stderr.strip(),
        ]
        if output_path.exists():
            content_parts.extend(["", "## final message", output_path.read_text(encoding="utf-8", errors="replace")])

        workspace_files = self._collect_workspace_text_files(workspace_dir)
        content_parts.extend(["", "## workspace files"])
        if workspace_files:
            for file_path, file_content in workspace_files:
                relative_path = file_path.relative_to(workspace_dir).as_posix()
                content_parts.extend(["", f"### {relative_path}", file_content])
        else:
            content_parts.append("(none)")

        log_path = artifacts_dir / f"{step.id}_attempt_{attempt}_codex_log.md"
        content = "\n".join(content_parts)
        log_path.write_text(content, encoding="utf-8")

        artifact_paths = [str(log_path)]
        if output_path.exists():
            artifact_paths.append(str(output_path))
        artifact_paths.extend(str(file_path) for file_path, _ in workspace_files)

        return ExecutionResult(
            step_id=step.id,
            attempt=attempt,
            status="completed" if completed.returncode == 0 else "failed",
            content=content,
            artifact_paths=artifact_paths,
            notes=[f"executed through {command_display} exec"],
        )
