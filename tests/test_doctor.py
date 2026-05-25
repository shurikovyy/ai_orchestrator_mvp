from __future__ import annotations

from contextlib import contextmanager, redirect_stdout
from io import StringIO
from pathlib import Path
import json
import os
import shutil
import subprocess
import sys
import textwrap
import unittest
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_orchestrator.cli import doctor_main
from ai_orchestrator.doctor import run_doctor

TEST_TEMP_ROOT = Path(__file__).resolve().parents[1] / ".tmp_tests"
TEST_TEMP_ROOT.mkdir(exist_ok=True)


@contextmanager
def temporary_test_dir():
    path = TEST_TEMP_ROOT / f"tmp_{uuid4().hex}"
    path.mkdir(parents=True, exist_ok=False)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@contextmanager
def pushd(path: Path):
    old = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(old)


def git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(repo), *args], text=True, capture_output=True, check=True)


def write_text(path: Path, content: str) -> None:
    path.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")


def make_git_repo(root: Path, *, ignore_tasks_yaml: bool = False) -> Path:
    repo = root / "repo"
    repo.mkdir(parents=True)
    write_text(repo / "README.md", "# Test Repo\n")
    if ignore_tasks_yaml:
        write_text(repo / ".gitignore", "tasks.yaml\n")
    subprocess.run(["git", "init", str(repo)], text=True, capture_output=True, check=True)
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test User")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "seed")
    return repo


def write_tasks_file(repo: Path, content: str) -> Path:
    path = repo / "tasks.yaml"
    write_text(path, content)
    return path


def check_map(result):
    return {check.name: check for check in result.checks}


def write_codex_cli_task(repo: Path, *, enabled: bool = True) -> None:
    write_tasks_file(
        repo,
        f"""
        tasks:
          - id: "codex-task"
            enabled: {str(enabled).lower()}
            backend: codex_cli
            prompt: Demo
        """,
    )


