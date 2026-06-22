from __future__ import annotations

from contextlib import contextmanager, redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
import json
import shutil
import sys
import textwrap
import unittest
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_orchestrator.cli import (
    classify_run_main,
    prepare_review_main,
    run_review_checks_main,
    show_pipeline_main,
    show_run_main,
)
from ai_orchestrator.pipeline import PipelineSelectedTask, PipelineState, PipelineTaskResult
from ai_orchestrator.risk_classification import build_risk_classification_markdown, classify_changed_files, load_run_risk_classification
from ai_orchestrator.schemas import ExecutionResult, RunState, TaskSpec, ValidationResult

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


def output_value(output: str, key: str) -> str:
    prefix = f"{key}="
    for line in output.splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :].strip()
    raise AssertionError(f"missing output line for {key!r} in:\n{output}")


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")


def make_run_fixture(
    root: Path,
    *,
    run_id: str = "run_test_risk_classification",
    changed_files: list[str],
    workspace_files: dict[str, str] | None = None,
    status: str = "approved",
    description: str = "Synthetic risk classification fixture",
) -> tuple[Path, Path]:
    runs_dir = root / ".runs"
    run_dir = runs_dir / run_id
    workspace = run_dir / "artifacts" / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    for relative_path, content in (workspace_files or {}).items():
        write_text(workspace / relative_path, content)

    report = {
        "schema_version": "1.0",
        "status": "completed",
        "summary": "Synthetic risk classification fixture.",
        "changed_files": changed_files,
        "commands_run": [
            {
                "command": "python -m unittest discover -s tests",
                "exit_code": 0,
                "status": "passed",
                "summary": "ok",
            }
        ],
        "tests": [
            {
                "name": "tests",
                "command": "python -m unittest discover -s tests",
                "status": "passed",
                "total": 1,
                "passed": 1,
                "failed": 0,
                "output": "OK",
            }
        ],
        "risks": [],
        "assumptions": [],
        "validation_notes": [],
    }
    report_path = workspace / "EXECUTION_REPORT.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    state = RunState(
        run_id=run_id,
        task=TaskSpec(description=description),
        backend_name="mock",
        final_status=status,
    )
    state.executions.append(
        ExecutionResult(
            step_id="step_1",
            attempt=1,
            status="completed",
            content="\n".join(["# log", "### EXECUTION_REPORT.json", json.dumps(report)]),
            artifact_paths=[str(report_path), *[str(workspace / path) for path in (workspace_files or {})]],
        )
    )
    state.validations.append(
        ValidationResult(step_id="step_1", attempt=1, approved=status == "approved", score=1.0, feedback=["ok"])
    )
    state.save_json(run_dir / "state.json")
    write_text(run_dir / "final_report.md", "# Final report\n")
    write_text(run_dir / "REVIEW_PACKET.md", "# Review packet\n")
    return run_dir, runs_dir


def create_pipeline_fixture(
    root: Path,
    *,
    pipeline_id: str,
    tasks: list[PipelineTaskResult],
    status: str = "approved",
) -> None:
    pipeline_dir = root / ".runs" / "pipelines" / pipeline_id
    pipeline_dir.mkdir(parents=True, exist_ok=True)
    state = PipelineState(
        pipeline_id=pipeline_id,
        tasks_file=str((root / "tasks.yaml").resolve()),
        status=status,
        selected_tasks=[
            PipelineSelectedTask(task_id=task.task_id, title=task.title, enabled=True)
            for task in tasks
        ],
        tasks=tasks,
    )
    state.save_json(pipeline_dir / "pipeline_state.json")
    write_text(pipeline_dir / "PIPELINE_REPORT.md", f"# Pipeline Report: {pipeline_id}\n")


def build_pipeline_task_result(task_id: str, run_id: str, runs_dir: Path, *, title: str | None = None) -> PipelineTaskResult:
    run_dir = runs_dir / run_id
    return PipelineTaskResult(
        task_id=task_id,
        title=title,
        status="approved",
        run_id=run_id,
        final_report=str((run_dir / "final_report.md").resolve()),
        review_packet=str((run_dir / "REVIEW_PACKET.md").resolve()),
        state=str((run_dir / "state.json").resolve()),
    )


