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

from ai_orchestrator.policy import PolicyError, check_policy, load_or_default_policy, load_policy_file


REPO_ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def temp_project() -> Iterator[str]:
    root = REPO_ROOT / f"tmp_policy_{uuid.uuid4().hex}"
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


def permissive_policy_yaml() -> str:
    return """
    schema_version: "1.0"
    autonomy:
      allow_auto_task_intake: true
      allow_auto_execution: true
      allow_auto_reviewer_agents: true
      allow_auto_apply: true
      allow_auto_commit: true
    risk:
      max_auto_risk: high
      safety_sensitive_requires_human: true
    gates:
      require_human_before: []
    paths:
      forbidden: []
      require_human: []
    backends:
      allowed:
        - mock
        - codex
    review_requirements:
      medium:
        - qa
      high:
        - security
        - qa
      critical:
        - security
        - architecture
        - qa
    """


class PolicyTests(unittest.TestCase):
    def test_missing_policy_file_uses_default_policy(self) -> None:
        with temp_project() as tmp:
            loaded = load_or_default_policy(project_root=tmp)

            self.assertEqual(loaded.policy_source, "default")
            self.assertIsNone(loaded.policy_file)
            self.assertIn("policy_file_missing_using_defaults", loaded.warnings)

    def test_default_policy_disables_auto_apply_and_commit(self) -> None:
        with temp_project() as tmp:
            policy = load_or_default_policy(project_root=tmp).policy

            self.assertFalse(policy.autonomy.allow_auto_apply)
            self.assertFalse(policy.autonomy.allow_auto_commit)

    def test_default_policy_requires_human_for_review_write_and_finalization_actions(self) -> None:
        with temp_project() as tmp:
            policy = load_or_default_policy(project_root=tmp).policy

            self.assertEqual(
                policy.gates.require_human_before,
                (
                    "record-findings",
                    "record-arbitration",
                    "review-run",
                    "apply-run",
                    "accept-run",
                    "commit",
                ),
            )

    def test_default_record_findings_requires_human_gate(self) -> None:
        with temp_project() as tmp:
            loaded = load_or_default_policy(project_root=tmp)

            decision = check_policy(loaded, action="record-findings", risk_level="low")

            self.assertEqual(decision.decision, "human_gate_required")
            self.assertFalse(decision.allowed)
            self.assertIn("action_requires_human_gate", decision.reasons)

    def test_default_record_arbitration_requires_human_gate(self) -> None:
        with temp_project() as tmp:
            loaded = load_or_default_policy(project_root=tmp)

            decision = check_policy(loaded, action="record-arbitration", risk_level="low")

            self.assertEqual(decision.decision, "human_gate_required")
            self.assertFalse(decision.allowed)
            self.assertIn("action_requires_human_gate", decision.reasons)

    def test_default_review_run_requires_human_gate(self) -> None:
        with temp_project() as tmp:
            loaded = load_or_default_policy(project_root=tmp)

            decision = check_policy(loaded, action="review-run", risk_level="low")

            self.assertEqual(decision.decision, "human_gate_required")
            self.assertFalse(decision.allowed)
            self.assertIn("action_requires_human_gate", decision.reasons)

    def test_default_apply_run_requires_human_gate(self) -> None:
        with temp_project() as tmp:
            loaded = load_or_default_policy(project_root=tmp)

            decision = check_policy(loaded, action="apply-run", risk_level="low")

            self.assertEqual(decision.decision, "human_gate_required")
            self.assertFalse(decision.allowed)
            self.assertIn("action_requires_human_gate", decision.reasons)
            self.assertIn("auto_apply_disabled", decision.reasons)

    def test_default_commit_is_not_allowed(self) -> None:
        with temp_project() as tmp:
            loaded = load_or_default_policy(project_root=tmp)

            decision = check_policy(loaded, action="commit", risk_level="docs_only")

            self.assertEqual(decision.decision, "human_gate_required")
            self.assertFalse(decision.allowed)
            self.assertIn("auto_commit_disabled", decision.reasons)

    def test_explicit_policy_loads_yaml_file(self) -> None:
        with temp_project() as tmp:
            root = Path(tmp)
            policy_path = root / "custom_policy.yaml"
            write_text(policy_path, permissive_policy_yaml())

            loaded = load_or_default_policy(project_root=root, policy_path=policy_path)

            self.assertEqual(loaded.policy_source, "file")
            self.assertEqual(loaded.policy_file, policy_path.resolve())
            self.assertTrue(loaded.policy.autonomy.allow_auto_execution)

    def test_policy_path_from_config_resolver_works(self) -> None:
        with temp_project() as tmp:
            root = Path(tmp)
            write_text(root / "ai_orchestrator.yaml", "policy:\n  policy_file: config/policy.yaml\n")
            write_text(root / "config" / "policy.yaml", permissive_policy_yaml())

            loaded = load_or_default_policy(project_root=root)

            self.assertEqual(loaded.policy_source, "file")
            self.assertEqual(loaded.policy_file, (root / "config" / "policy.yaml").resolve())

    def test_explicit_policy_wins_over_config_policy_path(self) -> None:
        with temp_project() as tmp:
            root = Path(tmp)
            write_text(root / "ai_orchestrator.yaml", "policy:\n  policy_file: config/policy.yaml\n")
            write_text(root / "config" / "policy.yaml", permissive_policy_yaml().replace("max_auto_risk: high", "max_auto_risk: medium"))
            explicit = root / "explicit.yaml"
            write_text(explicit, permissive_policy_yaml())

            loaded = load_or_default_policy(project_root=root, policy_path=explicit)

            self.assertEqual(loaded.policy_file, explicit.resolve())
            self.assertEqual(loaded.policy.risk.max_auto_risk, "high")

    def test_invalid_yaml_rejected(self) -> None:
        with temp_project() as tmp:
            path = Path(tmp) / "bad.yaml"
            write_text(path, "autonomy: [")

            with self.assertRaisesRegex(PolicyError, "invalid YAML"):
                load_policy_file(path)

    def test_top_level_non_mapping_rejected(self) -> None:
        with temp_project() as tmp:
            path = Path(tmp) / "bad.yaml"
            write_text(path, "[]")

            with self.assertRaisesRegex(PolicyError, "YAML mapping"):
                load_policy_file(path)

    def test_unknown_top_level_key_rejected(self) -> None:
        with temp_project() as tmp:
            path = Path(tmp) / "bad.yaml"
            write_text(path, "credentials:\n  token: secret\n")

            with self.assertRaisesRegex(PolicyError, "unknown top-level"):
                load_policy_file(path)

    def test_unsupported_schema_version_rejected(self) -> None:
        with temp_project() as tmp:
            path = Path(tmp) / "bad.yaml"
            write_text(path, 'schema_version: "2.0"\n')

            with self.assertRaisesRegex(PolicyError, "unsupported policy schema_version"):
                load_policy_file(path)

    def test_invalid_boolean_field_rejected(self) -> None:
        with temp_project() as tmp:
            path = Path(tmp) / "bad.yaml"
            write_text(path, 'autonomy:\n  allow_auto_apply: "yes"\n')

            with self.assertRaisesRegex(PolicyError, "allow_auto_apply must be a boolean"):
                load_policy_file(path)

    def test_invalid_risk_level_rejected(self) -> None:
        with temp_project() as tmp:
            path = Path(tmp) / "bad.yaml"
            write_text(path, "risk:\n  max_auto_risk: impossible\n")

            with self.assertRaisesRegex(PolicyError, "max_auto_risk"):
                load_policy_file(path)

    def test_invalid_list_field_rejected(self) -> None:
        with temp_project() as tmp:
            path = Path(tmp) / "bad.yaml"
            write_text(path, "paths:\n  forbidden: src/**\n")

            with self.assertRaisesRegex(PolicyError, "forbidden must be a list of strings"):
                load_policy_file(path)

    def test_risk_above_max_auto_risk_requires_human(self) -> None:
        with temp_project() as tmp:
            root = Path(tmp)
            path = root / "policy.yaml"
            write_text(path, permissive_policy_yaml().replace("max_auto_risk: high", "max_auto_risk: low"))
            loaded = load_or_default_policy(project_root=root, policy_path=path)

            decision = check_policy(loaded, action="record-findings", risk_level="medium")

            self.assertEqual(decision.decision, "human_gate_required")
            self.assertIn("risk_exceeds_max_auto_risk", decision.reasons)

    def test_safety_sensitive_requires_human(self) -> None:
        with temp_project() as tmp:
            root = Path(tmp)
            path = root / "policy.yaml"
            write_text(path, permissive_policy_yaml())
            loaded = load_or_default_policy(project_root=root, policy_path=path)

            decision = check_policy(loaded, action="record-findings", risk_level="safety_sensitive")

            self.assertEqual(decision.decision, "human_gate_required")
            self.assertIn("safety_sensitive_requires_human", decision.reasons)

    def test_changed_file_matching_forbidden_path_blocks(self) -> None:
        with temp_project() as tmp:
            root = Path(tmp)
            path = root / "policy.yaml"
            write_text(path, permissive_policy_yaml().replace("forbidden: []", "forbidden:\n      - src/ai_orchestrator/apply.py"))
            loaded = load_or_default_policy(project_root=root, policy_path=path)

            decision = check_policy(loaded, action="record-findings", risk_level="low", changed_files=["src/ai_orchestrator/apply.py"])

            self.assertEqual(decision.decision, "blocked")
            self.assertIn("path_forbidden", decision.reasons)

    def test_changed_file_matching_require_human_path_requires_human(self) -> None:
        with temp_project() as tmp:
            root = Path(tmp)
            path = root / "policy.yaml"
            write_text(path, permissive_policy_yaml().replace("require_human: []", "require_human:\n      - src/ai_orchestrator_web/routes/**"))
            loaded = load_or_default_policy(project_root=root, policy_path=path)

            decision = check_policy(loaded, action="record-findings", risk_level="low", changed_files=["src/ai_orchestrator_web/routes/runs.py"])

            self.assertEqual(decision.decision, "human_gate_required")
            self.assertIn("path_requires_human", decision.reasons)

    def test_backend_not_allowed_blocks(self) -> None:
        with temp_project() as tmp:
            root = Path(tmp)
            path = root / "policy.yaml"
            write_text(path, permissive_policy_yaml().replace("- codex", ""))
            loaded = load_or_default_policy(project_root=root, policy_path=path)

            decision = check_policy(loaded, action="run-pipeline", risk_level="low", backend="codex")

            self.assertEqual(decision.decision, "blocked")
            self.assertIn("backend_not_allowed", decision.reasons)

    def test_blocked_wins_over_human_gate_required(self) -> None:
        with temp_project() as tmp:
            root = Path(tmp)
            path = root / "policy.yaml"
            policy = permissive_policy_yaml()
            policy = policy.replace("forbidden: []", "forbidden:\n      - src/secret.py")
            policy = policy.replace("require_human: []", "require_human:\n      - src/**")
            write_text(path, policy)
            loaded = load_or_default_policy(project_root=root, policy_path=path)

            decision = check_policy(loaded, action="record-findings", risk_level="low", changed_files=["src/secret.py"])

            self.assertEqual(decision.decision, "blocked")
            self.assertIn("path_forbidden", decision.reasons)
            self.assertIn("path_requires_human", decision.reasons)

    def test_changed_file_absolute_or_path_traversal_rejected(self) -> None:
        with temp_project() as tmp:
            loaded = load_or_default_policy(project_root=tmp)

            with self.assertRaisesRegex(PolicyError, "project-relative"):
                check_policy(loaded, action="record-findings", risk_level="low", changed_files=["/etc/passwd"])
            with self.assertRaisesRegex(PolicyError, "path traversal"):
                check_policy(loaded, action="record-findings", risk_level="low", changed_files=["../secret.py"])
            with self.assertRaisesRegex(PolicyError, "POSIX"):
                check_policy(loaded, action="record-findings", risk_level="low", changed_files=[r"src\secret.py"])

    def test_unknown_action_rejected(self) -> None:
        with temp_project() as tmp:
            loaded = load_or_default_policy(project_root=tmp)

            with self.assertRaisesRegex(PolicyError, "unknown policy action"):
                check_policy(loaded, action="deploy", risk_level="low")

    def test_unknown_risk_level_rejected(self) -> None:
        with temp_project() as tmp:
            loaded = load_or_default_policy(project_root=tmp)

            with self.assertRaisesRegex(PolicyError, "unknown risk level"):
                check_policy(loaded, action="record-findings", risk_level="extreme")

    def test_check_policy_cli_exits_zero_for_valid_blocked_decision(self) -> None:
        with temp_project() as tmp:
            root = Path(tmp)

            completed = subprocess.run(
                [sys.executable, "-m", "ai_orchestrator.cli", "check-policy", "--action", "apply-run", "--risk-level", "low"],
                cwd=root,
                env=clean_cli_env(),
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("decision: human_gate_required", completed.stdout)
            self.assertIn("allowed: false", completed.stdout)

    def test_show_policy_json_returns_valid_json(self) -> None:
        with temp_project() as tmp:
            completed = subprocess.run(
                [sys.executable, "-m", "ai_orchestrator.cli", "show-policy", "--format", "json"],
                cwd=tmp,
                env=clean_cli_env(),
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertEqual(payload["schema_version"], "1.0")
            self.assertEqual(payload["policy_source"], "default")
            self.assertFalse(payload["autonomy"]["allow_auto_apply"])

    def test_check_policy_json_returns_decision_and_reasons(self) -> None:
        with temp_project() as tmp:
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "ai_orchestrator.cli",
                    "check-policy",
                    "--action",
                    "commit",
                    "--risk-level",
                    "docs_only",
                    "--format",
                    "json",
                ],
                cwd=tmp,
                env=clean_cli_env(),
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertEqual(payload["decision"], "human_gate_required")
            self.assertFalse(payload["allowed"])
            self.assertIn("auto_commit_disabled", payload["reasons"])

    def test_show_and_check_policy_do_not_create_runtime_artifacts(self) -> None:
        with temp_project() as tmp:
            root = Path(tmp)
            for command in (
                [sys.executable, "-m", "ai_orchestrator.cli", "show-policy"],
                [sys.executable, "-m", "ai_orchestrator.cli", "check-policy", "--action", "apply-run", "--risk-level", "low"],
            ):
                completed = subprocess.run(command, cwd=root, env=clean_cli_env(), text=True, capture_output=True, check=False)
                self.assertEqual(completed.returncode, 0, completed.stderr)

            self.assertFalse((root / ".runs").exists())
            self.assertFalse((root / ".web").exists())
            self.assertFalse((root / ".task_drafts").exists())
            self.assertFalse((root / "tasks.yaml").exists())
            self.assertFalse((root / "autonomy_policy.yaml").exists())


if __name__ == "__main__":
    unittest.main()
