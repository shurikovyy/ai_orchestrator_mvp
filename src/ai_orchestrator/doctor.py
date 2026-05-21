from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from ai_orchestrator.backends.codex_cli import CodexCliBackend
from ai_orchestrator.task_queue import TaskQueueConfig, TaskQueueConfigError, get_task_definition, load_task_queue_config

DoctorCheckStatus = Literal["ok", "warning", "error", "info", "skipped"]
DoctorOverallStatus = Literal["ok", "warning", "failed"]


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    status: DoctorCheckStatus
    message: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DoctorResult:
    doctor_status: DoctorOverallStatus
    next_action: str
    checks: list[DoctorCheck]
    strict: bool = False

    @property
    def exit_code(self) -> int:
        if self.doctor_status == "failed":
            return 1
        if self.doctor_status == "warning" and self.strict:
            return 1
        return 0


def _run_process(
    command: list[str],
    *,
    cwd: Path | None = None,
    timeout_seconds: int = 30,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=str(cwd) if cwd is not None else None,
        text=True,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_seconds,
        check=False,
    )


def _git_command(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return _run_process(["git", "-C", str(cwd), *args], cwd=cwd)


def _resolve_input_path(value: str | Path, *, cwd: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = cwd / path
    return path.resolve(strict=False)


def _summarize_unittest_output(stdout: str, stderr: str) -> str:
    combined = "\n".join(part for part in [stdout.strip(), stderr.strip()] if part)
    ran_match = re.search(r"Ran\s+(\d+)\s+tests?\s+in\s+.+", combined)
    if "OK" in combined:
        if ran_match:
            return f"Ran {ran_match.group(1)} tests OK"
        return "tests passed"
    failed_match = re.search(r"FAILED\s+\((.+)\)", combined)
    if failed_match and ran_match:
        return f"Ran {ran_match.group(1)} tests FAILED ({failed_match.group(1)})"
    if failed_match:
        return f"tests failed ({failed_match.group(1)})"
    return "tests failed"


def _count_tasks(config: TaskQueueConfig) -> tuple[int, int, int]:
    total = len(config.tasks)
    enabled = sum(1 for task in config.tasks if task.enabled)
    disabled = total - enabled
    return total, enabled, disabled


def _effective_codex_command(
    *,
    cli_codex_cmd: str | None,
    task_codex_cmd: str | None,
    default_codex_cmd: str | None,
) -> tuple[str | None, str | None]:
    if cli_codex_cmd:
        return cli_codex_cmd, "cli"
    if task_codex_cmd:
        return task_codex_cmd, "task"
    if default_codex_cmd:
        return default_codex_cmd, "defaults"
    env_cmd = os.environ.get("AI_ORCHESTRATOR_CODEX_CMD")
    if env_cmd:
        return env_cmd, "env:AI_ORCHESTRATOR_CODEX_CMD"
    env_cmd = os.environ.get("CODEX_CMD")
    if env_cmd:
        return env_cmd, "env:CODEX_CMD"
    return None, None


def _check_codex_command(command: str) -> DoctorCheck:
    normalized = CodexCliBackend._normalize_command(command)
    binary = normalized[0]
    if not CodexCliBackend._command_exists(binary):
        return DoctorCheck(
            name="codex_cmd",
            status="error",
            message="codex command binary was not found",
            details={
                "command": command,
                "binary": binary,
            },
        )

    completed = _run_process([*normalized, "--version"], timeout_seconds=10)
    version_line = next(
        (
            line.strip()
            for line in (completed.stdout.splitlines() + completed.stderr.splitlines())
            if line.strip()
        ),
        "",
    )
    if completed.returncode != 0:
        return DoctorCheck(
            name="codex_cmd",
            status="error",
            message="codex command failed to report version",
            details={
                "command": command,
                "exit_code": completed.returncode,
                "version": version_line,
            },
        )
    return DoctorCheck(
        name="codex_cmd",
        status="ok",
        message="codex command is available",
        details={
            "command": command,
            "version": version_line,
        },
    )


def _compute_next_action(checks: list[DoctorCheck], *, tasks_file: str | None, task_id: str | None) -> str:
    if any(check.status == "error" for check in checks):
        return "fix_errors"
    if any(check.status == "warning" for check in checks):
        return "review_warnings"
    if tasks_file or task_id:
        return "run_pipeline"
    return "ready"


def _compute_overall_status(checks: list[DoctorCheck]) -> DoctorOverallStatus:
    if any(check.status == "error" for check in checks):
        return "failed"
    if any(check.status == "warning" for check in checks):
        return "warning"
    return "ok"


def run_doctor(
    *,
    tasks_file: str | None = None,
    task_id: str | None = None,
    codex_cmd: str | None = None,
    skip_tests: bool = False,
    strict: bool = False,
    cwd: str | Path | None = None,
) -> DoctorResult:
    repo_cwd = Path.cwd() if cwd is None else Path(cwd).expanduser().resolve()
    checks: list[DoctorCheck] = []

    git_repo_result = _git_command(repo_cwd, "rev-parse", "--is-inside-work-tree")
    if git_repo_result.returncode == 0 and git_repo_result.stdout.strip().lower() == "true":
        checks.append(DoctorCheck("git_repo", "ok", "git repository detected"))
        git_clean_result = _git_command(repo_cwd, "status", "--short", "--untracked-files=no")
        tracked_status = git_clean_result.stdout.strip()
        if git_clean_result.returncode != 0:
            checks.append(
                DoctorCheck(
                    "git_clean",
                    "error",
                    "failed to inspect tracked git working tree state",
                    details={"git_exit_code": git_clean_result.returncode},
                )
            )
        elif tracked_status:
            checks.append(
                DoctorCheck(
                    "git_clean",
                    "error",
                    "tracked working tree is not clean",
                    details={"tracked_status": tracked_status},
                )
            )
        else:
            checks.append(DoctorCheck("git_clean", "ok", "tracked working tree clean"))
    else:
        checks.append(DoctorCheck("git_repo", "error", "current directory is not a git repository"))
        checks.append(DoctorCheck("git_clean", "skipped", "git cleanliness check skipped because git repository was not detected"))

    if skip_tests:
        checks.append(
            DoctorCheck(
                "unit_tests",
                "skipped",
                "unit tests were skipped",
                details={"command": "python -m unittest discover -s tests"},
            )
        )
    else:
        test_command = [sys.executable, "-m", "unittest", "discover", "-s", "tests"]
        test_result = _run_process(test_command, cwd=repo_cwd, timeout_seconds=600)
        summary = _summarize_unittest_output(test_result.stdout, test_result.stderr)
        if test_result.returncode == 0:
            checks.append(
                DoctorCheck(
                    "unit_tests",
                    "ok",
                    "unit tests passed",
                    details={
                        "command": "python -m unittest discover -s tests",
                        "summary": summary,
                    },
                )
            )
        else:
            checks.append(
                DoctorCheck(
                    "unit_tests",
                    "error",
                    "unit tests failed",
                    details={
                        "command": "python -m unittest discover -s tests",
                        "summary": summary,
                        "exit_code": test_result.returncode,
                    },
                )
            )

    loaded_config: TaskQueueConfig | None = None
    normalized_tasks_path: Path | None = None
    effective_codex_command: str | None = codex_cmd
    effective_codex_command_source: str | None = "cli" if codex_cmd else None

    if tasks_file is not None:
        normalized_tasks_path = _resolve_input_path(tasks_file, cwd=repo_cwd)
        if not normalized_tasks_path.exists():
            checks.append(
                DoctorCheck(
                    "tasks_file",
                    "error",
                    "tasks file not found",
                    details={"path": str(normalized_tasks_path)},
                )
            )
        elif not normalized_tasks_path.is_file():
            checks.append(
                DoctorCheck(
                    "tasks_file",
                    "error",
                    "tasks file is not a file",
                    details={"path": str(normalized_tasks_path)},
                )
            )
        else:
            try:
                loaded_config = load_task_queue_config(normalized_tasks_path)
            except (TaskQueueConfigError, FileNotFoundError) as exc:
                checks.append(
                    DoctorCheck(
                        "tasks_file",
                        "error",
                        str(exc),
                        details={"path": str(normalized_tasks_path)},
                    )
                )
            else:
                total, enabled, disabled = _count_tasks(loaded_config)
                checks.append(
                    DoctorCheck(
                        "tasks_file",
                        "ok",
                        "tasks file loaded successfully",
                        details={
                            "path": str(normalized_tasks_path),
                            "tasks_total": total,
                            "tasks_enabled": enabled,
                            "tasks_disabled": disabled,
                        },
                    )
                )

    if task_id is not None:
        if loaded_config is None or normalized_tasks_path is None:
            checks.append(
                DoctorCheck(
                    "task",
                    "error",
                    "--task-id requires a valid --tasks-file",
                    details={"task_id": task_id},
                )
            )
        else:
            try:
                task = get_task_definition(loaded_config, task_id)
            except TaskQueueConfigError as exc:
                checks.append(DoctorCheck("task", "error", str(exc), details={"task_id": task_id}))
            else:
                if not task.enabled:
                    checks.append(
                        DoctorCheck(
                            "task",
                            "error",
                            "task is disabled",
                            details={
                                "task_id": task.id,
                                "enabled": task.enabled,
                                "backend": task.backend or loaded_config.defaults.backend or "mock",
                                "seed_workspace": task.seed_workspace or "",
                            },
                        )
                    )
                else:
                    resolved_seed_workspace = ""
                    if task.seed_workspace:
                        seed_path = Path(task.seed_workspace).expanduser()
                        if not seed_path.is_absolute():
                            seed_path = normalized_tasks_path.parent / seed_path
                        seed_path = seed_path.resolve(strict=False)
                        resolved_seed_workspace = str(seed_path)
                        if not seed_path.exists():
                            checks.append(
                                DoctorCheck(
                                    "seed_workspace",
                                    "error",
                                    "seed workspace does not exist",
                                    details={
                                        "task_id": task.id,
                                        "seed_workspace": task.seed_workspace,
                                        "resolved_path": resolved_seed_workspace,
                                    },
                                )
                            )
                        elif not seed_path.is_dir():
                            checks.append(
                                DoctorCheck(
                                    "seed_workspace",
                                    "error",
                                    "seed workspace is not a directory",
                                    details={
                                        "task_id": task.id,
                                        "seed_workspace": task.seed_workspace,
                                        "resolved_path": resolved_seed_workspace,
                                    },
                                )
                            )
                        else:
                            checks.append(
                                DoctorCheck(
                                    "seed_workspace",
                                    "ok",
                                    "seed workspace exists",
                                    details={
                                        "task_id": task.id,
                                        "seed_workspace": task.seed_workspace,
                                        "resolved_path": resolved_seed_workspace,
                                    },
                                )
                            )

                    resolved_backend = task.backend or loaded_config.defaults.backend or "mock"
                    effective_codex_command, effective_codex_command_source = _effective_codex_command(
                        cli_codex_cmd=codex_cmd,
                        task_codex_cmd=task.codex_cmd,
                        default_codex_cmd=loaded_config.defaults.codex_cmd,
                    )
                    checks.append(
                        DoctorCheck(
                            "task",
                            "ok",
                            "task is enabled and available",
                            details={
                                "task_id": task.id,
                                "enabled": task.enabled,
                                "backend": resolved_backend,
                                "seed_workspace": task.seed_workspace or "",
                            },
                        )
                    )
                    if resolved_backend in {"codex", "codex_cli"} and effective_codex_command is None:
                        checks.append(
                            DoctorCheck(
                                "codex_cmd",
                                "warning",
                                "codex command is not configured for this codex_cli task",
                                details={
                                    "task_id": task.id,
                                },
                            )
                        )

    if codex_cmd is not None:
        checks.append(_check_codex_command(codex_cmd))
    elif effective_codex_command is not None:
        codex_check = _check_codex_command(effective_codex_command)
        checks.append(
            DoctorCheck(
                name=codex_check.name,
                status=codex_check.status,
                message=codex_check.message,
                details={
                    **codex_check.details,
                    "source": effective_codex_command_source or "",
                },
            )
        )

    checks.append(
        DoctorCheck(
            "nested_codex",
            "info",
            "doctor cannot reliably detect nested Codex sessions; do not run codex_cli pipeline from inside Codex agent session.",
        )
    )

    runs_dir = repo_cwd / ".runs"
    tmp_tests_dir = repo_cwd / ".tmp_tests"
    node_modules_dir = repo_cwd / "node_modules"
    venv_dir = repo_cwd / ".venv"
    checks.append(
        DoctorCheck(
            "runtime_artifacts",
            "info",
            "runtime artifact directories inspected",
            details={
                "runs_exists": runs_dir.exists(),
                "tmp_tests_exists": tmp_tests_dir.exists(),
                "node_modules_exists": node_modules_dir.exists(),
                "venv_exists": venv_dir.exists(),
            },
        )
    )

    overall_status = _compute_overall_status(checks)
    next_action = _compute_next_action(checks, tasks_file=tasks_file, task_id=task_id)
    return DoctorResult(
        doctor_status=overall_status,
        next_action=next_action,
        checks=checks,
        strict=strict,
    )


def _format_text_value(value: Any) -> str:
    if isinstance(value, bool):
        return str(value).lower()
    if value is None:
        return '""'
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)


def format_doctor_text(result: DoctorResult) -> str:
    lines = [f"doctor_status={result.doctor_status}"]
    for check in result.checks:
        parts = [
            f"check={check.name}",
            f"status={check.status}",
            f"message={json.dumps(check.message, ensure_ascii=False)}",
        ]
        for key, value in check.details.items():
            parts.append(f"{key}={_format_text_value(value)}")
        lines.append(" ".join(parts))
    lines.append(f"next_action={result.next_action}")
    return "\n".join(lines)


def format_doctor_json(result: DoctorResult) -> str:
    payload = {
        "doctor_status": result.doctor_status,
        "next_action": result.next_action,
        "checks": [
            {
                "name": check.name,
                "status": check.status,
                "message": check.message,
                **check.details,
            }
            for check in result.checks
        ],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)
