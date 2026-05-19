from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
from collections.abc import Sequence
from pathlib import Path

from ai_orchestrator.backends.mock import MockBackend
from ai_orchestrator.schemas import ExecutionResult, PlanStep, TaskSpec
from ai_orchestrator.validation import write_workspace_manifest_snapshot


class CodexCliBackend(MockBackend):
    """Codex CLI executor adapter.

    Planner and validator stay deterministic in this MVP. Execution is delegated
    to ``codex exec`` or to a compatible command such as ``npx @openai/codex exec``.
    """

    name = "codex_cli"
    _seed_ignored_names = {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        ".runs",
        ".tmp_tests",
        ".codex_home",
        ".codex_temp",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "build",
        "dist",
    }

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
        stream_output: bool = False,
    ) -> None:
        # Priority:
        # 1. Explicit CLI argument passed by ai_orchestrator.cli
        # 2. AI_ORCHESTRATOR_CODEX_CMD env var
        # 3. CODEX_CMD env var
        # 4. Plain "codex" from PATH
        raw_cmd = codex_cmd or os.environ.get("AI_ORCHESTRATOR_CODEX_CMD") or os.environ.get("CODEX_CMD") or "codex"
        self.codex_cmd = self._normalize_command(raw_cmd)
        self.timeout_seconds = timeout_seconds
        self.stream_output = stream_output

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
    def _collect_workspace_text_files(
        cls,
        workspace_dir: Path,
        *,
        include_paths: set[str] | None = None,
    ) -> list[tuple[Path, str]]:
        collected: list[tuple[Path, str]] = []
        include_keys = {path.replace("\\", "/").strip().lstrip("./").lower() for path in include_paths or set()}
        for path in sorted(workspace_dir.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in cls._workspace_text_extensions:
                continue
            rel_path = path.relative_to(workspace_dir).as_posix()
            if include_keys and rel_path.lower() not in include_keys:
                continue
            collected.append((path, path.read_text(encoding="utf-8", errors="replace")))
        return collected

    @classmethod
    def _seed_ignore(cls, directory: str, names: list[str]) -> set[str]:
        del directory
        return {name for name in names if name in cls._seed_ignored_names or name.endswith(".egg-info")}

    @classmethod
    def _copy_seed_workspace(cls, seed_workspace_path: str, workspace_dir: Path) -> None:
        seed_path = Path(seed_workspace_path).expanduser()
        if not seed_path.exists():
            raise FileNotFoundError(f"seed workspace does not exist: {seed_workspace_path}")
        if not seed_path.is_dir():
            raise NotADirectoryError(f"seed workspace is not a directory: {seed_workspace_path}")
        if workspace_dir.exists():
            shutil.rmtree(workspace_dir)
        shutil.copytree(seed_path, workspace_dir, ignore=cls._seed_ignore)

    @staticmethod
    def _workspace_files_to_capture_for_seed(workspace_dir: Path) -> set[str]:
        # With a seeded project, capturing every text file would duplicate the
        # whole input repo into the Codex log. Capture only the structured report
        # plus files that the report claims changed. If the report is invalid or
        # missing, this still captures EXECUTION_REPORT.json when present.
        paths = {"EXECUTION_REPORT.json"}
        report_path = workspace_dir / "EXECUTION_REPORT.json"
        if report_path.exists():
            try:
                payload = json.loads(report_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return paths
            changed_files = payload.get("changed_files", [])
            if isinstance(changed_files, list):
                paths.update(str(item) for item in changed_files)
        return paths


    @staticmethod
    def _stream_process_output(stream, sink: list[str], prefix: str) -> None:
        try:
            for line in iter(stream.readline, ""):
                sink.append(line)
                print(f"{prefix}{line.rstrip()}", file=sys.stderr, flush=True)
        except UnicodeDecodeError as exc:
            diagnostic = f"ERROR: failed to decode subprocess stream: {exc}"
            sink.append(diagnostic + "\n")
            print(f"{prefix}{diagnostic}", file=sys.stderr, flush=True)
        except Exception as exc:  # noqa: BLE001 - diagnostic path should never kill the stream thread.
            diagnostic = f"ERROR: failed while reading subprocess stream: {type(exc).__name__}: {exc}"
            sink.append(diagnostic + "\n")
            print(f"{prefix}{diagnostic}", file=sys.stderr, flush=True)
        finally:
            try:
                stream.close()
            except Exception:  # noqa: BLE001 - best-effort cleanup only.
                pass

    def _run_codex_process(self, cmd: list[str], prompt: str) -> subprocess.CompletedProcess[str]:
        if not self.stream_output:
            return subprocess.run(
                cmd,
                input=prompt,
                text=True,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_seconds,
                check=False,
            )

        print("[codex-cli] starting codex exec", file=sys.stderr, flush=True)
        stdout_lines: list[str] = []
        stderr_lines: list[str] = []
        process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None
        process.stdin.write(prompt)
        process.stdin.close()

        stdout_thread = threading.Thread(
            target=self._stream_process_output,
            args=(process.stdout, stdout_lines, "[codex:stdout] "),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=self._stream_process_output,
            args=(process.stderr, stderr_lines, "[codex:stderr] "),
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()
        timed_out = False
        try:
            returncode = process.wait(timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            process.kill()
            returncode = process.wait()
        stdout_thread.join(timeout=5)
        stderr_thread.join(timeout=5)
        stdout = "".join(stdout_lines)
        stderr = "".join(stderr_lines)
        if timed_out:
            stderr += f"\nERROR: codex exec timed out after {self.timeout_seconds} seconds."
        print(f"[codex-cli] finished codex exec exit_code={returncode}", file=sys.stderr, flush=True)
        return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)

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
        baseline_manifest_path = artifacts_dir / "workspace_baseline_manifest.json"

        if task.seed_workspace_path:
            try:
                if attempt == 1:
                    self._copy_seed_workspace(task.seed_workspace_path, workspace_dir)
                    write_workspace_manifest_snapshot(baseline_manifest_path, workspace_dir)
                else:
                    workspace_dir.mkdir(parents=True, exist_ok=True)
                    if not baseline_manifest_path.exists():
                        raise FileNotFoundError(
                            "seed workspace baseline manifest is missing; cannot validate diff reliably"
                        )
            except (OSError, ValueError) as exc:
                log_path = artifacts_dir / f"{step.id}_attempt_{attempt}_codex_log.md"
                content = "\n".join([
                    "# Codex execution result",
                    f"command: {command_display}",
                    f"workspace: {workspace_dir}",
                    f"seed_workspace: {task.seed_workspace_path}",
                    "exit_code: not_run",
                    "",
                    "## error",
                    f"Could not prepare seed workspace: {exc}",
                ])
                log_path.write_text(content, encoding="utf-8")
                return ExecutionResult(
                    step_id=step.id,
                    attempt=attempt,
                    status="failed",
                    content=content,
                    artifact_paths=[str(log_path)],
                    notes=["failed before Codex execution while preparing seed workspace"],
                )
        else:
            workspace_dir.mkdir(parents=True, exist_ok=True)

        output_path = (artifacts_dir / f"{step.id}_attempt_{attempt}_codex_final.md").resolve()

        feedback = "\n".join(f"- {item}" for item in previous_feedback) or "- none"
        criteria = "\n".join(f"- {item}" for item in step.acceptance_criteria) or "- no explicit criteria"
        rework_feedback_block = ""
        if task.rework_feedback:
            rework_feedback_block = f"""

Human review feedback for this rework run:
{task.rework_feedback}

Rules:
- Treat this feedback as authoritative correction guidance.
- Do not ignore it.
- If it conflicts with the original task, explain the conflict in EXECUTION_REPORT.json assumptions/risks.
- Do not ask follow-up questions.
"""
        structured_report_instruction = ""
        if task.require_structured_report:
            structured_report_instruction = """

Structured execution report is required. Create or overwrite `EXECUTION_REPORT.json` at the workspace root. It must be valid JSON, not Markdown, and match this schema:
{
  "schema_version": "1.0",
  "status": "completed | failed | partial",
  "summary": "short summary of what changed",
  "changed_files": ["relative/path.ext"],
  "commands_run": [
    {"command": "command string", "exit_code": 0, "status": "passed | failed | skipped", "summary": "short result"}
  ],
  "tests": [
    {"name": "test name", "command": "test command", "status": "passed | failed | skipped | not_run", "total": 0, "passed": 0, "failed": 0, "output": "short test output"}
  ],
  "risks": ["risk or empty list"],
  "assumptions": ["assumption or empty list"],
  "validation_notes": ["note or empty list"]
}
Use `status: completed` only when the requested artifact is produced and required checks pass. If tests are requested, include at least one item in `tests` and set its status to `passed` only if the command actually passed.
The deterministic validator may rerun each command from `tests[*].command`, so every test command must be safe, repeatable, and runnable from the workspace root.
"""
            if task.validate_workspace_manifest:
                structured_report_instruction += """
The deterministic validator will also compare `changed_files` against reportable files that actually exist in the workspace. If a seed workspace was provided, list only files added, modified, or deleted relative to the seed baseline. If no seed workspace was provided, include every created/modified source/report file in `changed_files`. Always include `EXECUTION_REPORT.json` when it is created or modified. Do not include unchanged seed files or generated/runtime files such as `__pycache__/`, `*.pyc`, `.pytest_cache/`, `.mypy_cache/`, or `.ruff_cache/`.
"""

        prompt = f"""You are the executor in a deterministic orchestration loop.

Task:
{task.description}

Current step:
{step.description}

Acceptance criteria:
{criteria}

Previous validator feedback:
{feedback}
{rework_feedback_block}
{structured_report_instruction}
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
        completed = self._run_codex_process(cmd, prompt)
        content_parts = [
            "# Codex execution result",
            f"command: {command_display}",
            f"workspace: {workspace_dir}",
            f"seed_workspace: {task.seed_workspace_path or '(none)'}",
            f"baseline_manifest: {baseline_manifest_path if baseline_manifest_path.exists() else '(none)'}",
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

        after_manifest_path = artifacts_dir / f"{step.id}_attempt_{attempt}_workspace_manifest.json"
        write_workspace_manifest_snapshot(after_manifest_path, workspace_dir)

        include_paths = self._workspace_files_to_capture_for_seed(workspace_dir) if task.seed_workspace_path else None
        workspace_files = self._collect_workspace_text_files(workspace_dir, include_paths=include_paths)
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
        if baseline_manifest_path.exists():
            artifact_paths.append(str(baseline_manifest_path))
        artifact_paths.append(str(after_manifest_path))
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