class RiskClassificationRuleTests(unittest.TestCase):
    def test_docs_only_is_low_with_no_required_profiles(self) -> None:
        classification = classify_changed_files(
            run_id="run_1",
            changed_files=["docs/guide.md", "README.md", "EXECUTION_REPORT.json"],
        )
        self.assertEqual(classification.risk_level, "low")
        self.assertEqual(classification.change_type, "docs_only")
        self.assertEqual(classification.required_review_profiles, [])
        self.assertEqual(classification.optional_review_profiles, ["business", "qa"])
        self.assertIn("docs_only_change", classification.reasons)

    def test_tests_only_is_low_and_requires_qa(self) -> None:
        classification = classify_changed_files(
            run_id="run_1",
            changed_files=["tests/test_demo.py", "EXECUTION_REPORT.json"],
        )
        self.assertEqual(classification.risk_level, "low")
        self.assertEqual(classification.change_type, "tests_only")
        self.assertEqual(classification.required_review_profiles, ["qa"])
        self.assertIn("tests_only_change", classification.reasons)

    def test_source_without_tests_is_high_and_requires_qa_architecture(self) -> None:
        classification = classify_changed_files(
            run_id="run_1",
            changed_files=["src/demo.py", "EXECUTION_REPORT.json"],
        )
        self.assertEqual(classification.risk_level, "high")
        self.assertEqual(classification.change_type, "source_code")
        self.assertEqual(classification.required_review_profiles, ["qa", "architecture", "maintainability"])
        self.assertIn("source_code_change", classification.reasons)
        self.assertIn("missing_tests_for_code_change", classification.reasons)

    def test_source_with_tests_is_medium_and_requires_qa_architecture(self) -> None:
        classification = classify_changed_files(
            run_id="run_1",
            changed_files=["src/demo.py", "tests/test_demo.py", "EXECUTION_REPORT.json"],
        )
        self.assertEqual(classification.risk_level, "medium")
        self.assertEqual(classification.change_type, "source_and_tests")
        self.assertEqual(classification.required_review_profiles, ["qa", "architecture", "maintainability"])
        self.assertIn("source_code_change", classification.reasons)
        self.assertNotIn("missing_tests_for_code_change", classification.reasons)

    def test_safety_critical_file_is_critical_and_requires_maintainability_too(self) -> None:
        classification = classify_changed_files(
            run_id="run_1",
            changed_files=["src/ai_orchestrator/apply.py", "EXECUTION_REPORT.json"],
        )
        self.assertEqual(classification.risk_level, "critical")
        self.assertEqual(classification.change_type, "safety_critical")
        self.assertEqual(
            classification.required_review_profiles,
            ["security", "architecture", "qa", "ops", "maintainability"],
        )

    def test_data_logic_file_is_high_and_requires_data_and_qa(self) -> None:
        classification = classify_changed_files(
            run_id="run_1",
            changed_files=["src/pipeline/data_loader.py", "EXECUTION_REPORT.json"],
        )
        self.assertEqual(classification.risk_level, "high")
        self.assertEqual(classification.change_type, "data_logic")
        self.assertEqual(classification.required_review_profiles, ["data", "qa", "maintainability"])

    def test_broad_change_over_ten_is_high(self) -> None:
        changed_files = [f"docs/file_{i}.md" for i in range(11)] + ["EXECUTION_REPORT.json"]
        classification = classify_changed_files(run_id="run_1", changed_files=changed_files)
        self.assertEqual(classification.risk_level, "high")
        self.assertIn("architecture", classification.required_review_profiles)
        self.assertIn("qa", classification.required_review_profiles)
        self.assertIn("maintainability", classification.required_review_profiles)

    def test_broad_change_over_twenty_is_critical(self) -> None:
        changed_files = [f"docs/file_{i}.md" for i in range(21)] + ["EXECUTION_REPORT.json"]
        classification = classify_changed_files(run_id="run_1", changed_files=changed_files)
        self.assertEqual(classification.risk_level, "critical")
        self.assertIn("ops", classification.required_review_profiles)
        self.assertIn("maintainability", classification.required_review_profiles)

    def test_unsafe_path_is_critical_and_requires_security_ops(self) -> None:
        classification = classify_changed_files(
            run_id="run_1",
            changed_files=["../escape.py", "EXECUTION_REPORT.json"],
        )
        self.assertEqual(classification.risk_level, "critical")
        self.assertIn("security", classification.required_review_profiles)
        self.assertIn("ops", classification.required_review_profiles)

    def test_mixed_changes_pick_highest_risk_and_union_profiles(self) -> None:
        classification = classify_changed_files(
            run_id="run_1",
            changed_files=["docs/guide.md", "src/demo.py", "tests/test_demo.py", "EXECUTION_REPORT.json"],
        )
        self.assertEqual(classification.risk_level, "medium")
        self.assertEqual(classification.change_type, "mixed")
        self.assertEqual(classification.required_review_profiles, ["qa", "architecture", "maintainability"])

    def test_maintainability_sensitive_task_intake_module_requires_maintainability_architecture_and_qa(self) -> None:
        classification = classify_changed_files(
            run_id="run_1",
            changed_files=["src/ai_orchestrator/task_drafts.py", "EXECUTION_REPORT.json"],
        )
        self.assertEqual(classification.risk_level, "high")
        self.assertEqual(
            classification.required_review_profiles,
            ["architecture", "qa", "maintainability"],
        )

    def test_docs_only_change_does_not_require_maintainability(self) -> None:
        classification = classify_changed_files(
            run_id="run_1",
            changed_files=["docs/guide.md", "README.md", "EXECUTION_REPORT.json"],
        )
        self.assertNotIn("maintainability", classification.required_review_profiles)

    def test_small_source_and_tests_change_requires_maintainability(self) -> None:
        classification = classify_changed_files(
            run_id="run_1",
            changed_files=["src/demo.py", "tests/test_demo.py", "EXECUTION_REPORT.json"],
        )
        self.assertIn("maintainability", classification.required_review_profiles)

    def test_web_route_change_is_medium_with_reason(self) -> None:
        classification = classify_changed_files(
            run_id="run_1",
            changed_files=["src/ai_orchestrator_web/routes/runs.py", "EXECUTION_REPORT.json"],
        )
        self.assertEqual(classification.risk_level, "high")
        self.assertIn("web_route_change", classification.reasons)
        self.assertIn("qa", classification.required_review_profiles)
        self.assertIn("maintainability", classification.required_review_profiles)

    def test_web_job_action_change_is_high_with_subprocess_reason(self) -> None:
        classification = classify_changed_files(
            run_id="run_1",
            changed_files=["src/ai_orchestrator_web/jobs/actions.py", "EXECUTION_REPORT.json"],
        )
        self.assertEqual(classification.risk_level, "high")
        self.assertIn("web_job_action_change", classification.reasons)
        self.assertIn("subprocess_command_construction_change", classification.reasons)
        self.assertIn("security", classification.required_review_profiles)
        self.assertIn("qa", classification.required_review_profiles)
        self.assertIn("maintainability", classification.required_review_profiles)

    def test_job_runner_change_is_high_with_subprocess_reason(self) -> None:
        classification = classify_changed_files(
            run_id="run_1",
            changed_files=["src/ai_orchestrator_web/jobs/runner.py", "EXECUTION_REPORT.json"],
        )
        self.assertEqual(classification.risk_level, "high")
        self.assertIn("job_runner_change", classification.reasons)
        self.assertIn("subprocess_command_construction_change", classification.reasons)

    def test_apply_logic_change_has_apply_reason_and_security_profile(self) -> None:
        classification = classify_changed_files(
            run_id="run_1",
            changed_files=["src/ai_orchestrator/apply.py", "EXECUTION_REPORT.json"],
        )
        self.assertIn(classification.risk_level, {"high", "critical"})
        self.assertIn("apply_logic_change", classification.reasons)
        self.assertIn("security", classification.required_review_profiles)

    def test_review_decision_logic_change_has_reason(self) -> None:
        classification = classify_changed_files(
            run_id="run_1",
            changed_files=["src/ai_orchestrator/review_decision.py", "EXECUTION_REPORT.json"],
        )
        self.assertIn(classification.risk_level, {"high", "critical"})
        self.assertIn("review_decision_logic_change", classification.reasons)

    def test_policy_logic_change_is_high_with_security_profile(self) -> None:
        classification = classify_changed_files(
            run_id="run_1",
            changed_files=["src/ai_orchestrator/policy.py", "EXECUTION_REPORT.json"],
        )
        self.assertEqual(classification.risk_level, "high")
        self.assertIn("policy_logic_change", classification.reasons)
        self.assertIn("security", classification.required_review_profiles)

    def test_ci_workflow_change_is_high_with_security_ops_profiles(self) -> None:
        classification = classify_changed_files(
            run_id="run_1",
            changed_files=[".github/workflows/ci.yml", "EXECUTION_REPORT.json"],
        )
        self.assertEqual(classification.risk_level, "high")
        self.assertIn("ci_workflow_change", classification.reasons)
        self.assertIn("security", classification.required_review_profiles)
        self.assertIn("ops", classification.required_review_profiles)

    def test_dependency_manifest_change_is_medium_with_security_profile(self) -> None:
        classification = classify_changed_files(
            run_id="run_1",
            changed_files=["pyproject.toml", "EXECUTION_REPORT.json"],
        )
        self.assertEqual(classification.risk_level, "medium")
        self.assertIn("dependency_manifest_change", classification.reasons)
        self.assertIn("security", classification.required_review_profiles)

    def test_large_change_set_adds_reason_code(self) -> None:
        changed_files = [f"docs/file_{i}.md" for i in range(16)] + ["EXECUTION_REPORT.json"]
        classification = classify_changed_files(run_id="run_1", changed_files=changed_files)
        self.assertIn("large_change_set", classification.reasons)

    def test_mixed_docs_and_web_job_action_is_high_not_docs_only(self) -> None:
        classification = classify_changed_files(
            run_id="run_1",
            changed_files=["README.md", "src/ai_orchestrator_web/jobs/actions.py", "EXECUTION_REPORT.json"],
        )
        self.assertEqual(classification.risk_level, "high")
        self.assertNotEqual(classification.change_type, "docs_only")
        self.assertIn("web_job_action_change", classification.reasons)
        self.assertIn("mixed_docs_and_code", classification.reasons)

    def test_reason_codes_and_profiles_are_unique_and_deterministic(self) -> None:
        classification = classify_changed_files(
            run_id="run_1",
            changed_files=[
                "src/ai_orchestrator_web/jobs/actions.py",
                "src/ai_orchestrator_web/jobs/actions.py",
                "src/ai_orchestrator_web/jobs/runner.py",
                "EXECUTION_REPORT.json",
            ],
        )
        self.assertEqual(classification.reasons, list(dict.fromkeys(classification.reasons)))
        self.assertEqual(classification.required_review_profiles, list(dict.fromkeys(classification.required_review_profiles)))

    def test_markdown_report_includes_reason_codes(self) -> None:
        classification = classify_changed_files(
            run_id="run_1",
            changed_files=["src/ai_orchestrator_web/jobs/actions.py", "EXECUTION_REPORT.json"],
        )
        markdown = build_risk_classification_markdown(classification)
        self.assertIn("## Reason codes", markdown)
        self.assertIn("web_job_action_change", markdown)
        self.assertIn("subprocess_command_construction_change", markdown)


