from __future__ import annotations

from contextlib import contextmanager, redirect_stdout
from io import StringIO
from pathlib import Path
import json
import shutil
import sys
import textwrap
import unittest
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_orchestrator.cli import review_run_main, run_review_checks_main, show_run_main
from ai_orchestrator.deterministic_review import run_deterministic_review_checks
from ai_orchestrator.review_findings import load_run_findings
from ai_orchestrator.schemas import ExecutionResult, ReviewFindingsReport, RunState, TaskSpec, ValidationResult

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
    run_id: str = "run_test_deterministic_review",
    changed_files: list[str],
    workspace_files: dict[str, str] | None = None,
    status: str = "approved",
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
        "summary": "Synthetic deterministic review fixture.",
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
        task=TaskSpec(description="Synthetic deterministic review fixture"),
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
    (run_dir / "final_report.md").write_text("# Final report\n", encoding="utf-8")
    (run_dir / "REVIEW_PACKET.md").write_text("# Review packet\n", encoding="utf-8")
    return run_dir, runs_dir


class DeterministicReviewTests(unittest.TestCase):
    def test_docs_only_run_produces_pass_and_zero_findings(self) -> None:
        with temporary_test_dir() as tmp:
            run_dir, runs_dir = make_run_fixture(
                tmp,
                changed_files=["docs/guide.md", "EXECUTION_REPORT.json"],
                workspace_files={"docs/guide.md": "# Guide\n"},
            )

            result = run_deterministic_review_checks(
                run_id=run_dir.name,
                runs_dir=runs_dir,
                profiles=["docs-only"],
            )
            report = load_run_findings(run_dir)

        self.assertEqual(result.report.overall_decision, "pass")
        self.assertEqual(result.report.counts.total, 0)
        self.assertIsNotNone(report)
        self.assertEqual(report.overall_decision, "pass")
        self.assertEqual(report.counts.blocking_open, 0)

    def test_runtime_generated_file_in_changed_files_creates_critical_blocking_finding(self) -> None:
        with temporary_test_dir() as tmp:
            run_dir, runs_dir = make_run_fixture(
                tmp,
                changed_files=["__pycache__/demo.pyc", "EXECUTION_REPORT.json"],
            )

            result = run_deterministic_review_checks(run_id=run_dir.name, runs_dir=runs_dir)

        self.assertEqual(result.report.overall_decision, "blocked")
        self.assertTrue(any(f.title == "Runtime/generated file listed as changed" for f in result.report.findings))
        self.assertEqual(result.report.counts.critical, 1)
        self.assertEqual(result.report.counts.blocking_open, 1)

    def test_missing_execution_report_in_changed_files_creates_major_blocking_finding(self) -> None:
        with temporary_test_dir() as tmp:
            run_dir, runs_dir = make_run_fixture(
                tmp,
                changed_files=["docs/guide.md"],
                workspace_files={"docs/guide.md": "# Guide\n"},
            )

            result = run_deterministic_review_checks(run_id=run_dir.name, runs_dir=runs_dir)

        self.assertEqual(result.report.overall_decision, "needs_rework")
        self.assertTrue(any(f.title == "EXECUTION_REPORT.json missing from changed_files" for f in result.report.findings))
        self.assertEqual(result.report.counts.major, 1)
        self.assertEqual(result.report.counts.blocking_open, 1)

    def test_source_code_changed_without_tests_creates_major_blocking_finding(self) -> None:
        with temporary_test_dir() as tmp:
            run_dir, runs_dir = make_run_fixture(
                tmp,
                changed_files=["src/demo.py", "EXECUTION_REPORT.json"],
                workspace_files={"src/demo.py": "VALUE = 1\n"},
            )

            result = run_deterministic_review_checks(run_id=run_dir.name, runs_dir=runs_dir)

        self.assertTrue(any(f.title == "Source code changed without test changes" for f in result.report.findings))
        self.assertEqual(result.report.overall_decision, "needs_rework")

    def test_tests_changed_without_source_or_docs_creates_minor_non_blocking_finding(self) -> None:
        with temporary_test_dir() as tmp:
            run_dir, runs_dir = make_run_fixture(
                tmp,
                changed_files=["tests/test_demo.py", "EXECUTION_REPORT.json"],
                workspace_files={"tests/test_demo.py": "def test_demo():\n    assert True\n"},
            )

            result = run_deterministic_review_checks(run_id=run_dir.name, runs_dir=runs_dir)

        self.assertEqual(result.report.overall_decision, "pass")
        self.assertTrue(any(f.title == "Tests changed without source or documentation changes" for f in result.report.findings))
        self.assertEqual(result.report.counts.minor, 1)
        self.assertEqual(result.report.counts.blocking_open, 0)

    def test_high_risk_file_change_creates_major_blocking_finding(self) -> None:
        with temporary_test_dir() as tmp:
            run_dir, runs_dir = make_run_fixture(
                tmp,
                changed_files=["src/ai_orchestrator/apply.py", "tests/test_apply.py", "EXECUTION_REPORT.json"],
                workspace_files={
                    "src/ai_orchestrator/apply.py": "# modified\n",
                    "tests/test_apply.py": "def test_apply():\n    assert True\n",
                },
            )

            result = run_deterministic_review_checks(
                run_id=run_dir.name,
                runs_dir=runs_dir,
                profiles=["code-safety"],
            )

        self.assertTrue(any(f.title == "High-risk orchestration/safety file changed" for f in result.report.findings))
        self.assertEqual(result.report.overall_decision, "needs_rework")

    def test_cli_module_touched_creates_non_blocking_maintainability_finding(self) -> None:
        with temporary_test_dir() as tmp:
            run_dir, runs_dir = make_run_fixture(
                tmp,
                changed_files=["src/ai_orchestrator/cli.py", "tests/test_cli.py", "EXECUTION_REPORT.json"],
                workspace_files={
                    "src/ai_orchestrator/cli.py": "def main():\n    return 0\n",
                    "tests/test_cli.py": "def test_cli():\n    assert True\n",
                },
            )

            result = run_deterministic_review_checks(run_id=run_dir.name, runs_dir=runs_dir)

        finding = next((f for f in result.report.findings if f.title == "CLI module touched"), None)
        self.assertIsNotNone(finding)
        self.assertEqual(finding.category, "maintainability")
        self.assertEqual(finding.severity, "minor")
        self.assertEqual(result.report.counts.blocking_open, 0)

    def test_cli_plus_multiple_source_modules_creates_blocking_maintainability_finding(self) -> None:
        with temporary_test_dir() as tmp:
            workspace_files = {
                "src/ai_orchestrator/cli.py": "def main():\n    return 0\n",
                "src/ai_orchestrator/review.py": "def review():\n    return 'ok'\n",
                "src/ai_orchestrator/rework.py": "def rework():\n    return 'ok'\n",
                "src/ai_orchestrator/run_status.py": "VALUE = 'status'\n",
                "tests/test_cli.py": "def test_cli():\n    assert True\n",
            }
            run_dir, runs_dir = make_run_fixture(
                tmp,
                changed_files=[*workspace_files.keys(), "EXECUTION_REPORT.json"],
                workspace_files=workspace_files,
            )

            result = run_deterministic_review_checks(run_id=run_dir.name, runs_dir=runs_dir)

        finding = next((f for f in result.report.findings if f.title == "CLI and multiple source modules changed together"), None)
        self.assertIsNotNone(finding)
        self.assertEqual(finding.category, "maintainability")
        self.assertEqual(finding.severity, "major")
        self.assertGreaterEqual(result.report.counts.blocking_open, 1)

    def test_schemas_module_touched_creates_non_blocking_maintainability_finding(self) -> None:
        with temporary_test_dir() as tmp:
            run_dir, runs_dir = make_run_fixture(
                tmp,
                changed_files=["src/ai_orchestrator/schemas.py", "tests/test_schemas.py", "EXECUTION_REPORT.json"],
                workspace_files={
                    "src/ai_orchestrator/schemas.py": "class Demo:\n    pass\n",
                    "tests/test_schemas.py": "def test_demo():\n    assert True\n",
                },
            )

            result = run_deterministic_review_checks(run_id=run_dir.name, runs_dir=runs_dir)

        finding = next((f for f in result.report.findings if f.title == "Shared schema module touched"), None)
        self.assertIsNotNone(finding)
        self.assertEqual(finding.category, "maintainability")
        self.assertEqual(finding.severity, "minor")

    def test_large_python_module_over_seven_hundred_lines_creates_minor_maintainability_finding(self) -> None:
        with temporary_test_dir() as tmp:
            large_module = "\n".join(f"LINE_{index} = {index}" for index in range(701)) + "\n"
            run_dir, runs_dir = make_run_fixture(
                tmp,
                changed_files=["src/demo.py", "tests/test_demo.py", "EXECUTION_REPORT.json"],
                workspace_files={
                    "src/demo.py": large_module,
                    "tests/test_demo.py": "def test_demo():\n    assert True\n",
                },
            )

            result = run_deterministic_review_checks(run_id=run_dir.name, runs_dir=runs_dir)

        finding = next((f for f in result.report.findings if f.title == "Large Python module touched"), None)
        self.assertIsNotNone(finding)
        self.assertEqual(finding.category, "maintainability")
        self.assertEqual(finding.severity, "minor")
        self.assertEqual(result.report.counts.blocking_open, 0)

    def test_large_python_module_over_twelve_hundred_lines_creates_major_blocking_finding(self) -> None:
        with temporary_test_dir() as tmp:
            huge_module = "\n".join(f"LINE_{index} = {index}" for index in range(1201)) + "\n"
            run_dir, runs_dir = make_run_fixture(
                tmp,
                changed_files=["src/demo.py", "tests/test_demo.py", "EXECUTION_REPORT.json"],
                workspace_files={
                    "src/demo.py": huge_module,
                    "tests/test_demo.py": "def test_demo():\n    assert True\n",
                },
            )

            result = run_deterministic_review_checks(run_id=run_dir.name, runs_dir=runs_dir)

        finding = next((f for f in result.report.findings if f.title == "Very large Python module touched"), None)
        self.assertIsNotNone(finding)
        self.assertEqual(finding.category, "maintainability")
        self.assertEqual(finding.severity, "major")
        self.assertGreaterEqual(result.report.counts.blocking_open, 1)

    def test_broad_change_more_than_ten_files_creates_major_blocking_finding(self) -> None:
        with temporary_test_dir() as tmp:
            docs_files = {f"docs/file_{index}.md": f"# File {index}\n" for index in range(11)}
            run_dir, runs_dir = make_run_fixture(
                tmp,
                changed_files=[*docs_files.keys(), "EXECUTION_REPORT.json"],
                workspace_files=docs_files,
            )

            result = run_deterministic_review_checks(run_id=run_dir.name, runs_dir=runs_dir)

        self.assertTrue(any(f.title == "Broad change surface" for f in result.report.findings))
        self.assertEqual(result.report.overall_decision, "needs_rework")

    def test_medium_broad_change_six_to_ten_files_creates_minor_non_blocking_finding(self) -> None:
        with temporary_test_dir() as tmp:
            docs_files = {f"docs/file_{index}.md": f"# File {index}\n" for index in range(6)}
            run_dir, runs_dir = make_run_fixture(
                tmp,
                changed_files=[*docs_files.keys(), "EXECUTION_REPORT.json"],
                workspace_files=docs_files,
            )

            result = run_deterministic_review_checks(run_id=run_dir.name, runs_dir=runs_dir)

        self.assertTrue(any(f.title == "Moderately broad change surface" for f in result.report.findings))
        self.assertEqual(result.report.overall_decision, "pass")

    def test_unsafe_path_creates_critical_blocking_finding(self) -> None:
        with temporary_test_dir() as tmp:
            run_dir, runs_dir = make_run_fixture(
                tmp,
                changed_files=["../escape.py", "EXECUTION_REPORT.json"],
            )

            result = run_deterministic_review_checks(run_id=run_dir.name, runs_dir=runs_dir)

        self.assertTrue(any(f.title == "Unsafe changed file path" for f in result.report.findings))
        self.assertEqual(result.report.overall_decision, "blocked")

    def test_run_review_checks_refuses_overwrite_without_force(self) -> None:
        with temporary_test_dir() as tmp:
            run_dir, runs_dir = make_run_fixture(
                tmp,
                changed_files=["docs/guide.md", "EXECUTION_REPORT.json"],
                workspace_files={"docs/guide.md": "# Guide\n"},
            )
            with redirect_stdout(StringIO()):
                first_exit = run_review_checks_main([run_dir.name, "--runs-dir", str(runs_dir)])
            stdout = StringIO()
            with redirect_stdout(stdout):
                second_exit = run_review_checks_main([run_dir.name, "--runs-dir", str(runs_dir)])
            output = stdout.getvalue()

        self.assertEqual(first_exit, 0)
        self.assertEqual(second_exit, 1, output)
        self.assertIn("Review findings already recorded. Pass --force to overwrite them.", output)

    def test_run_review_checks_overwrites_with_force(self) -> None:
        with temporary_test_dir() as tmp:
            run_dir, runs_dir = make_run_fixture(
                tmp,
                changed_files=["docs/guide.md", "EXECUTION_REPORT.json"],
                workspace_files={"docs/guide.md": "# Guide\n"},
            )
            with redirect_stdout(StringIO()):
                self.assertEqual(run_review_checks_main([run_dir.name, "--runs-dir", str(runs_dir)]), 0)

            workspace = run_dir / "artifacts" / "workspace"
            write_text(workspace / "src" / "demo.py", "VALUE = 1\n")
            report_path = workspace / "EXECUTION_REPORT.json"
            payload = json.loads(report_path.read_text(encoding="utf-8"))
            payload["changed_files"] = ["src/demo.py", "EXECUTION_REPORT.json"]
            report_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = run_review_checks_main([run_dir.name, "--runs-dir", str(runs_dir), "--force"])
            output = stdout.getvalue()
            report = load_run_findings(run_dir)

        self.assertEqual(exit_code, 0, output)
        self.assertEqual(report.overall_decision, "needs_rework")
        self.assertTrue(any(f.title == "Source code changed without test changes" for f in report.findings))

    def test_run_review_checks_writes_findings_artifacts_and_updates_state(self) -> None:
        with temporary_test_dir() as tmp:
            run_dir, runs_dir = make_run_fixture(
                tmp,
                changed_files=["src/demo.py", "EXECUTION_REPORT.json"],
                workspace_files={"src/demo.py": "VALUE = 1\n"},
            )

            result = run_deterministic_review_checks(run_id=run_dir.name, runs_dir=runs_dir)
            state = RunState.model_validate_json((run_dir / "state.json").read_text(encoding="utf-8"))
            findings_json = run_dir / "REVIEW_FINDINGS.json"
            findings_md = run_dir / "REVIEW_FINDINGS.md"
            findings_json_exists = findings_json.exists()
            findings_md_exists = findings_md.exists()
            findings_json_path = findings_json.resolve()

        self.assertTrue(findings_json_exists)
        self.assertTrue(findings_md_exists)
        self.assertEqual(state.review_findings_path, str(findings_json_path))
        self.assertEqual(state.review_findings_decision, result.report.overall_decision)
        self.assertEqual(state.review_findings_blocking_count, result.report.counts.blocking_open)
        self.assertIsNotNone(state.review_findings_created_at)
        self.assertEqual(state.review_findings_source_profile, "deterministic")
        self.assertEqual(state.review_findings_source_kind, "deterministic")

    def test_review_run_approved_fails_after_blocking_deterministic_finding(self) -> None:
        with temporary_test_dir() as tmp:
            run_dir, runs_dir = make_run_fixture(
                tmp,
                changed_files=["src/demo.py", "EXECUTION_REPORT.json"],
                workspace_files={"src/demo.py": "VALUE = 1\n"},
            )
            run_deterministic_review_checks(run_id=run_dir.name, runs_dir=runs_dir)
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = review_run_main([run_dir.name, "--runs-dir", str(runs_dir), "--decision", "approved"])
            output = stdout.getvalue()

        self.assertEqual(exit_code, 1, output)
        self.assertIn("run has open blocking review findings", output)

    def test_show_run_displays_pass_findings_for_docs_only_run(self) -> None:
        with temporary_test_dir() as tmp:
            run_dir, runs_dir = make_run_fixture(
                tmp,
                changed_files=["docs/guide.md", "EXECUTION_REPORT.json"],
                workspace_files={"docs/guide.md": "# Guide\n"},
            )
            run_deterministic_review_checks(run_id=run_dir.name, runs_dir=runs_dir, profiles=["docs-only"])
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = show_run_main([run_dir.name, "--runs-dir", str(runs_dir)])
            output = stdout.getvalue()

        self.assertEqual(exit_code, 0, output)
        self.assertEqual(output_value(output, "findings_exists"), "true")
        self.assertEqual(output_value(output, "review_findings_decision"), "pass")
        self.assertEqual(output_value(output, "blocking_findings"), "0")
        self.assertEqual(output_value(output, "review_findings_source_profile"), "deterministic")
        self.assertEqual(output_value(output, "review_findings_source_kind"), "deterministic")

    def test_show_run_next_action_arbitrate_findings_for_blocking_finding(self) -> None:
        with temporary_test_dir() as tmp:
            run_dir, runs_dir = make_run_fixture(
                tmp,
                changed_files=["src/demo.py", "EXECUTION_REPORT.json"],
                workspace_files={"src/demo.py": "VALUE = 1\n"},
            )
            run_deterministic_review_checks(run_id=run_dir.name, runs_dir=runs_dir)
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = show_run_main([run_dir.name, "--runs-dir", str(runs_dir)])
            output = stdout.getvalue()

        self.assertEqual(exit_code, 0, output)
        self.assertEqual(output_value(output, "next_action"), "arbitrate_findings")

    def test_written_json_report_validates_through_model(self) -> None:
        with temporary_test_dir() as tmp:
            run_dir, runs_dir = make_run_fixture(
                tmp,
                changed_files=["docs/guide.md", "EXECUTION_REPORT.json"],
                workspace_files={"docs/guide.md": "# Guide\n"},
            )
            run_deterministic_review_checks(run_id=run_dir.name, runs_dir=runs_dir)
            payload = (run_dir / "REVIEW_FINDINGS.json").read_text(encoding="utf-8")

        report = ReviewFindingsReport.model_validate_json(payload)
        self.assertEqual(report.overall_decision, "pass")


if __name__ == "__main__":
    unittest.main()
