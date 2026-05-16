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
from ai_orchestrator.schemas import ExecutionResult, PlanStep, TaskSpec

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


def make_report(**overrides):
    report = {
        "schema_version": "1.0",
        "status": "completed",
        "summary": "Created toy calc project and ran tests.",
        "changed_files": ["src/toy_calc.py", "tests/test_toy_calc.py", "EXECUTION_REPORT.json"],
        "commands_run": [
            {
                "command": "python -m unittest discover -s tests",
                "exit_code": 0,
                "status": "passed",
                "summary": "All tests passed.",
            }
        ],
        "tests": [
            {
                "name": "unittest",
                "command": "python -m unittest discover -s tests",
                "status": "passed",
                "total": 5,
                "passed": 5,
                "failed": 0,
                "output": "Ran 5 tests in 0.000s\n\nOK",
            }
        ],
        "risks": ["Toy sys.path import is acceptable for MVP."],
        "assumptions": ["No packaging metadata was required."],
        "validation_notes": ["TOY_CALC_TESTS_PASSED"],
    }
    report.update(overrides)
    return report


class StructuredReportValidationTests(unittest.TestCase):
    def test_required_structured_report_approves_valid_report_and_dsl_criteria(self):
        with temporary_test_dir() as tmp:
            report_path = tmp / "EXECUTION_REPORT.json"
            report_path.write_text(json.dumps(make_report(), indent=2), encoding="utf-8")
            result = ExecutionResult(
                step_id="step_1",
                attempt=1,
                status="completed",
                content="executor output",
                artifact_paths=[str(report_path)],
            )
            task = TaskSpec(
                description="Create toy project and run tests",
                acceptance_criteria=[
                    "report.status=completed",
                    "changed_files includes src/toy_calc.py",
                    "commands_run includes python -m unittest discover -s tests",
                    "tests.status=passed",
                ],
                require_structured_report=True,
            )
            step = PlanStep(
                id="step_1",
                title="Create task artifact",
                description=task.description,
                acceptance_criteria=task.acceptance_criteria,
            )

            validation = MockBackend().validate_step(task=task, step=step, result=result)

        self.assertTrue(validation.approved)
        self.assertEqual(validation.score, 1.0)
        self.assertIn("Structured report and explicit acceptance criteria passed.", validation.feedback)

    def test_required_structured_report_fails_when_missing(self):
        result = ExecutionResult(
            step_id="step_1",
            attempt=1,
            status="completed",
            content="No JSON report here.",
            artifact_paths=[],
        )
        task = TaskSpec(
            description="Create toy project and run tests",
            acceptance_criteria=[],
            require_structured_report=True,
        )
        step = PlanStep(id="step_1", title="Create task artifact", description=task.description)

        validation = MockBackend().validate_step(task=task, step=step, result=result)

        self.assertFalse(validation.approved)
        self.assertIn("structured_execution_report_present", validation.failed_criteria)

    def test_structured_report_fails_when_test_status_failed(self):
        with temporary_test_dir() as tmp:
            bad = make_report(
                tests=[
                    {
                        "name": "unittest",
                        "command": "python -m unittest discover -s tests",
                        "status": "failed",
                        "total": 5,
                        "passed": 4,
                        "failed": 1,
                        "output": "FAILED",
                    }
                ]
            )
            report_path = tmp / "EXECUTION_REPORT.json"
            report_path.write_text(json.dumps(bad, indent=2), encoding="utf-8")
            result = ExecutionResult(
                step_id="step_1",
                attempt=1,
                status="completed",
                content="executor output",
                artifact_paths=[str(report_path)],
            )
            task = TaskSpec(
                description="Create toy project and run tests",
                acceptance_criteria=["tests.status=passed"],
                require_structured_report=True,
            )
            step = PlanStep(
                id="step_1",
                title="Create task artifact",
                description=task.description,
                acceptance_criteria=task.acceptance_criteria,
            )

            validation = MockBackend().validate_step(task=task, step=step, result=result)

        self.assertFalse(validation.approved)
        self.assertIn("test_status_passed:unittest", validation.failed_criteria)
        self.assertIn("tests.status=passed", validation.failed_criteria)

    def test_invalid_report_json_fails_validation(self):
        with temporary_test_dir() as tmp:
            report_path = tmp / "EXECUTION_REPORT.json"
            report_path.write_text("{not valid json", encoding="utf-8")
            result = ExecutionResult(
                step_id="step_1",
                attempt=1,
                status="completed",
                content="executor output",
                artifact_paths=[str(report_path)],
            )
            task = TaskSpec(
                description="Create toy project",
                acceptance_criteria=[],
                require_structured_report=True,
            )
            step = PlanStep(id="step_1", title="Create task artifact", description=task.description)

            validation = MockBackend().validate_step(task=task, step=step, result=result)

        self.assertFalse(validation.approved)
        self.assertIn("valid_structured_execution_report", validation.failed_criteria)

    def test_rerun_report_test_commands_approves_when_reported_tests_really_pass(self):
        with temporary_test_dir() as tmp:
            tests_dir = tmp / "tests"
            tests_dir.mkdir()
            (tests_dir / "test_smoke.py").write_text(
                "import unittest\n\n"
                "class SmokeTest(unittest.TestCase):\n"
                "    def test_truth(self):\n"
                "        self.assertTrue(True)\n",
                encoding="utf-8",
            )
            report_path = tmp / "EXECUTION_REPORT.json"
            report_path.write_text(json.dumps(make_report(), indent=2), encoding="utf-8")
            result = ExecutionResult(
                step_id="step_1",
                attempt=1,
                status="completed",
                content="executor output",
                artifact_paths=[str(report_path)],
            )
            task = TaskSpec(
                description="Create toy project and run tests",
                acceptance_criteria=["tests.status=passed"],
                require_structured_report=True,
                rerun_report_test_commands=True,
            )
            step = PlanStep(
                id="step_1",
                title="Create task artifact",
                description=task.description,
                acceptance_criteria=task.acceptance_criteria,
            )

            validation = MockBackend().validate_step(task=task, step=step, result=result)

        self.assertTrue(validation.approved, validation.feedback)
        self.assertTrue(any("Validator re-ran test command successfully" in item for item in validation.feedback))

    def test_rerun_report_test_commands_fails_when_actual_test_fails(self):
        with temporary_test_dir() as tmp:
            tests_dir = tmp / "tests"
            tests_dir.mkdir()
            (tests_dir / "test_smoke.py").write_text(
                "import unittest\n\n"
                "class SmokeTest(unittest.TestCase):\n"
                "    def test_truth(self):\n"
                "        self.assertTrue(False)\n",
                encoding="utf-8",
            )
            report_path = tmp / "EXECUTION_REPORT.json"
            # The report claims tests passed. The validator rerun must catch the lie.
            report_path.write_text(json.dumps(make_report(), indent=2), encoding="utf-8")
            result = ExecutionResult(
                step_id="step_1",
                attempt=1,
                status="completed",
                content="executor output",
                artifact_paths=[str(report_path)],
            )
            task = TaskSpec(
                description="Create toy project and run tests",
                acceptance_criteria=["tests.status=passed"],
                require_structured_report=True,
                rerun_report_test_commands=True,
            )
            step = PlanStep(
                id="step_1",
                title="Create task artifact",
                description=task.description,
                acceptance_criteria=task.acceptance_criteria,
            )

            validation = MockBackend().validate_step(task=task, step=step, result=result)

        self.assertFalse(validation.approved)
        self.assertTrue(
            any(item.startswith("validator_rerun_test_command:") for item in validation.failed_criteria),
            validation.failed_criteria,
        )

    def test_rerun_report_test_commands_blocks_shell_control_operators(self):
        with temporary_test_dir() as tmp:
            report = make_report(
                tests=[
                    {
                        "name": "bad command",
                        "command": "python -m unittest discover -s tests && echo hacked",
                        "status": "passed",
                        "total": 1,
                        "passed": 1,
                        "failed": 0,
                        "output": "OK",
                    }
                ]
            )
            report_path = tmp / "EXECUTION_REPORT.json"
            report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
            result = ExecutionResult(
                step_id="step_1",
                attempt=1,
                status="completed",
                content="executor output",
                artifact_paths=[str(report_path)],
            )
            task = TaskSpec(
                description="Create toy project and run tests",
                acceptance_criteria=["tests.status=passed"],
                require_structured_report=True,
                rerun_report_test_commands=True,
            )
            step = PlanStep(
                id="step_1",
                title="Create task artifact",
                description=task.description,
                acceptance_criteria=task.acceptance_criteria,
            )

            validation = MockBackend().validate_step(task=task, step=step, result=result)

        self.assertFalse(validation.approved)
        self.assertTrue(any("validator_rerun_test_command:" in item for item in validation.failed_criteria))
        self.assertTrue(any("shell control" in item for item in validation.feedback))


    def test_workspace_manifest_validation_approves_when_report_matches_workspace_files(self):
        with temporary_test_dir() as tmp:
            (tmp / "src").mkdir()
            (tmp / "tests").mkdir()
            (tmp / "src" / "toy_calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
            (tmp / "tests" / "test_toy_calc.py").write_text("import unittest\n", encoding="utf-8")
            (tmp / "src" / "__pycache__").mkdir()
            (tmp / "src" / "__pycache__" / "toy_calc.cpython-313.pyc").write_bytes(b"ignored")
            report_path = tmp / "EXECUTION_REPORT.json"
            report_path.write_text(json.dumps(make_report(), indent=2), encoding="utf-8")
            result = ExecutionResult(
                step_id="step_1",
                attempt=1,
                status="completed",
                content="executor output",
                artifact_paths=[str(report_path)],
            )
            task = TaskSpec(
                description="Create toy project and run tests",
                acceptance_criteria=["changed_files includes src/toy_calc.py"],
                require_structured_report=True,
                validate_workspace_manifest=True,
            )
            step = PlanStep(
                id="step_1",
                title="Create task artifact",
                description=task.description,
                acceptance_criteria=task.acceptance_criteria,
            )

            validation = MockBackend().validate_step(task=task, step=step, result=result)

        self.assertTrue(validation.approved, validation.feedback)
        self.assertTrue(any("Workspace file manifest matches" in item for item in validation.feedback))

    def test_workspace_manifest_validation_fails_when_reported_file_is_missing(self):
        with temporary_test_dir() as tmp:
            (tmp / "src").mkdir()
            (tmp / "src" / "toy_calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
            report_path = tmp / "EXECUTION_REPORT.json"
            report_path.write_text(json.dumps(make_report(), indent=2), encoding="utf-8")
            result = ExecutionResult(
                step_id="step_1",
                attempt=1,
                status="completed",
                content="executor output",
                artifact_paths=[str(report_path)],
            )
            task = TaskSpec(
                description="Create toy project and run tests",
                acceptance_criteria=[],
                require_structured_report=True,
                validate_workspace_manifest=True,
            )
            step = PlanStep(id="step_1", title="Create task artifact", description=task.description)

            validation = MockBackend().validate_step(task=task, step=step, result=result)

        self.assertFalse(validation.approved)
        self.assertIn("workspace_manifest_matches_changed_files", validation.failed_criteria)
        self.assertTrue(any("missing reported files" in item for item in validation.feedback))

    def test_workspace_manifest_validation_fails_when_workspace_has_unreported_source_file(self):
        with temporary_test_dir() as tmp:
            (tmp / "src").mkdir()
            (tmp / "tests").mkdir()
            (tmp / "src" / "toy_calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
            (tmp / "src" / "extra.py").write_text("EXTRA = True\n", encoding="utf-8")
            (tmp / "tests" / "test_toy_calc.py").write_text("import unittest\n", encoding="utf-8")
            report_path = tmp / "EXECUTION_REPORT.json"
            report_path.write_text(json.dumps(make_report(), indent=2), encoding="utf-8")
            result = ExecutionResult(
                step_id="step_1",
                attempt=1,
                status="completed",
                content="executor output",
                artifact_paths=[str(report_path)],
            )
            task = TaskSpec(
                description="Create toy project and run tests",
                acceptance_criteria=[],
                require_structured_report=True,
                validate_workspace_manifest=True,
            )
            step = PlanStep(id="step_1", title="Create task artifact", description=task.description)

            validation = MockBackend().validate_step(task=task, step=step, result=result)

        self.assertFalse(validation.approved)
        self.assertIn("workspace_manifest_matches_changed_files", validation.failed_criteria)
        self.assertTrue(any("unreported files" in item and "src/extra.py" in item for item in validation.feedback))

    def test_workspace_manifest_validation_fails_when_report_lists_generated_file(self):
        with temporary_test_dir() as tmp:
            (tmp / "src").mkdir()
            (tmp / "tests").mkdir()
            (tmp / "src" / "toy_calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
            (tmp / "tests" / "test_toy_calc.py").write_text("import unittest\n", encoding="utf-8")
            (tmp / "src" / "__pycache__").mkdir()
            (tmp / "src" / "__pycache__" / "toy_calc.cpython-313.pyc").write_bytes(b"ignored")
            report = make_report(changed_files=[
                "src/toy_calc.py",
                "tests/test_toy_calc.py",
                "EXECUTION_REPORT.json",
                "src/__pycache__/toy_calc.cpython-313.pyc",
            ])
            report_path = tmp / "EXECUTION_REPORT.json"
            report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
            result = ExecutionResult(
                step_id="step_1",
                attempt=1,
                status="completed",
                content="executor output",
                artifact_paths=[str(report_path)],
            )
            task = TaskSpec(
                description="Create toy project and run tests",
                acceptance_criteria=[],
                require_structured_report=True,
                validate_workspace_manifest=True,
            )
            step = PlanStep(id="step_1", title="Create task artifact", description=task.description)

            validation = MockBackend().validate_step(task=task, step=step, result=result)

        self.assertFalse(validation.approved)
        self.assertIn("workspace_manifest_matches_changed_files", validation.failed_criteria)
        self.assertTrue(any("ignored/generated files" in item for item in validation.feedback))


if __name__ == "__main__":
    unittest.main()
