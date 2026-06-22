from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import shutil
import sys
import unittest
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_orchestrator.backends.mock import MockBackend
from ai_orchestrator.schemas import ExecutionResult, PlanStep, StructuredExecutionReport, TaskSpec
from ai_orchestrator.validation import collect_validator_advisory_warnings


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


def make_report(changed_files: list[str], **overrides: object) -> dict[str, object]:
    report: dict[str, object] = {
        "schema_version": "1.0",
        "status": "completed",
        "summary": "Changed files for validator advisory warning tests.",
        "changed_files": changed_files,
        "commands_run": [
            {
                "command": "python -m unittest tests.test_validator_warnings",
                "exit_code": 0,
                "status": "passed",
                "summary": "Advisory warning tests passed.",
            }
        ],
        "tests": [],
        "risks": [],
        "assumptions": [],
        "validation_notes": [],
    }
    report.update(overrides)
    return report


def collect_codes(changed_files: list[str]) -> list[str]:
    report = StructuredExecutionReport.model_validate(make_report(changed_files))
    return [warning.code for warning in collect_validator_advisory_warnings(report)]


def collect_by_code(changed_files: list[str]) -> dict[str, object]:
    report = StructuredExecutionReport.model_validate(make_report(changed_files))
    warnings = collect_validator_advisory_warnings(report)
    return {warning.code: warning for warning in warnings}


def validate_report(changed_files: list[str], **report_overrides: object):
    with temporary_test_dir() as tmp_dir:
        report_path = tmp_dir / "EXECUTION_REPORT.json"
        report_path.write_text(json.dumps(make_report(changed_files, **report_overrides), indent=2), encoding="utf-8")
        result = ExecutionResult(
            step_id="step_1",
            attempt=1,
            status="completed",
            content="executor output",
            artifact_paths=[str(report_path)],
        )
        task = TaskSpec(
            description="Modify validator-sensitive files",
            acceptance_criteria=[],
            require_structured_report=True,
        )
        step = PlanStep(
            id="step_1",
            title="Validate advisory warnings",
            description=task.description,
            acceptance_criteria=task.acceptance_criteria,
        )
        return MockBackend().validate_step(task=task, step=step, result=result)


class ValidatorAdvisoryWarningHelperTests(unittest.TestCase):
    def test_docs_only_changed_files_return_no_warnings(self):
        self.assertEqual(collect_codes(["README.md", "docs/usage.md"]), [])

    def test_apply_logic_warning_has_expected_severity_and_profiles(self):
        warnings = collect_by_code(["src/ai_orchestrator/apply.py"])
        warning = warnings["validator_warning_sensitive_apply_logic"]

        self.assertEqual(warning.severity, "high")
        self.assertEqual(warning.reviewer_profiles, ("security", "architecture", "qa"))

    def test_review_decision_logic_warning(self):
        self.assertIn(
            "validator_warning_sensitive_review_decision_logic",
            collect_codes(["src/ai_orchestrator/review_decision.py"]),
        )

    def test_web_job_actions_warn_for_action_and_subprocess_construction(self):
        codes = collect_codes(["src/ai_orchestrator_web/jobs/actions.py"])

        self.assertIn("validator_warning_sensitive_web_job_action", codes)
        self.assertIn("validator_warning_sensitive_subprocess_command_construction", codes)

    def test_web_job_runner_warns_for_runner_and_subprocess_construction(self):
        codes = collect_codes(["src/ai_orchestrator_web/jobs/runner.py"])

        self.assertIn("validator_warning_sensitive_job_runner", codes)
        self.assertIn("validator_warning_sensitive_subprocess_command_construction", codes)

    def test_ci_workflow_change_warning(self):
        self.assertIn(
            "validator_warning_ci_workflow_change",
            collect_codes([".github/workflows/ci.yml"]),
        )

    def test_dependency_manifest_change_warning(self):
        self.assertIn(
            "validator_warning_dependency_manifest_change",
            collect_codes(["pyproject.toml"]),
        )

    def test_lockfile_change_warning(self):
        self.assertIn(
            "validator_warning_lockfile_change",
            collect_codes(["poetry.lock"]),
        )

    def test_source_change_without_tests_warns(self):
        self.assertIn(
            "validator_warning_missing_tests_for_code_change",
            collect_codes(["src/ai_orchestrator/example.py"]),
        )

    def test_source_change_with_tests_does_not_warn_for_missing_tests(self):
        self.assertNotIn(
            "validator_warning_missing_tests_for_code_change",
            collect_codes(["src/ai_orchestrator/example.py", "tests/test_example.py"]),
        )

    def test_large_changed_file_set_warns(self):
        changed_files = [f"docs/file_{index}.md" for index in range(16)]

        self.assertIn("validator_warning_changed_files_large_set", collect_codes(changed_files))

    def test_warning_codes_and_reviewer_profiles_are_deterministic_and_unique(self):
        changed_files = [
            "src/ai_orchestrator_web/jobs/actions.py",
            "src/ai_orchestrator_web/jobs/actions.py",
            "src/ai_orchestrator/apply.py",
            "src/ai_orchestrator/review_arbitration.py",
            "src/ai_orchestrator/review_arbitration_schemas.py",
        ]
        report = StructuredExecutionReport.model_validate(make_report(changed_files))

        first = collect_validator_advisory_warnings(report)
        second = collect_validator_advisory_warnings(report)
        first_codes = [warning.code for warning in first]
        second_codes = [warning.code for warning in second]

        self.assertEqual(first_codes, second_codes)
        self.assertEqual(len(first_codes), len(set(first_codes)))
        for warning in first:
            self.assertEqual(len(warning.reviewer_profiles), len(set(warning.reviewer_profiles)))


class ValidatorAdvisoryWarningBackendIntegrationTests(unittest.TestCase):
    def test_sensitive_path_warning_does_not_fail_otherwise_passing_validation(self):
        validation = validate_report(["src/ai_orchestrator/apply.py"])

        self.assertTrue(validation.approved, validation.feedback)
        self.assertEqual(validation.failed_criteria, [])
        self.assertTrue(
            any(
                "Validator advisory warning [validator_warning_sensitive_apply_logic]" in item
                for item in validation.feedback
            )
        )

    def test_sensitive_path_warning_is_included_with_existing_validation_failure(self):
        validation = validate_report(["src/ai_orchestrator/apply.py"], status="partial")

        self.assertFalse(validation.approved)
        self.assertIn("report.status=completed", validation.failed_criteria)
        self.assertTrue(
            any(
                "Validator advisory warning [validator_warning_sensitive_apply_logic]" in item
                for item in validation.feedback
            )
        )

    def test_advisory_warnings_are_not_added_to_failed_criteria(self):
        validation = validate_report(["src/ai_orchestrator/apply.py"], status="partial")

        self.assertFalse(any(item.startswith("validator_warning_") for item in validation.failed_criteria))


if __name__ == "__main__":
    unittest.main()
