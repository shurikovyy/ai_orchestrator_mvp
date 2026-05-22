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

from ai_orchestrator.backends.mock import MockBackend
from ai_orchestrator.cli import findings_feedback_main, record_findings_main, review_run_main, show_pipeline_main, show_run_main
from ai_orchestrator.engine import TaskExecutionEngine
from ai_orchestrator.pipeline import PipelineSelectedTask, PipelineState, PipelineTaskResult
from ai_orchestrator.rework import execute_rework_run
from ai_orchestrator.apply import load_run_state
from ai_orchestrator.schemas import TaskSpec

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
    path.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")


def write_findings_file(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def make_findings_payload(
    *,
    run_id: str,
    overall_decision: str,
    findings: list[dict],
    summary: str = "Structured review findings.",
) -> dict:
    return {
        "schema_version": "1.0",
        "run_id": run_id,
        "created_at": "2026-05-22T08:00:00+00:00",
        "summary": summary,
        "overall_decision": overall_decision,
        "findings": findings,
    }


def make_approved_source_run(runs_dir: Path) -> tuple[Path, str]:
    state = TaskExecutionEngine(
        MockBackend(),
        runs_dir,
    ).run(
        task=TaskSpec(
            description="Create deterministic demo artifact",
            acceptance_criteria=["deterministic demo artifact"],
            max_retries=1,
        )
    )
    return runs_dir / state.run_id, state.run_id


def record_findings_for_run(runs_dir: Path, run_id: str, payload: dict) -> None:
    with temporary_test_dir() as tmp:
        findings_file = write_findings_file(tmp / "findings.json", payload)
        with redirect_stdout(StringIO()):
            exit_code = record_findings_main([run_id, "--runs-dir", str(runs_dir), "--findings-file", str(findings_file)])
        if exit_code != 0:
            raise AssertionError("failed to record findings fixture")


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
        selected_tasks=[PipelineSelectedTask(task_id=task.task_id, title=task.title, enabled=True) for task in tasks],
        tasks=tasks,
    )
    state.save_json(pipeline_dir / "pipeline_state.json")
    (pipeline_dir / "PIPELINE_REPORT.md").write_text(f"# Pipeline Report: {pipeline_id}\n", encoding="utf-8")


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


