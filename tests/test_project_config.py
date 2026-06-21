from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import textwrap
import uuid
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_orchestrator.config import ProjectConfigError, resolve_project_config


REPO_ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def temp_project() -> Iterator[str]:
    root = REPO_ROOT / f"tmp_project_config_{uuid.uuid4().hex}"
    root.mkdir()
    try:
        yield str(root)
    finally:
        shutil.rmtree(root)


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")


def clean_cli_env() -> dict[str, str]:
    env = dict(os.environ)
    for key in (
        "AI_ORCHESTRATOR_TASKS_FILE",
        "AI_ORCHESTRATOR_RUNS_DIR",
        "AI_ORCHESTRATOR_TASK_DRAFTS_DIR",
        "AI_ORCHESTRATOR_CODEX_CMD",
        "CODEX_CMD",
        "AI_ORCHESTRATOR_POLICY_FILE",
    ):
        env.pop(key, None)
    src_path = str(REPO_ROOT / "src")
    env["PYTHONPATH"] = src_path if not env.get("PYTHONPATH") else f"{src_path}{os.pathsep}{env['PYTHONPATH']}"
    return env


class ProjectConfigResolverTests(unittest.TestCase):
    def test_defaults_without_config_env_or_cli(self) -> None:
        with temp_project() as tmp:
            root = Path(tmp)

            resolved = resolve_project_config(project_root=root, env={})

            self.assertIsNone(resolved.config_file)
            self.assertEqual(resolved.schema_version, "1.0")
            self.assertEqual(resolved.tasks_file.value, str((root / "tasks.yaml").resolve()))
            self.assertEqual(resolved.tasks_file.source, "default")
            self.assertEqual(resolved.runs_dir.value, str((root / ".runs").resolve()))
            self.assertEqual(resolved.runs_dir.source, "default")
            self.assertEqual(resolved.task_drafts_dir.value, str((root / ".task_drafts").resolve()))
            self.assertEqual(resolved.task_drafts_dir.source, "default")
            self.assertIsNone(resolved.codex_cmd.value)
            self.assertEqual(resolved.codex_cmd.source, "default")
            self.assertEqual(resolved.default_review_profiles.value, ["qa", "maintainability"])
            self.assertEqual(resolved.default_review_profiles.source, "default")
            self.assertEqual(resolved.policy_file.value, str((root / "autonomy_policy.yaml").resolve()))
            self.assertEqual(resolved.policy_file.source, "default")

    def test_explicit_config_path_loads_yaml_values(self) -> None:
        with temp_project() as tmp:
            root = Path(tmp)
            config_path = root / "custom.yaml"
            write_text(
                config_path,
                """
                schema_version: "1.0"
                paths:
                  tasks_file: config/tasks.yaml
                  runs_dir: var/runs
                  task_drafts_dir: var/drafts
                codex:
                  cmd: C:/Tools/codex.cmd
                review:
                  default_profiles:
                    - security
                    - qa
                policy:
                  policy_file: config/policy.yaml
                """,
            )

            resolved = resolve_project_config(project_root=root, config_path=config_path, env={})

            self.assertEqual(resolved.config_file, config_path.resolve())
            self.assertEqual(resolved.tasks_file.value, str((root / "config" / "tasks.yaml").resolve()))
            self.assertEqual(resolved.tasks_file.source, "config")
            self.assertEqual(resolved.runs_dir.value, str((root / "var" / "runs").resolve()))
            self.assertEqual(resolved.runs_dir.source, "config")
            self.assertEqual(resolved.task_drafts_dir.value, str((root / "var" / "drafts").resolve()))
            self.assertEqual(resolved.task_drafts_dir.source, "config")
            self.assertEqual(resolved.codex_cmd.value, "C:/Tools/codex.cmd")
            self.assertEqual(resolved.codex_cmd.source, "config")
            self.assertEqual(resolved.default_review_profiles.value, ["security", "qa"])
            self.assertEqual(resolved.default_review_profiles.source, "config")
            self.assertEqual(resolved.policy_file.value, str((root / "config" / "policy.yaml").resolve()))
            self.assertEqual(resolved.policy_file.source, "config")

    def test_auto_discovery_finds_root_config(self) -> None:
        with temp_project() as tmp:
            root = Path(tmp)
            write_text(root / "ai_orchestrator.yaml", "paths:\n  runs_dir: root-runs\n")

            resolved = resolve_project_config(project_root=root, env={})

            self.assertEqual(resolved.config_file, (root / "ai_orchestrator.yaml").resolve())
            self.assertEqual(resolved.runs_dir.value, str((root / "root-runs").resolve()))
            self.assertEqual(resolved.runs_dir.source, "config")

    def test_auto_discovery_finds_dot_config(self) -> None:
        with temp_project() as tmp:
            root = Path(tmp)
            write_text(root / ".ai_orchestrator" / "config.yaml", "paths:\n  runs_dir: dot-runs\n")

            resolved = resolve_project_config(project_root=root, env={})

            self.assertEqual(resolved.config_file, (root / ".ai_orchestrator" / "config.yaml").resolve())
            self.assertEqual(resolved.runs_dir.value, str((root / "dot-runs").resolve()))
            self.assertEqual(resolved.runs_dir.source, "config")

    def test_root_config_wins_over_dot_config(self) -> None:
        with temp_project() as tmp:
            root = Path(tmp)
            write_text(root / "ai_orchestrator.yaml", "paths:\n  runs_dir: root-runs\n")
            write_text(root / ".ai_orchestrator" / "config.yaml", "paths:\n  runs_dir: dot-runs\n")

            resolved = resolve_project_config(project_root=root, env={})

            self.assertEqual(resolved.config_file, (root / "ai_orchestrator.yaml").resolve())
            self.assertEqual(resolved.runs_dir.value, str((root / "root-runs").resolve()))

    def test_env_overrides_config_values(self) -> None:
        with temp_project() as tmp:
            root = Path(tmp)
            write_text(
                root / "ai_orchestrator.yaml",
                """
                paths:
                  tasks_file: config-tasks.yaml
                  runs_dir: config-runs
                  task_drafts_dir: config-drafts
                codex:
                  cmd: config-codex
                policy:
                  policy_file: config-policy.yaml
                """,
            )
            env = {
                "AI_ORCHESTRATOR_TASKS_FILE": "env-tasks.yaml",
                "AI_ORCHESTRATOR_RUNS_DIR": "env-runs",
                "AI_ORCHESTRATOR_TASK_DRAFTS_DIR": "env-drafts",
                "AI_ORCHESTRATOR_CODEX_CMD": "env-codex",
                "CODEX_CMD": "legacy-codex",
                "AI_ORCHESTRATOR_POLICY_FILE": "env-policy.yaml",
            }

            resolved = resolve_project_config(project_root=root, env=env)

            self.assertEqual(resolved.tasks_file.value, str((root / "env-tasks.yaml").resolve()))
            self.assertEqual(resolved.tasks_file.source, "env")
            self.assertEqual(resolved.runs_dir.value, str((root / "env-runs").resolve()))
            self.assertEqual(resolved.runs_dir.source, "env")
            self.assertEqual(resolved.task_drafts_dir.value, str((root / "env-drafts").resolve()))
            self.assertEqual(resolved.task_drafts_dir.source, "env")
            self.assertEqual(resolved.codex_cmd.value, "env-codex")
            self.assertEqual(resolved.codex_cmd.source, "env")
            self.assertEqual(resolved.policy_file.value, str((root / "env-policy.yaml").resolve()))
            self.assertEqual(resolved.policy_file.source, "env")

    def test_codex_env_priority_prefers_ai_orchestrator_var(self) -> None:
        with temp_project() as tmp:
            root = Path(tmp)

            resolved = resolve_project_config(
                project_root=root,
                env={"AI_ORCHESTRATOR_CODEX_CMD": "ai-codex", "CODEX_CMD": "plain-codex"},
            )

            self.assertEqual(resolved.codex_cmd.value, "ai-codex")
            self.assertEqual(resolved.codex_cmd.source, "env")

    def test_cli_overrides_win_over_env_config_and_defaults(self) -> None:
        with temp_project() as tmp:
            root = Path(tmp)
            write_text(root / "ai_orchestrator.yaml", "paths:\n  runs_dir: config-runs\n")

            resolved = resolve_project_config(
                project_root=root,
                env={"AI_ORCHESTRATOR_RUNS_DIR": "env-runs", "AI_ORCHESTRATOR_CODEX_CMD": "env-codex"},
                cli_overrides={
                    "tasks_file": "cli-tasks.yaml",
                    "runs_dir": "cli-runs",
                    "task_drafts_dir": "cli-drafts",
                    "codex_cmd": "cli-codex",
                    "policy_file": "cli-policy.yaml",
                },
            )

            self.assertEqual(resolved.tasks_file.value, str((root / "cli-tasks.yaml").resolve()))
            self.assertEqual(resolved.tasks_file.source, "cli")
            self.assertEqual(resolved.runs_dir.value, str((root / "cli-runs").resolve()))
            self.assertEqual(resolved.runs_dir.source, "cli")
            self.assertEqual(resolved.task_drafts_dir.value, str((root / "cli-drafts").resolve()))
            self.assertEqual(resolved.task_drafts_dir.source, "cli")
            self.assertEqual(resolved.codex_cmd.value, "cli-codex")
            self.assertEqual(resolved.codex_cmd.source, "cli")
            self.assertEqual(resolved.policy_file.value, str((root / "cli-policy.yaml").resolve()))
            self.assertEqual(resolved.policy_file.source, "cli")

    def test_invalid_yaml_is_friendly_error(self) -> None:
        with temp_project() as tmp:
            root = Path(tmp)
            path = root / "bad.yaml"
            write_text(path, "paths: [")

            with self.assertRaisesRegex(ProjectConfigError, "invalid YAML"):
                resolve_project_config(project_root=root, config_path=path, env={})

    def test_top_level_non_mapping_rejected(self) -> None:
        with temp_project() as tmp:
            root = Path(tmp)
            path = root / "bad.yaml"
            write_text(path, "[]\n")

            with self.assertRaisesRegex(ProjectConfigError, "YAML mapping"):
                resolve_project_config(project_root=root, config_path=path, env={})

    def test_unknown_top_level_key_rejected(self) -> None:
        with temp_project() as tmp:
            root = Path(tmp)
            path = root / "bad.yaml"
            write_text(path, "unknown: true\n")

            with self.assertRaisesRegex(ProjectConfigError, "unknown top-level"):
                resolve_project_config(project_root=root, config_path=path, env={})

    def test_unsupported_schema_version_rejected(self) -> None:
        with temp_project() as tmp:
            root = Path(tmp)
            path = root / "bad.yaml"
            write_text(path, 'schema_version: "2.0"\n')

            with self.assertRaisesRegex(ProjectConfigError, "unsupported config schema_version"):
                resolve_project_config(project_root=root, config_path=path, env={})

    def test_invalid_review_default_profiles_type_rejected(self) -> None:
        with temp_project() as tmp:
            root = Path(tmp)
            path = root / "bad.yaml"
            write_text(path, "review:\n  default_profiles: qa\n")

            with self.assertRaisesRegex(ProjectConfigError, "default_profiles must be a list of strings"):
                resolve_project_config(project_root=root, config_path=path, env={})

    def test_invalid_path_field_type_rejected(self) -> None:
        with temp_project() as tmp:
            root = Path(tmp)
            path = root / "bad.yaml"
            write_text(path, "paths:\n  runs_dir: 123\n")

            with self.assertRaisesRegex(ProjectConfigError, "runs_dir must be a string or null"):
                resolve_project_config(project_root=root, config_path=path, env={})

    def test_show_config_cli_text_is_read_only(self) -> None:
        with temp_project() as tmp:
            root = Path(tmp)

            completed = subprocess.run(
                [sys.executable, "-m", "ai_orchestrator.cli", "show-config"],
                cwd=root,
                env=clean_cli_env(),
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("Project config", completed.stdout)
            self.assertIn("config_file: not found", completed.stdout)
            self.assertIn("tasks_file:", completed.stdout)
            self.assertFalse((root / ".runs").exists())
            self.assertFalse((root / ".web").exists())
            self.assertFalse((root / ".task_drafts").exists())
            self.assertFalse((root / "tasks.yaml").exists())

    def test_show_config_cli_json_is_valid_json(self) -> None:
        with temp_project() as tmp:
            root = Path(tmp)

            completed = subprocess.run(
                [sys.executable, "-m", "ai_orchestrator.cli", "show-config", "--format", "json"],
                cwd=root,
                env=clean_cli_env(),
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertEqual(payload["schema_version"], "1.0")
            self.assertIsNone(payload["config_file"])
            self.assertEqual(payload["values"]["tasks_file"]["source"], "default")
            self.assertEqual(payload["values"]["runs_dir"]["source"], "default")

    def test_show_config_cli_invalid_format_is_rejected(self) -> None:
        with temp_project() as tmp:
            root = Path(tmp)

            completed = subprocess.run(
                [sys.executable, "-m", "ai_orchestrator.cli", "show-config", "--format", "xml"],
                cwd=root,
                env=clean_cli_env(),
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("invalid choice", completed.stderr)

    def test_show_config_cli_invalid_yaml_is_friendly(self) -> None:
        with temp_project() as tmp:
            root = Path(tmp)
            write_text(root / "bad.yaml", "paths: [")

            completed = subprocess.run(
                [sys.executable, "-m", "ai_orchestrator.cli", "show-config", "--config", "bad.yaml"],
                cwd=root,
                env=clean_cli_env(),
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("show-config error", completed.stderr)
            self.assertIn("invalid YAML", completed.stderr)
            self.assertNotIn("Traceback", completed.stderr)


if __name__ == "__main__":
    unittest.main()
