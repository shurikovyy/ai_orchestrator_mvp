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
from ai_orchestrator.cli import record_findings_main, review_run_main, show_pipeline_main, show_run_main
from ai_orchestrator.engine import TaskExecutionEngine
from ai_orchestrator.pipeline import PipelineSelectedTask, PipelineState, PipelineTaskResult
from ai_orchestrator.review_findings import has_blocking_findings, load_findings_file
from ai_orchestrator.schemas import ReviewFindingsReport, RunState, TaskSpec

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
            return line[len(prefix):].strip()
    raise AssertionError(f"missing output line for {key!r} in:\n{output}")


def write_text(path: Path, content: str) -> None:
    path.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")


def write_findings_file(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def make_findings_payload(
    *,
    run_id: str,
    overall_decision: str = "pass",
    summary: str = "Review summary.",
    findings: list[dict] | None = None,
    source_profile: str | None = None,
    source_kind: str | None = None,
) -> dict:
    payload = {
        "schema_version": "1.0",
        "run_id": run_id,
        "created_at": "2026-05-21T10:00:00+00:00",
        "summary": summary,
        "overall_decision": overall_decision,
        "findings": findings or [],
    }
    if source_profile is not None:
        payload["source_profile"] = source_profile
    if source_kind is not None:
        payload["source_kind"] = source_kind
    return payload


def make_approved_source_run(runs_dir: Path, *, task: TaskSpec | None = None) -> tuple[Path, str]:
    source_task = task or TaskSpec(
        description="Create deterministic demo artifact",
        acceptance_criteria=["deterministic demo artifact"],
        max_retries=1,
    )
    state = TaskExecutionEngine(MockBackend(), runs_dir).run(source_task)
    return runs_dir / state.run_id, state.run_id


def create_pipeline_fixture(
    root: Path,
    *,
    pipeline_id: str,
    tasks: list[PipelineTaskResult],
    selected_tasks: list[PipelineSelectedTask] | None = None,
    status: str = "approved",
    tasks_file: str | None = None,
) -> tuple[Path, Path]:
    pipeline_dir = root / ".runs" / "pipelines" / pipeline_id
    pipeline_dir.mkdir(parents=True, exist_ok=True)
    state = PipelineState(
        pipeline_id=pipeline_id,
        tasks_file=tasks_file or str((root / "tasks.yaml").resolve()),
        status=status,
        selected_tasks=selected_tasks or [
            PipelineSelectedTask(task_id=task.task_id, title=task.title, enabled=True) for task in tasks
        ],
        tasks=tasks,
    )
    state_path = pipeline_dir / "pipeline_state.json"
    state.save_json(state_path)
    report_path = pipeline_dir / "PIPELINE_REPORT.md"
    report_path.write_text(f"# Pipeline Report: {pipeline_id}\n", encoding="utf-8")
    return state_path, report_path


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


class ReviewFindingsSchemaTests(unittest.TestCase):
    def test_valid_findings_file_loads(self) -> None:
        with temporary_test_dir() as tmp:
            findings_file = write_findings_file(
                tmp / "findings.json",
                make_findings_payload(
                    run_id="run_123",
                    findings=[
                        {
                            "id": "F001",
                            "reviewer": "qa",
                            "category": "qa",
                            "severity": "minor",
                            "title": "Missing wording tweak",
                            "evidence": "The output wording is slightly unclear.",
                            "required_action": "Tighten the wording.",
                            "status": "open",
                        }
                    ],
                ),
            )
            report = load_findings_file(findings_file)

        self.assertIsInstance(report, ReviewFindingsReport)
        self.assertEqual(report.run_id, "run_123")
        self.assertEqual(report.counts.total, 1)

    def test_counts_computed_correctly(self) -> None:
        report = ReviewFindingsReport.model_validate(
            make_findings_payload(
                run_id="run_123",
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
                        "status": "open",
                    },
                    {
                        "id": "F002",
                        "reviewer": "docs",
                        "category": "documentation",
                        "severity": "nit",
                        "title": "Wording nit",
                        "evidence": "One sentence is awkward.",
                        "required_action": "Polish wording.",
                        "status": "accepted_risk",
                    },
                    {
                        "id": "F003",
                        "reviewer": "architecture",
                        "category": "architecture",
                        "severity": "critical",
                        "title": "Architectural risk resolved",
                        "evidence": "A risky design was discussed and fixed.",
                        "required_action": "Keep final fix.",
                        "status": "resolved",
                    },
                ],
            )
        )

        self.assertEqual(report.counts.total, 3)
        self.assertEqual(report.counts.critical, 1)
        self.assertEqual(report.counts.major, 1)
        self.assertEqual(report.counts.nit, 1)
        self.assertEqual(report.counts.blocking_open, 1)
        self.assertEqual(report.counts.accepted_risk, 1)
        self.assertEqual(report.counts.resolved, 1)

    def test_critical_open_finding_is_blocking(self) -> None:
        report = ReviewFindingsReport.model_validate(
            make_findings_payload(
                run_id="run_123",
                overall_decision="blocked",
                findings=[
                    {
                        "id": "F001",
                        "reviewer": "security",
                        "category": "security",
                        "severity": "critical",
                        "title": "Security break",
                        "evidence": "An auth boundary is bypassed.",
                        "required_action": "Restore the boundary.",
                        "status": "open",
                    }
                ],
            )
        )
        self.assertTrue(report.findings[0].blocking)
        self.assertTrue(has_blocking_findings(report))

    def test_major_open_finding_is_blocking(self) -> None:
        report = ReviewFindingsReport.model_validate(
            make_findings_payload(
                run_id="run_123",
                overall_decision="needs_rework",
                findings=[
                    {
                        "id": "F001",
                        "reviewer": "qa",
                        "category": "qa",
                        "severity": "major",
                        "title": "Missing coverage",
                        "evidence": "No regression coverage exists.",
                        "required_action": "Add regression coverage.",
                        "status": "open",
                    }
                ],
            )
        )
        self.assertTrue(report.findings[0].blocking)
        self.assertTrue(has_blocking_findings(report))

    def test_minor_and_nit_are_non_blocking(self) -> None:
        report = ReviewFindingsReport.model_validate(
            make_findings_payload(
                run_id="run_123",
                overall_decision="pass",
                findings=[
                    {
                        "id": "F001",
                        "reviewer": "docs",
                        "category": "documentation",
                        "severity": "minor",
                        "title": "Small doc gap",
                        "evidence": "One step is missing.",
                        "required_action": "Add the missing step.",
                        "status": "open",
                    },
                    {
                        "id": "F002",
                        "reviewer": "qa",
                        "category": "other",
                        "severity": "nit",
                        "title": "Naming nit",
                        "evidence": "One variable name is awkward.",
                        "required_action": "Rename it when convenient.",
                        "status": "open",
                    },
                ],
            )
        )
        self.assertFalse(report.findings[0].blocking)
        self.assertFalse(report.findings[1].blocking)
        self.assertFalse(has_blocking_findings(report))

    def test_resolved_critical_is_non_blocking(self) -> None:
        report = ReviewFindingsReport.model_validate(
            make_findings_payload(
                run_id="run_123",
                overall_decision="pass",
                findings=[
                    {
                        "id": "F001",
                        "reviewer": "security",
                        "category": "security",
                        "severity": "critical",
                        "title": "Resolved issue",
                        "evidence": "The issue existed but is now fixed.",
                        "required_action": "Keep the fix.",
                        "status": "resolved",
                    }
                ],
            )
        )
        self.assertTrue(report.findings[0].blocking)
        self.assertFalse(has_blocking_findings(report))

    def test_invalid_severity_fails(self) -> None:
        with self.assertRaisesRegex(ValueError, "severity"):
            ReviewFindingsReport.model_validate(
                make_findings_payload(
                    run_id="run_123",
                    findings=[
                        {
                            "id": "F001",
                            "reviewer": "qa",
                            "category": "qa",
                            "severity": "medium",
                            "title": "Bad severity",
                            "evidence": "Invalid severity used.",
                            "required_action": "Use allowed severity.",
                            "status": "open",
                        }
                    ],
                )
            )

    def test_missing_required_fields_fail(self) -> None:
        with self.assertRaisesRegex(ValueError, "required finding field must not be empty"):
            ReviewFindingsReport.model_validate(
                make_findings_payload(
                    run_id="run_123",
                    findings=[
                        {
                            "id": "F001",
                            "reviewer": "qa",
                            "category": "qa",
                            "severity": "minor",
                            "title": "",
                            "evidence": "Observed problem.",
                            "required_action": "Fix it.",
                            "status": "open",
                        }
                    ],
                )
            )

    def test_duplicate_finding_ids_fail(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicate finding id: F001"):
            ReviewFindingsReport.model_validate(
                make_findings_payload(
                    run_id="run_123",
                    overall_decision="pass",
                    findings=[
                        {
                            "id": "F001",
                            "reviewer": "qa",
                            "category": "qa",
                            "severity": "minor",
                            "title": "First finding",
                            "evidence": "Observed first issue.",
                            "required_action": "Fix the first issue.",
                            "status": "open",
                        },
                        {
                            "id": "F001",
                            "reviewer": "qa",
                            "category": "correctness",
                            "severity": "nit",
                            "title": "Duplicate id",
                            "evidence": "Observed second issue.",
                            "required_action": "Use a different id.",
                            "status": "open",
                        },
                    ],
                )
            )

    def test_invalid_finding_file_paths_fail(self) -> None:
        invalid_paths = [
            "../src/apply.py",
            "/etc/passwd",
            r"C:\Users\x\file.py",
            "src/../secrets.txt",
            "   ",
        ]
        for invalid_path in invalid_paths:
            with self.subTest(file=invalid_path):
                with self.assertRaisesRegex(ValueError, "file"):
                    ReviewFindingsReport.model_validate(
                        make_findings_payload(
                            run_id="run_123",
                            findings=[
                                {
                                    "id": "F001",
                                    "reviewer": "qa",
                                    "category": "qa",
                                    "severity": "minor",
                                    "title": "Unsafe location",
                                    "evidence": "The file path is unsafe.",
                                    "required_action": "Use a safe relative path.",
                                    "file": invalid_path,
                                    "status": "open",
                                }
                            ],
                        )
                    )

    def test_overall_decision_cannot_be_pass_when_blocking_open_exists(self) -> None:
        with self.assertRaisesRegex(ValueError, "overall_decision cannot be pass"):
            ReviewFindingsReport.model_validate(
                make_findings_payload(
                    run_id="run_123",
                    overall_decision="pass",
                    findings=[
                        {
                            "id": "F001",
                            "reviewer": "qa",
                            "category": "qa",
                            "severity": "major",
                            "title": "Blocking finding",
                            "evidence": "Still open.",
                            "required_action": "Fix before approval.",
                            "status": "open",
                        }
                    ],
                )
            )


class RecordFindingsCommandTests(unittest.TestCase):
    def test_record_findings_writes_json_markdown_and_updates_state(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            run_dir, run_id = make_approved_source_run(runs_dir)
            findings_file = write_findings_file(
                tmp / "review_findings.json",
                make_findings_payload(
                    run_id=run_id,
                    overall_decision="needs_rework",
                    summary="QA review found a missing regression test.",
                    findings=[
                        {
                            "id": "F001",
                            "reviewer": "qa",
                            "category": "qa",
                            "severity": "major",
                            "title": "Missing regression test",
                            "evidence": "The change has no regression test.",
                            "required_action": "Add a regression test.",
                            "file": "src/ai_orchestrator/apply.py",
                            "status": "open",
                        }
                    ],
                ),
            )
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = record_findings_main(
                    [run_id, "--runs-dir", str(runs_dir), "--findings-file", str(findings_file)]
                )
            output = stdout.getvalue()
            state = RunState.model_validate_json((run_dir / "state.json").read_text(encoding="utf-8"))
            findings_json = run_dir / "REVIEW_FINDINGS.json"
            findings_md = run_dir / "REVIEW_FINDINGS.md"
            findings_json_exists = findings_json.exists()
            findings_md_exists = findings_md.exists()
            findings_md_text = findings_md.read_text(encoding="utf-8")
            findings_json_path = findings_json.resolve()

        self.assertEqual(exit_code, 0, output)
        self.assertEqual(output_value(output, "status"), "findings_recorded")
        self.assertEqual(output_value(output, "overall_decision"), "needs_rework")
        self.assertEqual(output_value(output, "blocking_findings"), "1")
        self.assertTrue(findings_json_exists)
        self.assertTrue(findings_md_exists)
        self.assertEqual(state.review_findings_path, str(findings_json_path))
        self.assertEqual(state.review_findings_decision, "needs_rework")
        self.assertEqual(state.review_findings_blocking_count, 1)
        self.assertIsNotNone(state.review_findings_created_at)
        self.assertEqual(state.review_findings_source_kind, "manual")
        self.assertIsNone(state.review_findings_source_profile)
        self.assertIn("Missing regression test", findings_md_text)
        self.assertIn("Source profile: `(none)`", findings_md_text)
        self.assertIn("Source kind: `manual`", findings_md_text)

    def test_record_findings_with_profile_enforces_reviewer_category_and_source_metadata(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            run_dir, run_id = make_approved_source_run(runs_dir)
            findings_file = write_findings_file(
                tmp / "review_findings.json",
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
                            "evidence": "The change has no regression test.",
                            "required_action": "Add a regression test.",
                            "file": "src\\ai_orchestrator\\apply.py",
                            "status": "open",
                        }
                    ],
                ),
            )
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = record_findings_main(
                    [
                        run_id,
                        "--runs-dir",
                        str(runs_dir),
                        "--findings-file",
                        str(findings_file),
                        "--profile",
                        "qa",
                    ]
                )
            output = stdout.getvalue()
            state = RunState.model_validate_json((run_dir / "state.json").read_text(encoding="utf-8"))
            report = ReviewFindingsReport.model_validate_json((run_dir / "REVIEW_FINDINGS.json").read_text(encoding="utf-8"))
            markdown = (run_dir / "REVIEW_FINDINGS.md").read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0, output)
        self.assertEqual(output_value(output, "review_findings_source_profile"), "qa")
        self.assertEqual(output_value(output, "review_findings_source_kind"), "reviewer_profile")
        self.assertEqual(report.source_profile, "qa")
        self.assertEqual(report.source_kind, "reviewer_profile")
        self.assertEqual(report.findings[0].file, "src/ai_orchestrator/apply.py")
        self.assertEqual(state.review_findings_source_profile, "qa")
        self.assertEqual(state.review_findings_source_kind, "reviewer_profile")
        self.assertIn("Source profile: `qa`", markdown)
        self.assertIn("Source kind: `reviewer_profile`", markdown)

    def test_record_findings_with_profile_fails_on_wrong_reviewer(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            _run_dir, run_id = make_approved_source_run(runs_dir)
            findings_file = write_findings_file(
                tmp / "review_findings.json",
                make_findings_payload(
                    run_id=run_id,
                    overall_decision="needs_rework",
                    findings=[
                        {
                            "id": "F001",
                            "reviewer": "security",
                            "category": "qa",
                            "severity": "major",
                            "title": "Reviewer mismatch",
                            "evidence": "The reviewer id does not match the selected profile.",
                            "required_action": "Return findings for the selected profile only.",
                            "status": "open",
                        }
                    ],
                ),
            )
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = record_findings_main(
                    [
                        run_id,
                        "--runs-dir",
                        str(runs_dir),
                        "--findings-file",
                        str(findings_file),
                        "--profile",
                        "qa",
                    ]
                )
            output = stdout.getvalue()

        self.assertEqual(exit_code, 1, output)
        self.assertEqual(output_value(output, "status"), "failed")
        self.assertEqual(
            output_value(output, "error"),
            "finding reviewer does not match selected profile: expected qa, got security",
        )

    def test_record_findings_with_profile_fails_on_disallowed_category(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            _run_dir, run_id = make_approved_source_run(runs_dir)
            findings_file = write_findings_file(
                tmp / "review_findings.json",
                make_findings_payload(
                    run_id=run_id,
                    overall_decision="needs_rework",
                    findings=[
                        {
                            "id": "F001",
                            "reviewer": "qa",
                            "category": "security",
                            "severity": "major",
                            "title": "Category mismatch",
                            "evidence": "The category is outside the QA contract.",
                            "required_action": "Use a QA-compatible category.",
                            "status": "open",
                        }
                    ],
                ),
            )
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = record_findings_main(
                    [
                        run_id,
                        "--runs-dir",
                        str(runs_dir),
                        "--findings-file",
                        str(findings_file),
                        "--profile",
                        "qa",
                    ]
                )
            output = stdout.getvalue()

        self.assertEqual(exit_code, 1, output)
        self.assertEqual(output_value(output, "status"), "failed")
        self.assertEqual(
            output_value(output, "error"),
            "finding category is not allowed for profile qa: security",
        )

    def test_record_findings_fails_if_run_missing(self) -> None:
        with temporary_test_dir() as tmp:
            findings_file = write_findings_file(
                tmp / "review_findings.json",
                make_findings_payload(run_id="missing-run"),
            )
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = record_findings_main(
                    ["missing-run", "--runs-dir", str(tmp / ".runs"), "--findings-file", str(findings_file)]
                )
            output = stdout.getvalue()

        self.assertEqual(exit_code, 1, output)
        self.assertEqual(output_value(output, "status"), "failed")
        self.assertEqual(output_value(output, "error"), "run does not exist: missing-run")

    def test_record_findings_fails_if_run_id_mismatch(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            _run_dir, run_id = make_approved_source_run(runs_dir)
            findings_file = write_findings_file(
                tmp / "review_findings.json",
                make_findings_payload(run_id="different-run"),
            )
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = record_findings_main(
                    [run_id, "--runs-dir", str(runs_dir), "--findings-file", str(findings_file)]
                )
            output = stdout.getvalue()

        self.assertEqual(exit_code, 1, output)
        self.assertEqual(output_value(output, "status"), "failed")
        self.assertIn("findings report run_id mismatch", output_value(output, "error"))

    def test_record_findings_refuses_overwrite_without_force(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            _run_dir, run_id = make_approved_source_run(runs_dir)
            findings_file = write_findings_file(tmp / "review_findings.json", make_findings_payload(run_id=run_id))
            with redirect_stdout(StringIO()):
                first_exit = record_findings_main(
                    [run_id, "--runs-dir", str(runs_dir), "--findings-file", str(findings_file)]
                )
            stdout = StringIO()
            with redirect_stdout(stdout):
                second_exit = record_findings_main(
                    [run_id, "--runs-dir", str(runs_dir), "--findings-file", str(findings_file)]
                )
            output = stdout.getvalue()

        self.assertEqual(first_exit, 0)
        self.assertEqual(second_exit, 1, output)
        self.assertIn("Review findings already recorded. Pass --force to overwrite them.", output)

    def test_record_findings_overwrites_with_force(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            run_dir, run_id = make_approved_source_run(runs_dir)
            initial_file = write_findings_file(
                tmp / "review_findings_1.json",
                make_findings_payload(
                    run_id=run_id,
                    overall_decision="pass",
                    findings=[],
                ),
            )
            updated_file = write_findings_file(
                tmp / "review_findings_2.json",
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
                            "evidence": "Still missing.",
                            "required_action": "Add the test.",
                            "status": "open",
                        }
                    ],
                ),
            )
            with redirect_stdout(StringIO()):
                self.assertEqual(
                    record_findings_main([run_id, "--runs-dir", str(runs_dir), "--findings-file", str(initial_file)]),
                    0,
                )
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = record_findings_main(
                    [run_id, "--runs-dir", str(runs_dir), "--findings-file", str(updated_file), "--force"]
                )
            output = stdout.getvalue()
            payload = json.loads((run_dir / "REVIEW_FINDINGS.json").read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0, output)
        self.assertEqual(payload["overall_decision"], "needs_rework")
        self.assertEqual(payload["counts"]["blocking_open"], 1)


class ReviewRunFindingsGateTests(unittest.TestCase):
    def test_review_run_approved_fails_when_blocking_findings_exist(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            _run_dir, run_id = make_approved_source_run(runs_dir)
            findings_file = write_findings_file(
                tmp / "review_findings.json",
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
                            "evidence": "Still missing.",
                            "required_action": "Add the test.",
                            "status": "open",
                        }
                    ],
                ),
            )
            with redirect_stdout(StringIO()):
                self.assertEqual(
                    record_findings_main([run_id, "--runs-dir", str(runs_dir), "--findings-file", str(findings_file)]),
                    0,
                )
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = review_run_main([run_id, "--runs-dir", str(runs_dir), "--decision", "approved"])
            output = stdout.getvalue()

        self.assertEqual(exit_code, 1, output)
        self.assertEqual(output_value(output, "status"), "failed")
        self.assertIn(
            "run has open blocking review findings; resolve findings or record rejected review for rework",
            output_value(output, "error"),
        )

    def test_review_run_rejected_succeeds_when_blocking_findings_exist(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            run_dir, run_id = make_approved_source_run(runs_dir)
            findings_file = write_findings_file(
                tmp / "review_findings.json",
                make_findings_payload(
                    run_id=run_id,
                    overall_decision="blocked",
                    findings=[
                        {
                            "id": "F001",
                            "reviewer": "security",
                            "category": "security",
                            "severity": "critical",
                            "title": "Critical security issue",
                            "evidence": "A boundary is bypassed.",
                            "required_action": "Restore the boundary.",
                            "status": "open",
                        }
                    ],
                ),
            )
            feedback_path = tmp / "review_feedback.md"
            feedback_path.write_text("Reviewer rejected the run.\n", encoding="utf-8")
            with redirect_stdout(StringIO()):
                self.assertEqual(
                    record_findings_main([run_id, "--runs-dir", str(runs_dir), "--findings-file", str(findings_file)]),
                    0,
                )
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = review_run_main(
                    [run_id, "--runs-dir", str(runs_dir), "--decision", "rejected", "--feedback", str(feedback_path)]
                )
            output = stdout.getvalue()
            state = RunState.model_validate_json((run_dir / "state.json").read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0, output)
        self.assertEqual(output_value(output, "status"), "review_recorded")
        self.assertEqual(state.human_review_decision, "rejected")

    def test_review_run_approved_succeeds_when_only_minor_findings_exist(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            _run_dir, run_id = make_approved_source_run(runs_dir)
            findings_file = write_findings_file(
                tmp / "review_findings.json",
                make_findings_payload(
                    run_id=run_id,
                    overall_decision="pass",
                    findings=[
                        {
                            "id": "F001",
                            "reviewer": "docs",
                            "category": "documentation",
                            "severity": "minor",
                            "title": "Doc improvement",
                            "evidence": "The docs could be clearer.",
                            "required_action": "Clarify wording.",
                            "status": "open",
                        }
                    ],
                ),
            )
            with redirect_stdout(StringIO()):
                self.assertEqual(
                    record_findings_main([run_id, "--runs-dir", str(runs_dir), "--findings-file", str(findings_file)]),
                    0,
                )
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = review_run_main([run_id, "--runs-dir", str(runs_dir), "--decision", "approved"])
            output = stdout.getvalue()

        self.assertEqual(exit_code, 0, output)
        self.assertEqual(output_value(output, "status"), "review_recorded")
        self.assertEqual(output_value(output, "decision"), "approved")

    def test_review_run_approved_succeeds_when_only_accepted_risk_findings_exist(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            _run_dir, run_id = make_approved_source_run(runs_dir)
            findings_file = write_findings_file(
                tmp / "review_findings.json",
                make_findings_payload(
                    run_id=run_id,
                    overall_decision="pass",
                    findings=[
                        {
                            "id": "F001",
                            "reviewer": "security",
                            "category": "security",
                            "severity": "major",
                            "title": "Accepted risk decision",
                            "evidence": "The risk was reviewed and explicitly accepted.",
                            "required_action": "Monitor the accepted risk.",
                            "status": "accepted_risk",
                        }
                    ],
                ),
            )
            with redirect_stdout(StringIO()):
                self.assertEqual(
                    record_findings_main([run_id, "--runs-dir", str(runs_dir), "--findings-file", str(findings_file)]),
                    0,
                )
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = review_run_main([run_id, "--runs-dir", str(runs_dir), "--decision", "approved"])
            output = stdout.getvalue()

        self.assertEqual(exit_code, 0, output)
        self.assertEqual(output_value(output, "status"), "review_recorded")
        self.assertEqual(output_value(output, "decision"), "approved")

    def test_review_run_approved_succeeds_when_findings_absent(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            _run_dir, run_id = make_approved_source_run(runs_dir)
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = review_run_main([run_id, "--runs-dir", str(runs_dir), "--decision", "approved"])
            output = stdout.getvalue()

        self.assertEqual(exit_code, 0, output)
        self.assertEqual(output_value(output, "status"), "review_recorded")


class ShowStatusFindingsTests(unittest.TestCase):
    def test_show_run_displays_findings_and_findings_feedback_next_action(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            _run_dir, run_id = make_approved_source_run(runs_dir)
            findings_file = write_findings_file(
                tmp / "review_findings.json",
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
                            "evidence": "Still missing.",
                            "required_action": "Add the test.",
                            "status": "open",
                        }
                    ],
                ),
            )
            with redirect_stdout(StringIO()):
                self.assertEqual(
                    record_findings_main([run_id, "--runs-dir", str(runs_dir), "--findings-file", str(findings_file)]),
                    0,
                )
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = show_run_main([run_id, "--runs-dir", str(runs_dir), "--show-paths"])
            output = stdout.getvalue()

        self.assertEqual(exit_code, 0, output)
        self.assertEqual(output_value(output, "findings_exists"), "true")
        self.assertEqual(output_value(output, "review_findings_decision"), "needs_rework")
        self.assertEqual(output_value(output, "blocking_findings"), "1")
        self.assertEqual(output_value(output, "review_findings_source_profile"), "")
        self.assertEqual(output_value(output, "review_findings_source_kind"), "manual")
        self.assertEqual(output_value(output, "findings_feedback_exists"), "false")
        self.assertEqual(output_value(output, "findings_feedback_count"), "0")
        self.assertEqual(output_value(output, "next_action"), "findings_feedback")
        self.assertIn("review_findings=", output)
        self.assertIn("review_findings_markdown=", output)

    def test_show_pipeline_counts_findings_and_prefers_findings_feedback(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            _run_dir_a, run_id_a = make_approved_source_run(runs_dir)
            _run_dir_b, run_id_b = make_approved_source_run(runs_dir)
            findings_file = write_findings_file(
                tmp / "review_findings.json",
                make_findings_payload(
                    run_id=run_id_a,
                    overall_decision="needs_rework",
                    findings=[
                        {
                            "id": "F001",
                            "reviewer": "qa",
                            "category": "qa",
                            "severity": "major",
                            "title": "Missing regression test",
                            "evidence": "Still missing.",
                            "required_action": "Add the test.",
                            "status": "open",
                        }
                    ],
                ),
            )
            with redirect_stdout(StringIO()):
                self.assertEqual(
                    record_findings_main([run_id_a, "--runs-dir", str(runs_dir), "--findings-file", str(findings_file)]),
                    0,
                )
            create_pipeline_fixture(
                tmp,
                pipeline_id="pipeline_findings_1",
                tasks=[
                    build_pipeline_task_result("task-a", run_id_a, runs_dir, title="Task A"),
                    build_pipeline_task_result("task-b", run_id_b, runs_dir, title="Task B"),
                ],
            )
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = show_pipeline_main(["pipeline_findings_1", "--runs-dir", str(runs_dir)])
            output = stdout.getvalue()

        self.assertEqual(exit_code, 0, output)
        self.assertEqual(output_value(output, "tasks_with_findings"), "1")
        self.assertEqual(output_value(output, "tasks_with_blocking_findings"), "1")
        self.assertEqual(output_value(output, "tasks_waiting_findings_feedback"), "1")
        self.assertEqual(output_value(output, "next_action"), "findings_feedback")
        self.assertIn("findings_exists=true", output)
        self.assertIn("blocking_findings=1", output)


if __name__ == "__main__":
    unittest.main()