class ClassifyRunCommandTests(unittest.TestCase):
    def test_classify_run_writes_json_markdown_and_updates_state(self) -> None:
        with temporary_test_dir() as tmp:
            run_dir, runs_dir = make_run_fixture(
                tmp,
                changed_files=["src/demo.py", "EXECUTION_REPORT.json"],
                workspace_files={"src/demo.py": "VALUE = 1\n"},
            )
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = classify_run_main([run_dir.name, "--runs-dir", str(runs_dir)])
            output = stdout.getvalue()
            state = RunState.model_validate_json((run_dir / "state.json").read_text(encoding="utf-8"))
            classification = load_run_risk_classification(run_dir)
            classification_payload = json.loads((run_dir / "RISK_CLASSIFICATION.json").read_text(encoding="utf-8"))
            classification_markdown = (run_dir / "RISK_CLASSIFICATION.md").read_text(encoding="utf-8")
            classification_json_exists = (run_dir / "RISK_CLASSIFICATION.json").exists()
            classification_md_exists = (run_dir / "RISK_CLASSIFICATION.md").exists()

        self.assertEqual(exit_code, 0, output)
        self.assertEqual(output_value(output, "status"), "risk_classified")
        self.assertTrue(classification_json_exists)
        self.assertTrue(classification_md_exists)
        self.assertEqual(state.risk_level, "high")
        self.assertEqual(state.change_type, "source_code")
        self.assertEqual(state.required_review_profiles, ["qa", "architecture", "maintainability"])
        self.assertIsNotNone(classification)
        self.assertEqual(classification.risk_level, "high")
        self.assertIn("source_code_change", classification.reasons)
        self.assertIn("reasons", classification_payload)
        self.assertIn("source_code_change", classification_payload["reasons"])
        self.assertIn("required_review_profiles", classification_payload)
        self.assertIn("source_code_change", classification_markdown)
        self.assertEqual(output_value(output, "next_action"), "prepare_required_reviews")

    def test_classify_run_next_action_run_review_checks_for_docs_only_without_required_profiles(self) -> None:
        with temporary_test_dir() as tmp:
            run_dir, runs_dir = make_run_fixture(
                tmp,
                changed_files=["docs/guide.md", "EXECUTION_REPORT.json"],
                workspace_files={"docs/guide.md": "# Guide\n"},
            )
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = classify_run_main([run_dir.name, "--runs-dir", str(runs_dir)])
            output = stdout.getvalue()

        self.assertEqual(exit_code, 0, output)
        self.assertEqual(output_value(output, "required_review_profiles"), "")
        self.assertEqual(output_value(output, "next_action"), "run_review_checks")

    def test_classify_run_refuses_overwrite_without_force(self) -> None:
        with temporary_test_dir() as tmp:
            run_dir, runs_dir = make_run_fixture(
                tmp,
                changed_files=["docs/guide.md", "EXECUTION_REPORT.json"],
                workspace_files={"docs/guide.md": "# Guide\n"},
            )
            with redirect_stdout(StringIO()):
                self.assertEqual(classify_run_main([run_dir.name, "--runs-dir", str(runs_dir)]), 0)
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = classify_run_main([run_dir.name, "--runs-dir", str(runs_dir)])
            output = stdout.getvalue()

        self.assertEqual(exit_code, 1, output)
        self.assertIn("Risk classification already recorded. Pass --force to overwrite it.", output)

    def test_classify_run_overwrites_with_force(self) -> None:
        with temporary_test_dir() as tmp:
            run_dir, runs_dir = make_run_fixture(
                tmp,
                changed_files=["docs/guide.md", "EXECUTION_REPORT.json"],
                workspace_files={"docs/guide.md": "# Guide\n"},
            )
            with redirect_stdout(StringIO()):
                self.assertEqual(classify_run_main([run_dir.name, "--runs-dir", str(runs_dir)]), 0)
            payload = json.loads((run_dir / "artifacts" / "workspace" / "EXECUTION_REPORT.json").read_text(encoding="utf-8"))
            payload["changed_files"] = ["src/demo.py", "EXECUTION_REPORT.json"]
            (run_dir / "artifacts" / "workspace" / "src").mkdir(parents=True, exist_ok=True)
            write_text(run_dir / "artifacts" / "workspace" / "src" / "demo.py", "VALUE = 1\n")
            (run_dir / "artifacts" / "workspace" / "EXECUTION_REPORT.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
            state = RunState.model_validate_json((run_dir / "state.json").read_text(encoding="utf-8"))
            state.executions[-1].artifact_paths.append(str(run_dir / "artifacts" / "workspace" / "src" / "demo.py"))
            state.save_json(run_dir / "state.json")
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = classify_run_main([run_dir.name, "--runs-dir", str(runs_dir), "--force"])
            output = stdout.getvalue()
            classification = load_run_risk_classification(run_dir)

        self.assertEqual(exit_code, 0, output)
        self.assertIsNotNone(classification)
        self.assertEqual(classification.risk_level, "high")

    def test_classify_run_json_output_is_valid(self) -> None:
        with temporary_test_dir() as tmp:
            run_dir, runs_dir = make_run_fixture(
                tmp,
                changed_files=["docs/guide.md", "EXECUTION_REPORT.json"],
                workspace_files={"docs/guide.md": "# Guide\n"},
            )
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = classify_run_main([run_dir.name, "--runs-dir", str(runs_dir), "--format", "json"])
            payload = json.loads(stdout.getvalue())

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "risk_classified")
        self.assertEqual(payload["risk_level"], "low")
        self.assertEqual(payload["next_action"], "run_review_checks")