class FindingsFeedbackTests(unittest.TestCase):
    def test_findings_feedback_creates_markdown_for_blocking_findings(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            run_dir, run_id = make_approved_source_run(runs_dir)
            record_findings_for_run(
                runs_dir,
                run_id,
                make_findings_payload(
                    run_id=run_id,
                    overall_decision="needs_rework",
                    findings=[
                        {
                            "id": "F001",
                            "reviewer": "qa",
                            "category": "qa",
                            "severity": "major",
                            "title": "Missing regression test",
                            "evidence": "No regression test was added.",
                            "required_action": "Add a regression test.",
                            "file": "src/demo.py",
                            "line": 12,
                            "status": "open",
                        }
                    ],
                ),
            )
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = findings_feedback_main([run_id, "--runs-dir", str(runs_dir)])
            output = stdout.getvalue()
            feedback_path = run_dir / "REVIEW_FEEDBACK_FROM_FINDINGS.md"
            feedback_exists = feedback_path.exists()
            feedback_text = feedback_path.read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0, output)
        self.assertTrue(feedback_exists)
        self.assertEqual(output_value(output, "status"), "findings_feedback_created")
        self.assertIn("F001", feedback_text)
        self.assertIn("Missing regression test", feedback_text)

    def test_feedback_includes_required_fields(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            _run_dir, run_id = make_approved_source_run(runs_dir)
            record_findings_for_run(
                runs_dir,
                run_id,
                make_findings_payload(
                    run_id=run_id,
                    overall_decision="needs_rework",
                    findings=[
                        {
                            "id": "F001",
                            "reviewer": "qa",
                            "category": "qa",
                            "severity": "major",
                            "title": "Missing regression test",
                            "evidence": "Observed missing regression coverage.",
                            "required_action": "Add regression coverage.",
                            "file": "src/demo.py",
                            "line": 10,
                            "status": "open",
                        }
                    ],
                ),
            )
            with redirect_stdout(StringIO()):
                self.assertEqual(findings_feedback_main([run_id, "--runs-dir", str(runs_dir)]), 0)
            feedback_text = (runs_dir / run_id / "REVIEW_FEEDBACK_FROM_FINDINGS.md").read_text(encoding="utf-8")

        self.assertIn("Reviewer: `qa`", feedback_text)
        self.assertIn("Severity: `major`", feedback_text)
        self.assertIn("Observed missing regression coverage.", feedback_text)
        self.assertIn("Add regression coverage.", feedback_text)

    def test_feedback_excludes_minor_and_nit_by_default(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            _run_dir, run_id = make_approved_source_run(runs_dir)
            record_findings_for_run(
                runs_dir,
                run_id,
                make_findings_payload(
                    run_id=run_id,
                    overall_decision="needs_rework",
                    findings=[
                        {
                            "id": "F001",
                            "reviewer": "qa",
                            "category": "qa",
                            "severity": "major",
                            "title": "Blocking issue",
                            "evidence": "Major issue.",
                            "required_action": "Fix the issue.",
                            "status": "open",
                        },
                        {
                            "id": "F002",
                            "reviewer": "docs",
                            "category": "documentation",
                            "severity": "minor",
                            "title": "Minor docs issue",
                            "evidence": "Minor docs gap.",
                            "required_action": "Improve docs.",
                            "status": "open",
                        },
                    ],
                ),
            )
            with redirect_stdout(StringIO()):
                self.assertEqual(findings_feedback_main([run_id, "--runs-dir", str(runs_dir)]), 0)
            feedback_text = (runs_dir / run_id / "REVIEW_FEEDBACK_FROM_FINDINGS.md").read_text(encoding="utf-8")

        self.assertIn("F001", feedback_text)
        self.assertNotIn("F002", feedback_text)

    def test_include_non_blocking_includes_minor_and_nit(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            _run_dir, run_id = make_approved_source_run(runs_dir)
            record_findings_for_run(
                runs_dir,
                run_id,
                make_findings_payload(
                    run_id=run_id,
                    overall_decision="needs_rework",
                    findings=[
                        {
                            "id": "F001",
                            "reviewer": "qa",
                            "category": "qa",
                            "severity": "major",
                            "title": "Blocking issue",
                            "evidence": "Major issue.",
                            "required_action": "Fix the issue.",
                            "status": "open",
                        },
                        {
                            "id": "F002",
                            "reviewer": "docs",
                            "category": "documentation",
                            "severity": "minor",
                            "title": "Minor docs issue",
                            "evidence": "Minor docs gap.",
                            "required_action": "Improve docs.",
                            "status": "open",
                        },
                    ],
                ),
            )
            with redirect_stdout(StringIO()):
                self.assertEqual(
                    findings_feedback_main([run_id, "--runs-dir", str(runs_dir), "--include-non-blocking"]),
                    0,
                )
            feedback_text = (runs_dir / run_id / "REVIEW_FEEDBACK_FROM_FINDINGS.md").read_text(encoding="utf-8")

        self.assertIn("F001", feedback_text)
        self.assertIn("F002", feedback_text)

    def test_resolved_findings_are_excluded(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            _run_dir, run_id = make_approved_source_run(runs_dir)
            record_findings_for_run(
                runs_dir,
                run_id,
                make_findings_payload(
                    run_id=run_id,
                    overall_decision="pass",
                    findings=[
                        {
                            "id": "F001",
                            "reviewer": "qa",
                            "category": "qa",
                            "severity": "major",
                            "title": "Resolved issue",
                            "evidence": "Already fixed.",
                            "required_action": "Keep it fixed.",
                            "status": "resolved",
                        }
                    ],
                ),
            )
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = findings_feedback_main([run_id, "--runs-dir", str(runs_dir), "--include-non-blocking"])
            output = stdout.getvalue()

        self.assertEqual(exit_code, 1, output)
        self.assertEqual(output_value(output, "error"), "no open findings available for feedback")

    def test_accepted_risk_major_findings_are_excluded_by_default(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            _run_dir, run_id = make_approved_source_run(runs_dir)
            record_findings_for_run(
                runs_dir,
                run_id,
                make_findings_payload(
                    run_id=run_id,
                    overall_decision="pass",
                    findings=[
                        {
                            "id": "F001",
                            "reviewer": "ops",
                            "category": "ops",
                            "severity": "major",
                            "title": "Accepted risk major",
                            "evidence": "Tracked as accepted risk.",
                            "required_action": "Monitor it carefully.",
                            "status": "accepted_risk",
                        }
                    ],
                ),
            )
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = findings_feedback_main([run_id, "--runs-dir", str(runs_dir)])
            output = stdout.getvalue()

        self.assertEqual(exit_code, 1, output)
        self.assertEqual(output_value(output, "error"), "no open findings available for feedback")

    def test_accepted_risk_major_findings_are_excluded_even_with_include_non_blocking(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            _run_dir, run_id = make_approved_source_run(runs_dir)
            record_findings_for_run(
                runs_dir,
                run_id,
                make_findings_payload(
                    run_id=run_id,
                    overall_decision="pass",
                    findings=[
                        {
                            "id": "F001",
                            "reviewer": "ops",
                            "category": "ops",
                            "severity": "major",
                            "title": "Accepted risk major",
                            "evidence": "Tracked as accepted risk.",
                            "required_action": "Monitor it carefully.",
                            "status": "accepted_risk",
                        }
                    ],
                ),
            )
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = findings_feedback_main([run_id, "--runs-dir", str(runs_dir), "--include-non-blocking"])
            output = stdout.getvalue()

        self.assertEqual(exit_code, 1, output)
        self.assertEqual(output_value(output, "error"), "no open findings available for feedback")

    def test_accepted_risk_minor_findings_are_excluded_even_with_include_non_blocking(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            _run_dir, run_id = make_approved_source_run(runs_dir)
            record_findings_for_run(
                runs_dir,
                run_id,
                make_findings_payload(
                    run_id=run_id,
                    overall_decision="pass",
                    findings=[
                        {
                            "id": "F001",
                            "reviewer": "docs",
                            "category": "documentation",
                            "severity": "minor",
                            "title": "Accepted risk minor",
                            "evidence": "Tracked as accepted risk.",
                            "required_action": "Monitor it.",
                            "status": "accepted_risk",
                        }
                    ],
                ),
            )
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = findings_feedback_main([run_id, "--runs-dir", str(runs_dir), "--include-non-blocking"])
            output = stdout.getvalue()

        self.assertEqual(exit_code, 1, output)
        self.assertEqual(output_value(output, "error"), "no open findings available for feedback")

    def test_command_fails_if_all_findings_are_accepted_risk_or_resolved(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            _run_dir, run_id = make_approved_source_run(runs_dir)
            record_findings_for_run(
                runs_dir,
                run_id,
                make_findings_payload(
                    run_id=run_id,
                    overall_decision="pass",
                    findings=[
                        {
                            "id": "F001",
                            "reviewer": "ops",
                            "category": "ops",
                            "severity": "major",
                            "title": "Accepted risk major",
                            "evidence": "Tracked as accepted risk.",
                            "required_action": "Monitor it carefully.",
                            "status": "accepted_risk",
                        },
                        {
                            "id": "F002",
                            "reviewer": "qa",
                            "category": "qa",
                            "severity": "major",
                            "title": "Resolved issue",
                            "evidence": "Already fixed.",
                            "required_action": "Keep it fixed.",
                            "status": "resolved",
                        },
                    ],
                ),
            )
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = findings_feedback_main([run_id, "--runs-dir", str(runs_dir), "--include-non-blocking"])
            output = stdout.getvalue()

        self.assertEqual(exit_code, 1, output)
        self.assertEqual(output_value(output, "error"), "no open findings available for feedback")

    def test_command_fails_if_run_missing(self) -> None:
        with temporary_test_dir() as tmp:
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = findings_feedback_main(["missing-run", "--runs-dir", str(tmp / ".runs")])
            output = stdout.getvalue()
        self.assertEqual(exit_code, 1, output)
        self.assertEqual(output_value(output, "error"), "run does not exist: missing-run")

    def test_command_fails_if_review_findings_missing(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            _run_dir, run_id = make_approved_source_run(runs_dir)
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = findings_feedback_main([run_id, "--runs-dir", str(runs_dir)])
            output = stdout.getvalue()
        self.assertEqual(exit_code, 1, output)
        self.assertEqual(output_value(output, "error"), f"review findings not found for run: {run_id}")

    def test_command_fails_if_no_open_findings_available(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            _run_dir, run_id = make_approved_source_run(runs_dir)
            record_findings_for_run(runs_dir, run_id, make_findings_payload(run_id=run_id, overall_decision="pass", findings=[]))
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = findings_feedback_main([run_id, "--runs-dir", str(runs_dir)])
            output = stdout.getvalue()
        self.assertEqual(exit_code, 1, output)
        self.assertEqual(output_value(output, "error"), "no open findings available for feedback")

    def test_command_refuses_overwrite_without_force(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            _run_dir, run_id = make_approved_source_run(runs_dir)
            record_findings_for_run(
                runs_dir,
                run_id,
                make_findings_payload(
                    run_id=run_id,
                    overall_decision="needs_rework",
                    findings=[{
                        "id": "F001", "reviewer": "qa", "category": "qa", "severity": "major",
                        "title": "Blocking issue", "evidence": "Major issue.", "required_action": "Fix it.", "status": "open"
                    }],
                ),
            )
            with redirect_stdout(StringIO()):
                self.assertEqual(findings_feedback_main([run_id, "--runs-dir", str(runs_dir)]), 0)
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = findings_feedback_main([run_id, "--runs-dir", str(runs_dir)])
            output = stdout.getvalue()
        self.assertEqual(exit_code, 1, output)
        self.assertIn("findings feedback already exists:", output_value(output, "error"))

    def test_command_overwrites_with_force(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            run_dir, run_id = make_approved_source_run(runs_dir)
            record_findings_for_run(
                runs_dir,
                run_id,
                make_findings_payload(
                    run_id=run_id,
                    overall_decision="needs_rework",
                    findings=[{
                        "id": "F001", "reviewer": "qa", "category": "qa", "severity": "major",
                        "title": "Blocking issue", "evidence": "Major issue.", "required_action": "Fix it.", "status": "open"
                    }],
                ),
            )
            with redirect_stdout(StringIO()):
                self.assertEqual(findings_feedback_main([run_id, "--runs-dir", str(runs_dir)]), 0)
            feedback_path = run_dir / "REVIEW_FEEDBACK_FROM_FINDINGS.md"
            feedback_path.write_text("# stale\n", encoding="utf-8")
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = findings_feedback_main([run_id, "--runs-dir", str(runs_dir), "--force"])
            output = stdout.getvalue()
            feedback_text = feedback_path.read_text(encoding="utf-8")
        self.assertEqual(exit_code, 0, output)
        self.assertIn("Blocking issue", feedback_text)

    def test_command_updates_state_fields(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            run_dir, run_id = make_approved_source_run(runs_dir)
            record_findings_for_run(
                runs_dir,
                run_id,
                make_findings_payload(
                    run_id=run_id,
                    overall_decision="needs_rework",
                    findings=[{
                        "id": "F001", "reviewer": "qa", "category": "qa", "severity": "major",
                        "title": "Blocking issue", "evidence": "Major issue.", "required_action": "Fix it.", "status": "open"
                    }],
                ),
            )
            with redirect_stdout(StringIO()):
                self.assertEqual(findings_feedback_main([run_id, "--runs-dir", str(runs_dir)]), 0)
            state = load_run_state(run_dir)
        self.assertTrue(state.findings_feedback_path)
        self.assertIsNotNone(state.findings_feedback_created_at)
        self.assertTrue(state.findings_feedback_source_path)
        self.assertEqual(state.findings_feedback_count, 1)

    def test_json_output_is_valid(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            _run_dir, run_id = make_approved_source_run(runs_dir)
            record_findings_for_run(
                runs_dir,
                run_id,
                make_findings_payload(
                    run_id=run_id,
                    overall_decision="needs_rework",
                    findings=[{
                        "id": "F001", "reviewer": "qa", "category": "qa", "severity": "major",
                        "title": "Blocking issue", "evidence": "Major issue.", "required_action": "Fix it.", "status": "open"
                    }],
                ),
            )
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = findings_feedback_main([run_id, "--runs-dir", str(runs_dir), "--format", "json"])
            payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["status"], "findings_feedback_created")
        self.assertEqual(payload["findings_included"], 1)


class ReviewRunFromFindingsTests(unittest.TestCase):
    def test_review_run_rejected_from_findings_creates_feedback_if_missing(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            run_dir, run_id = make_approved_source_run(runs_dir)
            record_findings_for_run(
                runs_dir,
                run_id,
                make_findings_payload(
                    run_id=run_id,
                    overall_decision="needs_rework",
                    findings=[{
                        "id": "F001", "reviewer": "qa", "category": "qa", "severity": "major",
                        "title": "Blocking issue", "evidence": "Major issue.", "required_action": "Fix it.", "status": "open"
                    }],
                ),
            )
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = review_run_main([run_id, "--runs-dir", str(runs_dir), "--decision", "rejected", "--from-findings"])
            output = stdout.getvalue()
            generated_feedback = run_dir / "REVIEW_FEEDBACK_FROM_FINDINGS.md"
            review_feedback = run_dir / "REVIEW_FEEDBACK.md"
            generated_feedback_exists = generated_feedback.exists()
            review_feedback_exists = review_feedback.exists()
        self.assertEqual(exit_code, 0, output)
        self.assertTrue(generated_feedback_exists)
        self.assertTrue(review_feedback_exists)

    def test_review_run_rejected_from_findings_records_rejected_review_with_generated_feedback(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            run_dir, run_id = make_approved_source_run(runs_dir)
            record_findings_for_run(
                runs_dir,
                run_id,
                make_findings_payload(
                    run_id=run_id,
                    overall_decision="needs_rework",
                    findings=[{
                        "id": "F001", "reviewer": "qa", "category": "qa", "severity": "major",
                        "title": "Blocking issue", "evidence": "Major issue.", "required_action": "Fix it.", "status": "open"
                    }],
                ),
            )
            with redirect_stdout(StringIO()):
                exit_code = review_run_main([run_id, "--runs-dir", str(runs_dir), "--decision", "rejected", "--from-findings"])
            state = load_run_state(run_dir)
            review_feedback = (run_dir / "REVIEW_FEEDBACK.md").read_text(encoding="utf-8")
        self.assertEqual(exit_code, 0)
        self.assertEqual(state.human_review_decision, "rejected")
        self.assertIn("F001", review_feedback)
        self.assertIn("Fix it.", review_feedback)

    def test_review_run_from_findings_reuses_existing_generated_feedback_by_default(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            run_dir, run_id = make_approved_source_run(runs_dir)
            record_findings_for_run(
                runs_dir,
                run_id,
                make_findings_payload(
                    run_id=run_id,
                    overall_decision="needs_rework",
                    findings=[{
                        "id": "F001", "reviewer": "qa", "category": "qa", "severity": "major",
                        "title": "Blocking issue", "evidence": "Major issue.", "required_action": "Fix it.", "status": "open"
                    }],
                ),
            )
            with redirect_stdout(StringIO()):
                self.assertEqual(findings_feedback_main([run_id, "--runs-dir", str(runs_dir)]), 0)
            generated_feedback_path = run_dir / "REVIEW_FEEDBACK_FROM_FINDINGS.md"
            generated_feedback_path.write_text("# marker reuse\n", encoding="utf-8")

            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = review_run_main([run_id, "--runs-dir", str(runs_dir), "--decision", "rejected", "--from-findings"])
            output = stdout.getvalue()
            review_feedback_text = (run_dir / "REVIEW_FEEDBACK.md").read_text(encoding="utf-8")
            generated_feedback_text = generated_feedback_path.read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0, output)
        self.assertEqual(generated_feedback_text, "# marker reuse\n")
        self.assertEqual(review_feedback_text, "# marker reuse\n")

    def test_review_run_from_findings_force_feedback_regenerates_feedback(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            run_dir, run_id = make_approved_source_run(runs_dir)
            record_findings_for_run(
                runs_dir,
                run_id,
                make_findings_payload(
                    run_id=run_id,
                    overall_decision="needs_rework",
                    findings=[{
                        "id": "F001", "reviewer": "qa", "category": "qa", "severity": "major",
                        "title": "Blocking issue", "evidence": "Major issue.", "required_action": "Fix it.", "status": "open"
                    }],
                ),
            )
            with redirect_stdout(StringIO()):
                self.assertEqual(findings_feedback_main([run_id, "--runs-dir", str(runs_dir)]), 0)
            generated_feedback_path = run_dir / "REVIEW_FEEDBACK_FROM_FINDINGS.md"
            generated_feedback_path.write_text("# stale marker\n", encoding="utf-8")

            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = review_run_main(
                    [run_id, "--runs-dir", str(runs_dir), "--decision", "rejected", "--from-findings", "--force-feedback"]
                )
            output = stdout.getvalue()
            review_feedback_text = (run_dir / "REVIEW_FEEDBACK.md").read_text(encoding="utf-8")
            generated_feedback_text = generated_feedback_path.read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0, output)
        self.assertNotIn("# stale marker", generated_feedback_text)
        self.assertIn("Blocking issue", generated_feedback_text)
        self.assertIn("Blocking issue", review_feedback_text)

    def test_review_run_from_findings_force_does_not_regenerate_feedback(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            run_dir, run_id = make_approved_source_run(runs_dir)
            record_findings_for_run(
                runs_dir,
                run_id,
                make_findings_payload(
                    run_id=run_id,
                    overall_decision="needs_rework",
                    findings=[{
                        "id": "F001", "reviewer": "qa", "category": "qa", "severity": "major",
                        "title": "Blocking issue", "evidence": "Major issue.", "required_action": "Fix it.", "status": "open"
                    }],
                ),
            )
            with redirect_stdout(StringIO()):
                self.assertEqual(review_run_main([run_id, "--runs-dir", str(runs_dir), "--decision", "rejected", "--from-findings"]), 0)
            generated_feedback_path = run_dir / "REVIEW_FEEDBACK_FROM_FINDINGS.md"
            generated_feedback_path.write_text("# force marker\n", encoding="utf-8")

            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = review_run_main(
                    [run_id, "--runs-dir", str(runs_dir), "--decision", "rejected", "--from-findings", "--force"]
                )
            output = stdout.getvalue()
            review_feedback_text = (run_dir / "REVIEW_FEEDBACK.md").read_text(encoding="utf-8")
            generated_feedback_text = generated_feedback_path.read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0, output)
        self.assertEqual(generated_feedback_text, "# force marker\n")
        self.assertEqual(review_feedback_text, "# force marker\n")

    def test_review_run_from_findings_force_feedback_does_not_overwrite_existing_review_decision(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            run_dir, run_id = make_approved_source_run(runs_dir)
            record_findings_for_run(
                runs_dir,
                run_id,
                make_findings_payload(
                    run_id=run_id,
                    overall_decision="needs_rework",
                    findings=[{
                        "id": "F001", "reviewer": "qa", "category": "qa", "severity": "major",
                        "title": "Blocking issue", "evidence": "Major issue.", "required_action": "Fix it.", "status": "open"
                    }],
                ),
            )
            with redirect_stdout(StringIO()):
                self.assertEqual(review_run_main([run_id, "--runs-dir", str(runs_dir), "--decision", "rejected", "--from-findings"]), 0)
            generated_feedback_path = run_dir / "REVIEW_FEEDBACK_FROM_FINDINGS.md"
            generated_feedback_path.write_text("# keep marker\n", encoding="utf-8")

            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = review_run_main(
                    [run_id, "--runs-dir", str(runs_dir), "--decision", "rejected", "--from-findings", "--force-feedback"]
                )
            output = stdout.getvalue()
            generated_feedback_text = generated_feedback_path.read_text(encoding="utf-8")

        self.assertEqual(exit_code, 1, output)
        self.assertEqual(output_value(output, "error"), "Human review decision already recorded. Pass --force to overwrite it.")
        self.assertEqual(generated_feedback_text, "# keep marker\n")

    def test_review_run_from_findings_force_and_force_feedback_overwrite_both(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            run_dir, run_id = make_approved_source_run(runs_dir)
            record_findings_for_run(
                runs_dir,
                run_id,
                make_findings_payload(
                    run_id=run_id,
                    overall_decision="needs_rework",
                    findings=[{
                        "id": "F001", "reviewer": "qa", "category": "qa", "severity": "major",
                        "title": "Blocking issue", "evidence": "Major issue.", "required_action": "Fix it.", "status": "open"
                    }],
                ),
            )
            with redirect_stdout(StringIO()):
                self.assertEqual(review_run_main([run_id, "--runs-dir", str(runs_dir), "--decision", "rejected", "--from-findings"]), 0)
            generated_feedback_path = run_dir / "REVIEW_FEEDBACK_FROM_FINDINGS.md"
            generated_feedback_path.write_text("# stale both marker\n", encoding="utf-8")

            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = review_run_main(
                    [
                        run_id,
                        "--runs-dir",
                        str(runs_dir),
                        "--decision",
                        "rejected",
                        "--from-findings",
                        "--force",
                        "--force-feedback",
                    ]
                )
            output = stdout.getvalue()
            generated_feedback_text = generated_feedback_path.read_text(encoding="utf-8")
            review_feedback_text = (run_dir / "REVIEW_FEEDBACK.md").read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0, output)
        self.assertNotIn("# stale both marker", generated_feedback_text)
        self.assertIn("Blocking issue", generated_feedback_text)
        self.assertIn("Blocking issue", review_feedback_text)

    def test_review_run_approved_from_findings_fails(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            _run_dir, run_id = make_approved_source_run(runs_dir)
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = review_run_main([run_id, "--runs-dir", str(runs_dir), "--decision", "approved", "--from-findings"])
            output = stdout.getvalue()
        self.assertEqual(exit_code, 1, output)
        self.assertEqual(output_value(output, "error"), "--from-findings is allowed only with --decision rejected")

    def test_review_run_from_findings_with_feedback_fails(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            _run_dir, run_id = make_approved_source_run(runs_dir)
            feedback_path = tmp / "feedback.md"
            write_text(feedback_path, "manual feedback\n")
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = review_run_main(
                    [run_id, "--runs-dir", str(runs_dir), "--decision", "rejected", "--from-findings", "--feedback", str(feedback_path)]
                )
            output = stdout.getvalue()
        self.assertEqual(exit_code, 1, output)
        self.assertEqual(output_value(output, "error"), "--from-findings and --feedback are mutually exclusive")

    def test_review_run_force_feedback_without_from_findings_fails(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            _run_dir, run_id = make_approved_source_run(runs_dir)
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = review_run_main([run_id, "--runs-dir", str(runs_dir), "--decision", "rejected", "--force-feedback"])
            output = stdout.getvalue()
        self.assertEqual(exit_code, 1, output)
        self.assertEqual(output_value(output, "error"), "--force-feedback is allowed only with --from-findings")

    def test_review_run_from_findings_fails_if_no_blocking_findings(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            _run_dir, run_id = make_approved_source_run(runs_dir)
            record_findings_for_run(
                runs_dir,
                run_id,
                make_findings_payload(
                    run_id=run_id,
                    overall_decision="pass",
                    findings=[{
                        "id": "F001", "reviewer": "docs", "category": "documentation", "severity": "minor",
                        "title": "Minor docs issue", "evidence": "Minor docs gap.", "required_action": "Improve docs.", "status": "open"
                    }],
                ),
            )
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = review_run_main([run_id, "--runs-dir", str(runs_dir), "--decision", "rejected", "--from-findings"])
            output = stdout.getvalue()
        self.assertEqual(exit_code, 1, output)
        self.assertEqual(output_value(output, "error"), "no open findings available for feedback")

    def test_rework_run_after_review_run_from_findings_uses_generated_feedback(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            _run_dir, run_id = make_approved_source_run(runs_dir)
            record_findings_for_run(
                runs_dir,
                run_id,
                make_findings_payload(
                    run_id=run_id,
                    overall_decision="needs_rework",
                    findings=[{
                        "id": "F001", "reviewer": "qa", "category": "qa", "severity": "major",
                        "title": "Blocking issue", "evidence": "Major issue.", "required_action": "Fix it.", "status": "open"
                    }],
                ),
            )
            with redirect_stdout(StringIO()):
                self.assertEqual(review_run_main([run_id, "--runs-dir", str(runs_dir), "--decision", "rejected", "--from-findings"]), 0)
            result = execute_rework_run(source_run_id=run_id, runs_dir=runs_dir, backend_name="mock")
            feedback_text = (runs_dir / result.rework_run_id / "REWORK_FEEDBACK.md").read_text(encoding="utf-8")
        self.assertIn("F001", feedback_text)
        self.assertIn("Blocking issue", feedback_text)
        self.assertIn("Fix it.", feedback_text)


class ShowFindingsFeedbackStatusTests(unittest.TestCase):
    def test_show_run_next_action_findings_feedback_when_blocking_findings_exist_but_feedback_missing(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            _run_dir, run_id = make_approved_source_run(runs_dir)
            record_findings_for_run(
                runs_dir,
                run_id,
                make_findings_payload(
                    run_id=run_id,
                    overall_decision="needs_rework",
                    findings=[{
                        "id": "F001", "reviewer": "qa", "category": "qa", "severity": "major",
                        "title": "Blocking issue", "evidence": "Major issue.", "required_action": "Fix it.", "status": "open"
                    }],
                ),
            )
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = show_run_main([run_id, "--runs-dir", str(runs_dir)])
            output = stdout.getvalue()
        self.assertEqual(exit_code, 0, output)
        self.assertEqual(output_value(output, "next_action"), "findings_feedback")

    def test_show_run_next_action_review_rejected_when_feedback_exists_but_human_review_missing(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            _run_dir, run_id = make_approved_source_run(runs_dir)
            record_findings_for_run(
                runs_dir,
                run_id,
                make_findings_payload(
                    run_id=run_id,
                    overall_decision="needs_rework",
                    findings=[{
                        "id": "F001", "reviewer": "qa", "category": "qa", "severity": "major",
                        "title": "Blocking issue", "evidence": "Major issue.", "required_action": "Fix it.", "status": "open"
                    }],
                ),
            )
            with redirect_stdout(StringIO()):
                self.assertEqual(findings_feedback_main([run_id, "--runs-dir", str(runs_dir)]), 0)
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = show_run_main([run_id, "--runs-dir", str(runs_dir), "--show-paths"])
            output = stdout.getvalue()
        self.assertEqual(exit_code, 0, output)
        self.assertEqual(output_value(output, "findings_feedback_exists"), "true")
        self.assertEqual(output_value(output, "findings_feedback_count"), "1")
        self.assertEqual(output_value(output, "next_action"), "review_rejected")
        self.assertIn("findings_feedback=", output)

    def test_show_run_next_action_rework_run_after_rejected_from_findings(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            _run_dir, run_id = make_approved_source_run(runs_dir)
            record_findings_for_run(
                runs_dir,
                run_id,
                make_findings_payload(
                    run_id=run_id,
                    overall_decision="needs_rework",
                    findings=[{
                        "id": "F001", "reviewer": "qa", "category": "qa", "severity": "major",
                        "title": "Blocking issue", "evidence": "Major issue.", "required_action": "Fix it.", "status": "open"
                    }],
                ),
            )
            with redirect_stdout(StringIO()):
                self.assertEqual(review_run_main([run_id, "--runs-dir", str(runs_dir), "--decision", "rejected", "--from-findings"]), 0)
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = show_run_main([run_id, "--runs-dir", str(runs_dir)])
            output = stdout.getvalue()
        self.assertEqual(exit_code, 0, output)
        self.assertEqual(output_value(output, "next_action"), "rework_run")

    def test_show_pipeline_aggregates_findings_feedback(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            _run_dir, run_id = make_approved_source_run(runs_dir)
            record_findings_for_run(
                runs_dir,
                run_id,
                make_findings_payload(
                    run_id=run_id,
                    overall_decision="needs_rework",
                    findings=[{
                        "id": "F001", "reviewer": "qa", "category": "qa", "severity": "major",
                        "title": "Blocking issue", "evidence": "Major issue.", "required_action": "Fix it.", "status": "open"
                    }],
                ),
            )
            create_pipeline_fixture(
                tmp,
                pipeline_id="pipeline_feedback_1",
                tasks=[build_pipeline_task_result("task-a", run_id, runs_dir, title="Task A")],
            )
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = show_pipeline_main(["pipeline_feedback_1", "--runs-dir", str(runs_dir)])
            output = stdout.getvalue()
        self.assertEqual(exit_code, 0, output)
        self.assertEqual(output_value(output, "tasks_waiting_findings_feedback"), "1")
        self.assertEqual(output_value(output, "next_action"), "findings_feedback")

    def test_show_pipeline_aggregates_review_rejected(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            _run_dir, run_id = make_approved_source_run(runs_dir)
            record_findings_for_run(
                runs_dir,
                run_id,
                make_findings_payload(
                    run_id=run_id,
                    overall_decision="needs_rework",
                    findings=[{
                        "id": "F001", "reviewer": "qa", "category": "qa", "severity": "major",
                        "title": "Blocking issue", "evidence": "Major issue.", "required_action": "Fix it.", "status": "open"
                    }],
                ),
            )
            with redirect_stdout(StringIO()):
                self.assertEqual(findings_feedback_main([run_id, "--runs-dir", str(runs_dir)]), 0)
            create_pipeline_fixture(
                tmp,
                pipeline_id="pipeline_feedback_2",
                tasks=[build_pipeline_task_result("task-a", run_id, runs_dir, title="Task A")],
            )
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = show_pipeline_main(["pipeline_feedback_2", "--runs-dir", str(runs_dir)])
            output = stdout.getvalue()
        self.assertEqual(exit_code, 0, output)
        self.assertEqual(output_value(output, "tasks_waiting_rejected_review"), "1")
        self.assertEqual(output_value(output, "next_action"), "review_rejected")


if __name__ == "__main__":
    unittest.main()