class DoctorTests(unittest.TestCase):
    def test_doctor_succeeds_in_clean_git_repo_with_skip_tests(self) -> None:
        with temporary_test_dir() as tmp:
            repo = make_git_repo(tmp)
            result = run_doctor(skip_tests=True, cwd=repo)

        checks = check_map(result)
        self.assertEqual(result.doctor_status, "ok")
        self.assertEqual(result.doctor_intent, "preflight")
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(checks["git_repo"].status, "ok")
        self.assertEqual(checks["git_clean"].status, "ok")
        self.assertEqual(checks["unit_tests"].status, "skipped")

    def test_doctor_fails_on_tracked_dirty_repo(self) -> None:
        with temporary_test_dir() as tmp:
            repo = make_git_repo(tmp)
            write_text(repo / "README.md", "# Dirty Repo\n")
            result = run_doctor(skip_tests=True, cwd=repo)

        checks = check_map(result)
        self.assertEqual(result.doctor_status, "failed")
        self.assertEqual(checks["git_clean"].status, "error")

    def test_doctor_ignores_ignored_local_tasks_yaml_if_git_clean_otherwise(self) -> None:
        with temporary_test_dir() as tmp:
            repo = make_git_repo(tmp, ignore_tasks_yaml=True)
            write_tasks_file(
                repo,
                """
                tasks:
                  - id: "mock-task"
                    prompt: Demo
                """,
            )
            result = run_doctor(skip_tests=True, cwd=repo)

        checks = check_map(result)
        self.assertEqual(result.doctor_status, "ok")
        self.assertEqual(checks["git_clean"].status, "ok")

    def test_doctor_validates_tasks_file_exists_and_loads(self) -> None:
        with temporary_test_dir() as tmp:
            repo = make_git_repo(tmp)
            write_tasks_file(
                repo,
                """
                defaults:
                  backend: mock
                tasks:
                  - id: "mock-task"
                    prompt: Demo
                  - id: "disabled-task"
                    enabled: false
                    prompt: Demo
                """,
            )
            result = run_doctor(skip_tests=True, cwd=repo, tasks_file="tasks.yaml")

        checks = check_map(result)
        self.assertEqual(checks["tasks_file"].status, "ok")
        self.assertEqual(checks["tasks_file"].details["tasks_total"], 2)
        self.assertEqual(checks["tasks_file"].details["tasks_enabled"], 1)
        self.assertEqual(checks["tasks_file"].details["tasks_disabled"], 1)

    def test_doctor_returns_error_for_missing_tasks_file(self) -> None:
        with temporary_test_dir() as tmp:
            repo = make_git_repo(tmp)
            result = run_doctor(skip_tests=True, cwd=repo, tasks_file="missing.yaml")

        checks = check_map(result)
        self.assertEqual(result.doctor_status, "failed")
        self.assertEqual(checks["tasks_file"].status, "error")

    def test_doctor_validates_task_id_exists(self) -> None:
        with temporary_test_dir() as tmp:
            repo = make_git_repo(tmp)
            write_tasks_file(
                repo,
                """
                tasks:
                  - id: "mock-task"
                    prompt: Demo
                """,
            )
            result = run_doctor(skip_tests=True, cwd=repo, tasks_file="tasks.yaml", task_id="mock-task")

        checks = check_map(result)
        self.assertEqual(checks["task"].status, "ok")
        self.assertEqual(checks["task"].details["task_id"], "mock-task")

    def test_doctor_returns_error_for_missing_task_id(self) -> None:
        with temporary_test_dir() as tmp:
            repo = make_git_repo(tmp)
            write_tasks_file(
                repo,
                """
                tasks:
                  - id: "mock-task"
                    prompt: Demo
                """,
            )
            result = run_doctor(skip_tests=True, cwd=repo, tasks_file="tasks.yaml", task_id="missing-task")

        checks = check_map(result)
        self.assertEqual(result.doctor_status, "failed")
        self.assertEqual(checks["task"].status, "error")

    def test_doctor_returns_error_if_task_disabled(self) -> None:
        with temporary_test_dir() as tmp:
            repo = make_git_repo(tmp)
            write_tasks_file(
                repo,
                """
                tasks:
                  - id: "disabled-task"
                    enabled: false
                    prompt: Demo
                """,
            )
            result = run_doctor(skip_tests=True, cwd=repo, tasks_file="tasks.yaml", task_id="disabled-task")

        checks = check_map(result)
        self.assertEqual(result.doctor_status, "failed")
        self.assertEqual(checks["task"].status, "error")

    def test_doctor_validates_seed_workspace_exists(self) -> None:
        with temporary_test_dir() as tmp:
            repo = make_git_repo(tmp)
            (repo / "seed_workspace").mkdir()
            write_tasks_file(
                repo,
                """
                tasks:
                  - id: "seeded-task"
                    prompt: Demo
                    seed_workspace: "seed_workspace"
                """,
            )
            result = run_doctor(skip_tests=True, cwd=repo, tasks_file="tasks.yaml", task_id="seeded-task")

        checks = check_map(result)
        self.assertEqual(checks["seed_workspace"].status, "ok")

    def test_doctor_returns_error_if_seed_workspace_missing(self) -> None:
        with temporary_test_dir() as tmp:
            repo = make_git_repo(tmp)
            write_tasks_file(
                repo,
                """
                tasks:
                  - id: "seeded-task"
                    prompt: Demo
                    seed_workspace: "missing_seed"
                """,
            )
            result = run_doctor(skip_tests=True, cwd=repo, tasks_file="tasks.yaml", task_id="seeded-task")

        checks = check_map(result)
        self.assertEqual(result.doctor_status, "failed")
        self.assertEqual(checks["seed_workspace"].status, "error")

    def test_doctor_checks_codex_cmd_version_via_mocked_subprocess(self) -> None:
        with temporary_test_dir() as tmp:
            repo = make_git_repo(tmp)
            from ai_orchestrator import doctor as doctor_module

            real_run_process = doctor_module._run_process

            def fake_run_process(command, *, cwd=None, timeout_seconds=30):
                if command == ["codex", "--version"]:
                    return subprocess.CompletedProcess(command, 0, "OpenAI Codex v1.2.3\n", "")
                return real_run_process(command, cwd=cwd, timeout_seconds=timeout_seconds)

            with patch("ai_orchestrator.doctor._run_process", side_effect=fake_run_process):
                with patch("ai_orchestrator.doctor.CodexCliBackend._command_exists", return_value=True):
                    result = run_doctor(skip_tests=True, cwd=repo, codex_cmd="codex")

        checks = check_map(result)
        self.assertEqual(checks["codex_cmd"].status, "ok")
        self.assertEqual(checks["codex_cmd"].details["version"], "OpenAI Codex v1.2.3")

    def test_doctor_returns_error_for_failing_codex_cmd(self) -> None:
        with temporary_test_dir() as tmp:
            repo = make_git_repo(tmp)
            from ai_orchestrator import doctor as doctor_module

            real_run_process = doctor_module._run_process

            def fake_run_process(command, *, cwd=None, timeout_seconds=30):
                if command == ["codex", "--version"]:
                    return subprocess.CompletedProcess(command, 1, "", "boom\n")
                return real_run_process(command, cwd=cwd, timeout_seconds=timeout_seconds)

            with patch("ai_orchestrator.doctor._run_process", side_effect=fake_run_process):
                with patch("ai_orchestrator.doctor.CodexCliBackend._command_exists", return_value=True):
                    result = run_doctor(skip_tests=True, cwd=repo, codex_cmd="codex")

        checks = check_map(result)
        self.assertEqual(result.doctor_status, "failed")
        self.assertEqual(checks["codex_cmd"].status, "error")

    def test_doctor_json_output_is_valid(self) -> None:
        with temporary_test_dir() as tmp:
            repo = make_git_repo(tmp)
            stdout = StringIO()
            with pushd(repo):
                with redirect_stdout(stdout):
                    exit_code = doctor_main(["--skip-tests", "--format", "json"])
            payload = json.loads(stdout.getvalue())

        self.assertEqual(exit_code, 0)
        self.assertIn("doctor_status", payload)
        self.assertEqual(payload["doctor_intent"], "preflight")
        self.assertIn("checks", payload)

    def test_doctor_text_output_includes_default_intent(self) -> None:
        with temporary_test_dir() as tmp:
            repo = make_git_repo(tmp)
            stdout = StringIO()
            with pushd(repo):
                with redirect_stdout(stdout):
                    exit_code = doctor_main(["--skip-tests"])

        self.assertEqual(exit_code, 0)
        self.assertIn("doctor_intent=preflight", stdout.getvalue())

    def test_doctor_strict_returns_nonzero_on_warning(self) -> None:
        with temporary_test_dir() as tmp:
            repo = make_git_repo(tmp)
            write_codex_cli_task(repo)
            stdout = StringIO()
            with pushd(repo):
                with patch.dict(os.environ, {"AI_ORCHESTRATOR_CODEX_CMD": "", "CODEX_CMD": ""}):
                    with redirect_stdout(stdout):
                        exit_code = doctor_main(
                            ["--tasks-file", "tasks.yaml", "--task-id", "codex-task", "--skip-tests", "--strict"]
                        )

        self.assertEqual(exit_code, 1)
        self.assertIn("doctor_status=warning", stdout.getvalue())

    def test_doctor_default_preflight_warns_for_missing_codex_cmd(self) -> None:
        with temporary_test_dir() as tmp:
            repo = make_git_repo(tmp)
            write_codex_cli_task(repo)
            with patch.dict(os.environ, {"AI_ORCHESTRATOR_CODEX_CMD": "", "CODEX_CMD": ""}):
                result = run_doctor(
                    tasks_file="tasks.yaml",
                    task_id="codex-task",
                    skip_tests=True,
                    cwd=repo,
                )

        checks = check_map(result)
        self.assertEqual(result.doctor_intent, "preflight")
        self.assertEqual(result.doctor_status, "warning")
        self.assertEqual(checks["codex_cmd"].status, "warning")
        self.assertEqual(result.next_action, "review_warnings")

    def test_doctor_dry_run_intent_does_not_warn_for_missing_codex_cmd(self) -> None:
        with temporary_test_dir() as tmp:
            repo = make_git_repo(tmp)
            write_codex_cli_task(repo)
            with patch.dict(os.environ, {"AI_ORCHESTRATOR_CODEX_CMD": "", "CODEX_CMD": ""}):
                result = run_doctor(
                    tasks_file="tasks.yaml",
                    task_id="codex-task",
                    skip_tests=True,
                    strict=True,
                    intent="dry-run",
                    cwd=repo,
                )

        checks = check_map(result)
        self.assertEqual(result.doctor_status, "ok")
        self.assertEqual(result.doctor_intent, "dry-run")
        self.assertEqual(checks["codex_cmd"].status, "info")
        self.assertEqual(checks["codex_cmd"].message, "codex command is not required for dry-run intent")
        self.assertEqual(result.next_action, "run_pipeline_dry_run")
        self.assertEqual(result.exit_code, 0)

    def test_doctor_dry_run_json_output_includes_intent_and_next_action(self) -> None:
        with temporary_test_dir() as tmp:
            repo = make_git_repo(tmp)
            write_codex_cli_task(repo)
            stdout = StringIO()
            with pushd(repo):
                with patch.dict(os.environ, {"AI_ORCHESTRATOR_CODEX_CMD": "", "CODEX_CMD": ""}):
                    with redirect_stdout(stdout):
                        exit_code = doctor_main(
                            [
                                "--tasks-file",
                                "tasks.yaml",
                                "--task-id",
                                "codex-task",
                                "--skip-tests",
                                "--strict",
                                "--intent",
                                "dry-run",
                                "--format",
                                "json",
                            ]
                        )
            payload = json.loads(stdout.getvalue())

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["doctor_status"], "ok")
        self.assertEqual(payload["doctor_intent"], "dry-run")
        self.assertEqual(payload["next_action"], "run_pipeline_dry_run")
        codex_check = next(check for check in payload["checks"] if check["name"] == "codex_cmd")
        self.assertEqual(codex_check["status"], "info")

    def test_doctor_real_run_intent_warns_for_missing_codex_cmd(self) -> None:
        with temporary_test_dir() as tmp:
            repo = make_git_repo(tmp)
            write_codex_cli_task(repo)
            stdout = StringIO()
            with pushd(repo):
                with patch.dict(os.environ, {"AI_ORCHESTRATOR_CODEX_CMD": "", "CODEX_CMD": ""}):
                    with redirect_stdout(stdout):
                        exit_code = doctor_main(
                            [
                                "--tasks-file",
                                "tasks.yaml",
                                "--task-id",
                                "codex-task",
                                "--skip-tests",
                                "--intent",
                                "real-run",
                            ]
                        )

        output = stdout.getvalue()
        self.assertEqual(exit_code, 0)
        self.assertIn("doctor_status=warning", output)
        self.assertIn("doctor_intent=real-run", output)
        self.assertIn('check=codex_cmd status=warning message="codex command is not configured for this codex_cli task"', output)
        self.assertIn("next_action=review_warnings", output)

    def test_doctor_real_run_intent_strict_fails_for_missing_codex_cmd(self) -> None:
        with temporary_test_dir() as tmp:
            repo = make_git_repo(tmp)
            write_codex_cli_task(repo)
            stdout = StringIO()
            with pushd(repo):
                with patch.dict(os.environ, {"AI_ORCHESTRATOR_CODEX_CMD": "", "CODEX_CMD": ""}):
                    with redirect_stdout(stdout):
                        exit_code = doctor_main(
                            [
                                "--tasks-file",
                                "tasks.yaml",
                                "--task-id",
                                "codex-task",
                                "--skip-tests",
                                "--intent",
                                "real-run",
                                "--strict",
                            ]
                        )

        self.assertEqual(exit_code, 1)
        self.assertIn("doctor_status=warning", stdout.getvalue())

    def test_doctor_dry_run_intent_with_explicit_invalid_codex_cmd_fails(self) -> None:
        with temporary_test_dir() as tmp:
            repo = make_git_repo(tmp)
            write_codex_cli_task(repo)
            with patch("ai_orchestrator.doctor.CodexCliBackend._command_exists", return_value=False):
                result = run_doctor(
                    tasks_file="tasks.yaml",
                    task_id="codex-task",
                    codex_cmd="missing-codex",
                    skip_tests=True,
                    intent="dry-run",
                    cwd=repo,
                )

        checks = check_map(result)
        self.assertEqual(result.doctor_status, "failed")
        self.assertEqual(checks["codex_cmd"].status, "error")
        self.assertEqual(result.next_action, "fix_errors")

    def test_doctor_dry_run_intent_still_fails_on_missing_task_id(self) -> None:
        with temporary_test_dir() as tmp:
            repo = make_git_repo(tmp)
            write_codex_cli_task(repo)
            result = run_doctor(
                tasks_file="tasks.yaml",
                task_id="missing-task",
                skip_tests=True,
                intent="dry-run",
                cwd=repo,
            )

        checks = check_map(result)
        self.assertEqual(result.doctor_status, "failed")
        self.assertEqual(checks["task"].status, "error")
        self.assertEqual(result.next_action, "fix_errors")

    def test_doctor_dry_run_intent_still_fails_on_disabled_task(self) -> None:
        with temporary_test_dir() as tmp:
            repo = make_git_repo(tmp)
            write_codex_cli_task(repo, enabled=False)
            result = run_doctor(
                tasks_file="tasks.yaml",
                task_id="codex-task",
                skip_tests=True,
                intent="dry-run",
                cwd=repo,
            )

        checks = check_map(result)
        self.assertEqual(result.doctor_status, "failed")
        self.assertEqual(checks["task"].status, "error")

    def test_doctor_real_run_intent_with_valid_mocked_codex_cmd_passes(self) -> None:
        with temporary_test_dir() as tmp:
            repo = make_git_repo(tmp)
            write_codex_cli_task(repo)
            from ai_orchestrator import doctor as doctor_module

            real_run_process = doctor_module._run_process

            def fake_run_process(command, *, cwd=None, timeout_seconds=30):
                if command == ["codex", "--version"]:
                    return subprocess.CompletedProcess(command, 0, "OpenAI Codex v1.2.3\n", "")
                return real_run_process(command, cwd=cwd, timeout_seconds=timeout_seconds)

            with patch("ai_orchestrator.doctor._run_process", side_effect=fake_run_process):
                with patch("ai_orchestrator.doctor.CodexCliBackend._command_exists", return_value=True):
                    result = run_doctor(
                        tasks_file="tasks.yaml",
                        task_id="codex-task",
                        codex_cmd="codex",
                        skip_tests=True,
                        intent="real-run",
                        cwd=repo,
                    )

        checks = check_map(result)
        self.assertEqual(result.doctor_status, "ok")
        self.assertEqual(result.doctor_intent, "real-run")
        self.assertEqual(checks["codex_cmd"].status, "ok")
        self.assertEqual(result.next_action, "run_pipeline")

    def test_doctor_skip_tests_marks_unit_tests_skipped(self) -> None:
        with temporary_test_dir() as tmp:
            repo = make_git_repo(tmp)
            result = run_doctor(skip_tests=True, cwd=repo)

        checks = check_map(result)
        self.assertEqual(checks["unit_tests"].status, "skipped")

    def test_doctor_does_not_create_files(self) -> None:
        with temporary_test_dir() as tmp:
            repo = make_git_repo(tmp)
            before = sorted(str(path.relative_to(repo)) for path in repo.rglob("*"))
            result = run_doctor(skip_tests=True, cwd=repo)
            after = sorted(str(path.relative_to(repo)) for path in repo.rglob("*"))

        self.assertEqual(result.doctor_status, "ok")
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