class PrepareReviewRequiredProfilesTests(unittest.TestCase):
    def test_prepare_review_required_profiles_fails_if_classification_missing(self) -> None:
        with temporary_test_dir() as tmp:
            run_dir, runs_dir = make_run_fixture(
                tmp,
                changed_files=["src/demo.py", "EXECUTION_REPORT.json"],
                workspace_files={"src/demo.py": "VALUE = 1\n"},
            )
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = prepare_review_main([run_dir.name, "--runs-dir", str(runs_dir), "--required-profiles"])
            output = stdout.getvalue()

        self.assertEqual(exit_code, 1, output)
        self.assertIn("risk classification not found; run classify-run first", output)

    def test_prepare_review_required_profiles_creates_prompts(self) -> None:
        with temporary_test_dir() as tmp:
            run_dir, runs_dir = make_run_fixture(
                tmp,
                changed_files=["src/ai_orchestrator/apply.py", "EXECUTION_REPORT.json"],
                workspace_files={"src/ai_orchestrator/apply.py": "VALUE = 1\n"},
            )
            with redirect_stdout(StringIO()):
                self.assertEqual(classify_run_main([run_dir.name, "--runs-dir", str(runs_dir)]), 0)
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = prepare_review_main([run_dir.name, "--runs-dir", str(runs_dir), "--required-profiles"])
            output = stdout.getvalue()
            prompts_dir = run_dir / "reviewer_prompts"
            security_prompt_exists = (prompts_dir / "security_review_prompt.md").exists()
            architecture_prompt_exists = (prompts_dir / "architecture_review_prompt.md").exists()
            qa_prompt_exists = (prompts_dir / "qa_review_prompt.md").exists()
            ops_prompt_exists = (prompts_dir / "ops_review_prompt.md").exists()
            maintainability_prompt_exists = (prompts_dir / "maintainability_review_prompt.md").exists()

        self.assertEqual(exit_code, 0, output)
        self.assertTrue(security_prompt_exists)
        self.assertTrue(architecture_prompt_exists)
        self.assertTrue(qa_prompt_exists)
        self.assertTrue(ops_prompt_exists)
        self.assertTrue(maintainability_prompt_exists)

    def test_prepare_review_required_profiles_with_no_required_profiles_succeeds_as_noop(self) -> None:
        with temporary_test_dir() as tmp:
            run_dir, runs_dir = make_run_fixture(
                tmp,
                changed_files=["docs/guide.md", "EXECUTION_REPORT.json"],
                workspace_files={"docs/guide.md": "# Guide\n"},
            )
            with redirect_stdout(StringIO()):
                self.assertEqual(classify_run_main([run_dir.name, "--runs-dir", str(runs_dir)]), 0)
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = prepare_review_main([run_dir.name, "--runs-dir", str(runs_dir), "--required-profiles"])
            output = stdout.getvalue()

        self.assertEqual(exit_code, 0, output)
        self.assertEqual(output_value(output, "profiles"), "")
        self.assertEqual(output_value(output, "message"), "no required review profiles for this run")

    def test_required_profiles_mutually_exclusive_with_profile(self) -> None:
        with temporary_test_dir() as tmp:
            run_dir, runs_dir = make_run_fixture(
                tmp,
                changed_files=["docs/guide.md", "EXECUTION_REPORT.json"],
                workspace_files={"docs/guide.md": "# Guide\n"},
            )
            stderr = StringIO()
            with redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as ctx:
                    prepare_review_main(
                        [run_dir.name, "--runs-dir", str(runs_dir), "--required-profiles", "--profile", "qa"]
                    )
        self.assertEqual(ctx.exception.code, 2)

    def test_required_profiles_mutually_exclusive_with_all_profiles(self) -> None:
        with temporary_test_dir() as tmp:
            run_dir, runs_dir = make_run_fixture(
                tmp,
                changed_files=["docs/guide.md", "EXECUTION_REPORT.json"],
                workspace_files={"docs/guide.md": "# Guide\n"},
            )
            stderr = StringIO()
            with redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as ctx:
                    prepare_review_main(
                        [run_dir.name, "--runs-dir", str(runs_dir), "--required-profiles", "--all-profiles"]
                    )
        self.assertEqual(ctx.exception.code, 2)


class RiskShowStatusTests(unittest.TestCase):
    def test_show_run_displays_risk_fields_after_classification(self) -> None:
        with temporary_test_dir() as tmp:
            run_dir, runs_dir = make_run_fixture(
                tmp,
                changed_files=["src/demo.py", "EXECUTION_REPORT.json"],
                workspace_files={"src/demo.py": "VALUE = 1\n"},
            )
            with redirect_stdout(StringIO()):
                self.assertEqual(classify_run_main([run_dir.name, "--runs-dir", str(runs_dir)]), 0)
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = show_run_main([run_dir.name, "--runs-dir", str(runs_dir), "--show-paths"])
            output = stdout.getvalue()

        self.assertEqual(exit_code, 0, output)
        self.assertEqual(output_value(output, "risk_classification_exists"), "true")
        self.assertEqual(output_value(output, "risk_level"), "high")
        self.assertEqual(output_value(output, "change_type"), "source_code")
        self.assertEqual(output_value(output, "required_review_profiles"), "qa,architecture,maintainability")
        self.assertIn("risk_classification=", output)
        self.assertIn("risk_classification_markdown=", output)

    def test_show_run_next_action_is_classify_run_when_classification_missing(self) -> None:
        with temporary_test_dir() as tmp:
            run_dir, runs_dir = make_run_fixture(
                tmp,
                changed_files=["docs/guide.md", "EXECUTION_REPORT.json"],
                workspace_files={"docs/guide.md": "# Guide\n"},
            )
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = show_run_main([run_dir.name, "--runs-dir", str(runs_dir)])
            output = stdout.getvalue()

        self.assertEqual(exit_code, 0, output)
        self.assertEqual(output_value(output, "next_action"), "classify_run")

    def test_show_run_next_action_prepare_required_reviews_when_prompts_missing(self) -> None:
        with temporary_test_dir() as tmp:
            run_dir, runs_dir = make_run_fixture(
                tmp,
                changed_files=["src/demo.py", "EXECUTION_REPORT.json"],
                workspace_files={"src/demo.py": "VALUE = 1\n"},
            )
            with redirect_stdout(StringIO()):
                self.assertEqual(classify_run_main([run_dir.name, "--runs-dir", str(runs_dir)]), 0)
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = show_run_main([run_dir.name, "--runs-dir", str(runs_dir)])
            output = stdout.getvalue()

        self.assertEqual(exit_code, 0, output)
        self.assertEqual(output_value(output, "next_action"), "prepare_required_reviews")

    def test_show_run_next_action_run_review_checks_when_docs_only_findings_missing(self) -> None:
        with temporary_test_dir() as tmp:
            run_dir, runs_dir = make_run_fixture(
                tmp,
                changed_files=["docs/guide.md", "EXECUTION_REPORT.json"],
                workspace_files={"docs/guide.md": "# Guide\n"},
            )
            with redirect_stdout(StringIO()):
                self.assertEqual(classify_run_main([run_dir.name, "--runs-dir", str(runs_dir)]), 0)
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = show_run_main([run_dir.name, "--runs-dir", str(runs_dir)])
            output = stdout.getvalue()

        self.assertEqual(exit_code, 0, output)
        self.assertEqual(output_value(output, "risk_classification_exists"), "true")
        self.assertEqual(output_value(output, "required_review_profiles"), "")
        self.assertEqual(output_value(output, "findings_exists"), "false")
        self.assertEqual(output_value(output, "next_action"), "run_review_checks")

    def test_show_run_next_action_review_run_after_review_checks_pass(self) -> None:
        with temporary_test_dir() as tmp:
            run_dir, runs_dir = make_run_fixture(
                tmp,
                changed_files=["docs/guide.md", "EXECUTION_REPORT.json"],
                workspace_files={"docs/guide.md": "# Guide\n"},
            )
            with redirect_stdout(StringIO()):
                self.assertEqual(classify_run_main([run_dir.name, "--runs-dir", str(runs_dir)]), 0)
                self.assertEqual(
                    run_review_checks_main([run_dir.name, "--runs-dir", str(runs_dir), "--profile", "docs-only"]),
                    0,
                )
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = show_run_main([run_dir.name, "--runs-dir", str(runs_dir)])
            output = stdout.getvalue()

        self.assertEqual(exit_code, 0, output)
        self.assertEqual(output_value(output, "findings_exists"), "true")
        self.assertEqual(output_value(output, "review_findings_decision"), "pass")
        self.assertEqual(output_value(output, "blocking_findings"), "0")
        self.assertEqual(output_value(output, "next_action"), "review_run")

    def test_show_run_next_action_external_reviewer_after_required_prompts_prepared(self) -> None:
        with temporary_test_dir() as tmp:
            run_dir, runs_dir = make_run_fixture(
                tmp,
                changed_files=["src/demo.py", "EXECUTION_REPORT.json"],
                workspace_files={"src/demo.py": "VALUE = 1\n"},
            )
            with redirect_stdout(StringIO()):
                self.assertEqual(classify_run_main([run_dir.name, "--runs-dir", str(runs_dir)]), 0)
                self.assertEqual(prepare_review_main([run_dir.name, "--runs-dir", str(runs_dir), "--required-profiles"]), 0)
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = show_run_main([run_dir.name, "--runs-dir", str(runs_dir)])
            output = stdout.getvalue()

        self.assertEqual(exit_code, 0, output)
        self.assertEqual(output_value(output, "required_review_profiles"), "qa,architecture,maintainability")
        self.assertEqual(output_value(output, "reviewer_prompts_exists"), "true")
        self.assertEqual(output_value(output, "findings_exists"), "false")
        self.assertEqual(output_value(output, "next_action"), "run_external_reviewer_or_record_findings")

    def test_show_pipeline_counts_risk_levels(self) -> None:
        with temporary_test_dir() as tmp:
            run_dir_a, runs_dir = make_run_fixture(
                tmp,
                run_id="run_low",
                changed_files=["docs/guide.md", "EXECUTION_REPORT.json"],
                workspace_files={"docs/guide.md": "# Guide\n"},
            )
            run_dir_b, _ = make_run_fixture(
                tmp,
                run_id="run_high",
                changed_files=["src/demo.py", "EXECUTION_REPORT.json"],
                workspace_files={"src/demo.py": "VALUE = 1\n"},
            )
            with redirect_stdout(StringIO()):
                self.assertEqual(classify_run_main([run_dir_a.name, "--runs-dir", str(runs_dir)]), 0)
                self.assertEqual(classify_run_main([run_dir_b.name, "--runs-dir", str(runs_dir)]), 0)
            create_pipeline_fixture(
                tmp,
                pipeline_id="pipeline_risk_counts",
                tasks=[
                    build_pipeline_task_result("task-a", run_dir_a.name, runs_dir),
                    build_pipeline_task_result("task-b", run_dir_b.name, runs_dir),
                ],
            )
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = show_pipeline_main(["pipeline_risk_counts", "--runs-dir", str(runs_dir)])
            output = stdout.getvalue()

        self.assertEqual(exit_code, 0, output)
        self.assertEqual(output_value(output, "tasks_low_risk"), "1")
        self.assertEqual(output_value(output, "tasks_high_risk"), "1")

    def test_show_pipeline_next_action_classify_runs(self) -> None:
        with temporary_test_dir() as tmp:
            run_dir, runs_dir = make_run_fixture(
                tmp,
                changed_files=["docs/guide.md", "EXECUTION_REPORT.json"],
                workspace_files={"docs/guide.md": "# Guide\n"},
            )
            create_pipeline_fixture(
                tmp,
                pipeline_id="pipeline_classify_runs",
                tasks=[build_pipeline_task_result("task-a", run_dir.name, runs_dir)],
            )
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = show_pipeline_main(["pipeline_classify_runs", "--runs-dir", str(runs_dir)])
            output = stdout.getvalue()

        self.assertEqual(exit_code, 0, output)
        self.assertEqual(output_value(output, "next_action"), "classify_runs")

    def test_show_pipeline_next_action_run_review_checks_for_docs_only_without_required_profiles(self) -> None:
        with temporary_test_dir() as tmp:
            run_dir, runs_dir = make_run_fixture(
                tmp,
                changed_files=["docs/guide.md", "EXECUTION_REPORT.json"],
                workspace_files={"docs/guide.md": "# Guide\n"},
            )
            with redirect_stdout(StringIO()):
                self.assertEqual(classify_run_main([run_dir.name, "--runs-dir", str(runs_dir)]), 0)
            create_pipeline_fixture(
                tmp,
                pipeline_id="pipeline_run_review_checks",
                tasks=[build_pipeline_task_result("task-a", run_dir.name, runs_dir)],
            )
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = show_pipeline_main(["pipeline_run_review_checks", "--runs-dir", str(runs_dir)])
            output = stdout.getvalue()

        self.assertEqual(exit_code, 0, output)
        self.assertEqual(output_value(output, "tasks_waiting_required_review_prompts"), "0")
        self.assertEqual(output_value(output, "tasks_waiting_review_checks"), "1")
        self.assertEqual(output_value(output, "next_action"), "run_review_checks")

    def test_show_pipeline_next_action_prepare_required_reviews(self) -> None:
        with temporary_test_dir() as tmp:
            run_dir, runs_dir = make_run_fixture(
                tmp,
                changed_files=["src/demo.py", "EXECUTION_REPORT.json"],
                workspace_files={"src/demo.py": "VALUE = 1\n"},
            )
            with redirect_stdout(StringIO()):
                self.assertEqual(classify_run_main([run_dir.name, "--runs-dir", str(runs_dir)]), 0)
            create_pipeline_fixture(
                tmp,
                pipeline_id="pipeline_prepare_required_reviews",
                tasks=[build_pipeline_task_result("task-a", run_dir.name, runs_dir)],
            )
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = show_pipeline_main(["pipeline_prepare_required_reviews", "--runs-dir", str(runs_dir)])
            output = stdout.getvalue()

        self.assertEqual(exit_code, 0, output)
        self.assertEqual(output_value(output, "tasks_waiting_required_review_prompts"), "1")
        self.assertEqual(output_value(output, "next_action"), "prepare_required_reviews")

    def test_show_pipeline_next_action_external_reviewer_after_required_prompts_prepared(self) -> None:
        with temporary_test_dir() as tmp:
            run_dir, runs_dir = make_run_fixture(
                tmp,
                changed_files=["src/demo.py", "EXECUTION_REPORT.json"],
                workspace_files={"src/demo.py": "VALUE = 1\n"},
            )
            with redirect_stdout(StringIO()):
                self.assertEqual(classify_run_main([run_dir.name, "--runs-dir", str(runs_dir)]), 0)
                self.assertEqual(prepare_review_main([run_dir.name, "--runs-dir", str(runs_dir), "--required-profiles"]), 0)
            create_pipeline_fixture(
                tmp,
                pipeline_id="pipeline_external_reviewer_findings",
                tasks=[build_pipeline_task_result("task-a", run_dir.name, runs_dir)],
            )
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = show_pipeline_main(["pipeline_external_reviewer_findings", "--runs-dir", str(runs_dir)])
            output = stdout.getvalue()

        self.assertEqual(exit_code, 0, output)
        self.assertEqual(output_value(output, "tasks_waiting_required_review_prompts"), "0")
        self.assertEqual(output_value(output, "tasks_waiting_external_review_findings"), "1")
        self.assertEqual(output_value(output, "next_action"), "run_external_reviewer_or_record_findings")


if __name__ == "__main__":
    unittest.main()
