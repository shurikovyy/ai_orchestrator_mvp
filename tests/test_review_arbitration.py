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

from ai_orchestrator.apply import load_run_state
from ai_orchestrator.backends.mock import MockBackend
from ai_orchestrator.cli import (
    record_arbitration_main,
    record_findings_main,
    review_run_main,
    show_pipeline_main,
    show_run_main,
)
from ai_orchestrator.engine import TaskExecutionEngine
from ai_orchestrator.pipeline import PipelineSelectedTask, PipelineState, PipelineTaskResult
from ai_orchestrator.review_arbitration import compute_file_sha256, is_arbitration_stale, load_arbitration_file
from ai_orchestrator.schemas import ReviewArbitrationReport, TaskSpec

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


def write_json_file(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def make_findings_payload(
    *,
    run_id: str,
    overall_decision: str = "pass",
    summary: str = "Structured review findings.",
    findings: list[dict] | None = None,
) -> dict:
    return {
        "schema_version": "1.0",
        "run_id": run_id,
        "created_at": "2026-05-23T08:00:00+00:00",
        "summary": summary,
        "overall_decision": overall_decision,
        "findings": findings or [],
    }


def make_arbitration_payload(
    *,
    run_id: str,
    overall_decision: str = "pass",
    summary: str = "Manual arbitration summary.",
    arbitrated_findings: list[dict] | None = None,
    arbiter: str = "manual",
    source_findings_path: str | None = None,
) -> dict:
    payload = {
        "schema_version": "1.0",
        "run_id": run_id,
        "created_at": "2026-05-23T09:00:00+00:00",
        "source_findings_path": source_findings_path,
        "arbiter": arbiter,
        "summary": summary,
        "overall_decision": overall_decision,
        "arbitrated_findings": arbitrated_findings or [],
    }
    return payload


def make_approved_source_run(runs_dir: Path, *, task: TaskSpec | None = None) -> tuple[Path, str]:
    source_task = task or TaskSpec(
        description="Create deterministic demo artifact",
        acceptance_criteria=["deterministic demo artifact"],
        max_retries=1,
    )
    state = TaskExecutionEngine(MockBackend(), runs_dir).run(source_task)
    return runs_dir / state.run_id, state.run_id


def record_findings_for_run(runs_dir: Path, run_id: str, payload: dict, *, force: bool = False) -> None:
    with temporary_test_dir() as tmp:
        findings_file = write_json_file(tmp / "findings.json", payload)
        args = [run_id, "--runs-dir", str(runs_dir), "--findings-file", str(findings_file)]
        if force:
            args.append("--force")
        with redirect_stdout(StringIO()):
            exit_code = record_findings_main(args)
        if exit_code != 0:
            raise AssertionError("failed to record findings fixture")


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
        selected_tasks=selected_tasks
        or [PipelineSelectedTask(task_id=task.task_id, title=task.title, enabled=True) for task in tasks],
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


def make_blocking_qa_finding() -> dict:
    return {
        "id": "F001",
        "reviewer": "qa",
        "category": "qa",
        "severity": "major",
        "title": "Missing regression test",
        "evidence": "No regression test was added for the changed behavior.",
        "required_action": "Add a regression test.",
        "status": "open",
    }


def make_blocking_deterministic_finding() -> dict:
    return {
        "id": "F001",
        "reviewer": "deterministic",
        "category": "security",
        "severity": "critical",
        "title": "Deterministic hard gate",
        "evidence": "A hard gate was bypassed.",
        "required_action": "Restore the hard gate.",
        "status": "open",
    }


class ReviewArbitrationSchemaTests(unittest.TestCase):
    def test_valid_arbitration_report_loads(self) -> None:
        with temporary_test_dir() as tmp:
            arbitration_file = write_json_file(
                tmp / "arbitration.json",
                make_arbitration_payload(
                    run_id="run_123",
                    overall_decision="pass",
                    arbitrated_findings=[
                        {
                            "finding_id": "F001",
                            "source_reviewer": "qa",
                            "original_severity": "major",
                            "final_severity": "minor",
                            "original_blocking": True,
                            "final_blocking": False,
                            "status": "downgraded",
                            "reason": "The issue is real but non-blocking.",
                            "final_required_action": None,
                        }
                    ],
                ),
            )
            report = load_arbitration_file(arbitration_file)

        self.assertIsInstance(report, ReviewArbitrationReport)
        self.assertEqual(report.run_id, "run_123")
        self.assertEqual(report.counts.total, 1)

    def test_duplicate_finding_ids_fail(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicate arbitrated finding_id: F001"):
            ReviewArbitrationReport.model_validate(
                make_arbitration_payload(
                    run_id="run_123",
                    overall_decision="pass",
                    arbitrated_findings=[
                        {
                            "finding_id": "F001",
                            "source_reviewer": "qa",
                            "original_severity": "major",
                            "final_severity": "minor",
                            "original_blocking": True,
                            "final_blocking": False,
                            "status": "downgraded",
                            "reason": "non-blocking",
                            "final_required_action": None,
                        },
                        {
                            "finding_id": "F001",
                            "source_reviewer": "architecture",
                            "original_severity": "minor",
                            "final_severity": "minor",
                            "original_blocking": False,
                            "final_blocking": False,
                            "status": "upheld",
                            "reason": "same finding id reused",
                            "final_required_action": None,
                        },
                    ],
                )
            )

    def test_downgraded_requires_lower_final_severity(self) -> None:
        with self.assertRaisesRegex(ValueError, "status=downgraded requires final_severity lower than original_severity"):
            ReviewArbitrationReport.model_validate(
                make_arbitration_payload(
                    run_id="run_123",
                    overall_decision="pass",
                    arbitrated_findings=[
                        {
                            "finding_id": "F001",
                            "source_reviewer": "qa",
                            "original_severity": "major",
                            "final_severity": "major",
                            "original_blocking": True,
                            "final_blocking": True,
                            "status": "downgraded",
                            "reason": "invalid downgrade",
                            "final_required_action": "Fix it.",
                        }
                    ],
                )
            )

    def test_upgraded_requires_higher_final_severity(self) -> None:
        with self.assertRaisesRegex(ValueError, "status=upgraded requires final_severity higher than original_severity"):
            ReviewArbitrationReport.model_validate(
                make_arbitration_payload(
                    run_id="run_123",
                    overall_decision="needs_rework",
                    arbitrated_findings=[
                        {
                            "finding_id": "F001",
                            "source_reviewer": "qa",
                            "original_severity": "minor",
                            "final_severity": "minor",
                            "original_blocking": False,
                            "final_blocking": True,
                            "status": "upgraded",
                            "reason": "invalid upgrade",
                            "final_required_action": "Treat as blocking.",
                        }
                    ],
                )
            )

    def test_dismissed_implies_final_blocking_false(self) -> None:
        with self.assertRaisesRegex(ValueError, "status=dismissed implies final_blocking=false"):
            ReviewArbitrationReport.model_validate(
                make_arbitration_payload(
                    run_id="run_123",
                    overall_decision="needs_rework",
                    arbitrated_findings=[
                        {
                            "finding_id": "F001",
                            "source_reviewer": "qa",
                            "original_severity": "major",
                            "final_severity": "nit",
                            "original_blocking": True,
                            "final_blocking": True,
                            "status": "dismissed",
                            "reason": "invalid dismissed state",
                            "final_required_action": "Still fix it.",
                        }
                    ],
                )
            )

    def test_final_blocking_requires_final_required_action(self) -> None:
        with self.assertRaisesRegex(ValueError, "final_required_action is required when final_blocking is true"):
            ReviewArbitrationReport.model_validate(
                make_arbitration_payload(
                    run_id="run_123",
                    overall_decision="needs_rework",
                    arbitrated_findings=[
                        {
                            "finding_id": "F001",
                            "source_reviewer": "qa",
                            "original_severity": "major",
                            "final_severity": "major",
                            "original_blocking": True,
                            "final_blocking": True,
                            "status": "upheld",
                            "reason": "Still blocking.",
                            "final_required_action": None,
                        }
                    ],
                )
            )

    def test_deterministic_hard_gate_cannot_be_dismissed(self) -> None:
        with self.assertRaisesRegex(ValueError, "deterministic_hard_gate findings cannot be dismissed"):
            ReviewArbitrationReport.model_validate(
                make_arbitration_payload(
                    run_id="run_123",
                    overall_decision="needs_rework",
                    arbitrated_findings=[
                        {
                            "finding_id": "F001",
                            "source_reviewer": "deterministic",
                            "original_severity": "critical",
                            "final_severity": "minor",
                            "original_blocking": True,
                            "final_blocking": False,
                            "status": "dismissed",
                            "reason": "invalid dismissal",
                            "final_required_action": None,
                            "deterministic_hard_gate": True,
                        }
                    ],
                )
            )

    def test_deterministic_hard_gate_cannot_be_downgraded_from_critical_or_major(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "deterministic_hard_gate findings cannot be downgraded below the original critical/major severity",
        ):
            ReviewArbitrationReport.model_validate(
                make_arbitration_payload(
                    run_id="run_123",
                    overall_decision="blocked",
                    arbitrated_findings=[
                        {
                            "finding_id": "F001",
                            "source_reviewer": "deterministic",
                            "original_severity": "major",
                            "final_severity": "minor",
                            "original_blocking": True,
                            "final_blocking": True,
                            "status": "downgraded",
                            "reason": "invalid downgrade",
                            "final_required_action": "Keep blocking.",
                            "deterministic_hard_gate": True,
                        }
                    ],
                )
            )

    def test_accepted_risk_for_critical_or_major_requires_human_escalation_required(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "accepted_risk for critical/major original severity requires human_escalation_required=true",
        ):
            ReviewArbitrationReport.model_validate(
                make_arbitration_payload(
                    run_id="run_123",
                    overall_decision="needs_rework",
                    arbitrated_findings=[
                        {
                            "finding_id": "F001",
                            "source_reviewer": "security",
                            "original_severity": "critical",
                            "final_severity": "critical",
                            "original_blocking": True,
                            "final_blocking": False,
                            "status": "accepted_risk",
                            "reason": "invalid accepted risk without escalation",
                            "final_required_action": None,
                            "human_escalation_required": False,
                        }
                    ],
                )
            )


class RecordArbitrationTests(unittest.TestCase):
    def test_record_arbitration_writes_json_and_markdown_and_updates_state(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            run_dir, run_id = make_approved_source_run(runs_dir)
            record_findings_for_run(
                runs_dir,
                run_id,
                make_findings_payload(run_id=run_id, overall_decision="needs_rework", findings=[make_blocking_qa_finding()]),
            )
            arbitration_file = write_json_file(
                tmp / "arbitration.json",
                make_arbitration_payload(
                    run_id=run_id,
                    overall_decision="pass",
                    arbitrated_findings=[
                        {
                            "finding_id": "F001",
                            "source_reviewer": "qa",
                            "original_severity": "major",
                            "final_severity": "minor",
                            "original_blocking": True,
                            "final_blocking": False,
                            "status": "downgraded",
                            "reason": "The missing test is real but non-blocking for this run.",
                            "final_required_action": None,
                        }
                    ],
                ),
            )
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = record_arbitration_main(
                    [run_id, "--runs-dir", str(runs_dir), "--arbitration-file", str(arbitration_file)]
                )
            output = stdout.getvalue()
            state = load_run_state(run_dir)
            arbitration_json_exists = (run_dir / "REVIEW_ARBITRATION.json").exists()
            arbitration_markdown_exists = (run_dir / "REVIEW_ARBITRATION.md").exists()
            findings_sha = compute_file_sha256(run_dir / "REVIEW_FINDINGS.json")
            arbitration_report = ReviewArbitrationReport.model_validate_json(
                (run_dir / "REVIEW_ARBITRATION.json").read_text(encoding="utf-8")
            )
            arbitration_markdown = (run_dir / "REVIEW_ARBITRATION.md").read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0, output)
        self.assertTrue(arbitration_json_exists)
        self.assertTrue(arbitration_markdown_exists)
        self.assertEqual(output_value(output, "status"), "arbitration_recorded")
        self.assertEqual(arbitration_report.source_findings_sha256, findings_sha)
        self.assertIsNotNone(arbitration_report.source_findings_updated_at)
        self.assertFalse(arbitration_report.arbitration_stale)
        self.assertIn(findings_sha, arbitration_markdown)
        self.assertEqual(state.review_arbitration_decision, "pass")
        self.assertEqual(state.review_arbitration_final_blocking_count, 0)
        self.assertFalse(state.review_arbitration_human_escalation_required)
        self.assertEqual(state.review_arbitration_source_findings_sha256, findings_sha)
        self.assertFalse(state.review_arbitration_stale)
        self.assertIsNotNone(state.review_arbitration_created_at)

    def test_is_arbitration_stale_false_immediately_after_record(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            run_dir, run_id = make_approved_source_run(runs_dir)
            record_findings_for_run(
                runs_dir,
                run_id,
                make_findings_payload(run_id=run_id, overall_decision="needs_rework", findings=[make_blocking_qa_finding()]),
            )
            arbitration_file = write_json_file(
                tmp / "arbitration.json",
                make_arbitration_payload(
                    run_id=run_id,
                    overall_decision="pass",
                    arbitrated_findings=[
                        {
                            "finding_id": "F001",
                            "source_reviewer": "qa",
                            "original_severity": "major",
                            "final_severity": "minor",
                            "original_blocking": True,
                            "final_blocking": False,
                            "status": "downgraded",
                            "reason": "Fresh arbitration.",
                            "final_required_action": None,
                        }
                    ],
                ),
            )
            with redirect_stdout(StringIO()):
                self.assertEqual(
                    record_arbitration_main([run_id, "--runs-dir", str(runs_dir), "--arbitration-file", str(arbitration_file)]),
                    0,
                )
            report = ReviewArbitrationReport.model_validate_json(
                (run_dir / "REVIEW_ARBITRATION.json").read_text(encoding="utf-8")
            )
            stale_result = is_arbitration_stale(run_dir, report)

        self.assertFalse(stale_result)

    def test_is_arbitration_stale_true_after_review_findings_are_replaced(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            run_dir, run_id = make_approved_source_run(runs_dir)
            record_findings_for_run(
                runs_dir,
                run_id,
                make_findings_payload(run_id=run_id, overall_decision="needs_rework", findings=[make_blocking_qa_finding()]),
            )
            arbitration_file = write_json_file(
                tmp / "arbitration.json",
                make_arbitration_payload(
                    run_id=run_id,
                    overall_decision="pass",
                    arbitrated_findings=[
                        {
                            "finding_id": "F001",
                            "source_reviewer": "qa",
                            "original_severity": "major",
                            "final_severity": "minor",
                            "original_blocking": True,
                            "final_blocking": False,
                            "status": "downgraded",
                            "reason": "Fresh arbitration.",
                            "final_required_action": None,
                        }
                    ],
                ),
            )
            with redirect_stdout(StringIO()):
                self.assertEqual(
                    record_arbitration_main([run_id, "--runs-dir", str(runs_dir), "--arbitration-file", str(arbitration_file)]),
                    0,
                )
            record_findings_for_run(
                runs_dir,
                run_id,
                make_findings_payload(
                    run_id=run_id,
                    overall_decision="needs_rework",
                    summary="Updated findings payload.",
                    findings=[
                        {
                            "id": "F001",
                            "reviewer": "qa",
                            "category": "qa",
                            "severity": "major",
                            "title": "Missing regression test",
                            "evidence": "No regression test was added and the evidence changed.",
                            "required_action": "Add a regression test.",
                            "status": "open",
                        }
                    ],
                ),
                force=True,
            )
            report = ReviewArbitrationReport.model_validate_json(
                (run_dir / "REVIEW_ARBITRATION.json").read_text(encoding="utf-8")
            )
            stale_result = is_arbitration_stale(run_dir, report)

        self.assertTrue(stale_result)

    def test_old_arbitration_report_without_source_hash_is_treated_as_stale(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            run_dir, run_id = make_approved_source_run(runs_dir)
            record_findings_for_run(
                runs_dir,
                run_id,
                make_findings_payload(run_id=run_id, overall_decision="needs_rework", findings=[make_blocking_qa_finding()]),
            )
            arbitration_file = write_json_file(
                tmp / "arbitration.json",
                make_arbitration_payload(
                    run_id=run_id,
                    overall_decision="pass",
                    arbitrated_findings=[
                        {
                            "finding_id": "F001",
                            "source_reviewer": "qa",
                            "original_severity": "major",
                            "final_severity": "minor",
                            "original_blocking": True,
                            "final_blocking": False,
                            "status": "downgraded",
                            "reason": "Fresh arbitration.",
                            "final_required_action": None,
                        }
                    ],
                ),
            )
            with redirect_stdout(StringIO()):
                self.assertEqual(
                    record_arbitration_main([run_id, "--runs-dir", str(runs_dir), "--arbitration-file", str(arbitration_file)]),
                    0,
                )
            report_path = run_dir / "REVIEW_ARBITRATION.json"
            payload = json.loads(report_path.read_text(encoding="utf-8"))
            payload.pop("source_findings_sha256", None)
            report_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            report = ReviewArbitrationReport.model_validate_json(report_path.read_text(encoding="utf-8"))
            stale_result = is_arbitration_stale(run_dir, report)

        self.assertTrue(stale_result)

    def test_record_arbitration_fails_if_run_missing(self) -> None:
        with temporary_test_dir() as tmp:
            arbitration_file = write_json_file(
                tmp / "arbitration.json",
                make_arbitration_payload(run_id="missing-run", overall_decision="pass"),
            )
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = record_arbitration_main(
                    ["missing-run", "--runs-dir", str(tmp / ".runs"), "--arbitration-file", str(arbitration_file)]
                )
            output = stdout.getvalue()

        self.assertEqual(exit_code, 1, output)
        self.assertEqual(output_value(output, "error"), "run does not exist: missing-run")

    def test_record_arbitration_fails_if_review_findings_missing(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            _run_dir, run_id = make_approved_source_run(runs_dir)
            arbitration_file = write_json_file(
                tmp / "arbitration.json",
                make_arbitration_payload(
                    run_id=run_id,
                    overall_decision="pass",
                    arbitrated_findings=[],
                ),
            )
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = record_arbitration_main(
                    [run_id, "--runs-dir", str(runs_dir), "--arbitration-file", str(arbitration_file)]
                )
            output = stdout.getvalue()

        self.assertEqual(exit_code, 1, output)
        self.assertEqual(
            output_value(output, "error"),
            "REVIEW_FINDINGS.json does not exist for this run; arbitration requires source findings",
        )

    def test_record_arbitration_fails_if_run_id_mismatch(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            _run_dir, run_id = make_approved_source_run(runs_dir)
            arbitration_file = write_json_file(
                tmp / "arbitration.json",
                make_arbitration_payload(run_id="other-run", overall_decision="pass"),
            )
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = record_arbitration_main(
                    [run_id, "--runs-dir", str(runs_dir), "--arbitration-file", str(arbitration_file)]
                )
            output = stdout.getvalue()

        self.assertEqual(exit_code, 1, output)
        self.assertEqual(
            output_value(output, "error"),
            f"arbitration report run_id mismatch: expected {run_id}, got other-run",
        )

    def test_record_arbitration_refuses_overwrite_without_force(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            _run_dir, run_id = make_approved_source_run(runs_dir)
            record_findings_for_run(
                runs_dir,
                run_id,
                make_findings_payload(run_id=run_id, overall_decision="needs_rework", findings=[make_blocking_qa_finding()]),
            )
            arbitration_file = write_json_file(
                tmp / "arbitration.json",
                make_arbitration_payload(
                    run_id=run_id,
                    overall_decision="pass",
                    arbitrated_findings=[
                        {
                            "finding_id": "F001",
                            "source_reviewer": "qa",
                            "original_severity": "major",
                            "final_severity": "minor",
                            "original_blocking": True,
                            "final_blocking": False,
                            "status": "downgraded",
                            "reason": "downgraded once",
                            "final_required_action": None,
                        }
                    ],
                ),
            )
            with redirect_stdout(StringIO()):
                self.assertEqual(
                    record_arbitration_main([run_id, "--runs-dir", str(runs_dir), "--arbitration-file", str(arbitration_file)]),
                    0,
                )
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = record_arbitration_main(
                    [run_id, "--runs-dir", str(runs_dir), "--arbitration-file", str(arbitration_file)]
                )
            output = stdout.getvalue()

        self.assertEqual(exit_code, 1, output)
        self.assertEqual(
            output_value(output, "error"),
            "Review arbitration already recorded. Pass --force to overwrite it.",
        )

    def test_record_arbitration_overwrite_with_force_works(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            run_dir, run_id = make_approved_source_run(runs_dir)
            record_findings_for_run(
                runs_dir,
                run_id,
                make_findings_payload(run_id=run_id, overall_decision="needs_rework", findings=[make_blocking_qa_finding()]),
            )
            first_file = write_json_file(
                tmp / "arbitration_1.json",
                make_arbitration_payload(
                    run_id=run_id,
                    overall_decision="pass",
                    summary="First summary.",
                    arbitrated_findings=[
                        {
                            "finding_id": "F001",
                            "source_reviewer": "qa",
                            "original_severity": "major",
                            "final_severity": "minor",
                            "original_blocking": True,
                            "final_blocking": False,
                            "status": "downgraded",
                            "reason": "first",
                            "final_required_action": None,
                        }
                    ],
                ),
            )
            second_file = write_json_file(
                tmp / "arbitration_2.json",
                make_arbitration_payload(
                    run_id=run_id,
                    overall_decision="needs_rework",
                    summary="Second summary.",
                    arbitrated_findings=[
                        {
                            "finding_id": "F001",
                            "source_reviewer": "qa",
                            "original_severity": "major",
                            "final_severity": "major",
                            "original_blocking": True,
                            "final_blocking": True,
                            "status": "upheld",
                            "reason": "second",
                            "final_required_action": "Still blocking.",
                        }
                    ],
                ),
            )
            with redirect_stdout(StringIO()):
                self.assertEqual(
                    record_arbitration_main([run_id, "--runs-dir", str(runs_dir), "--arbitration-file", str(first_file)]),
                    0,
                )
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = record_arbitration_main(
                    [run_id, "--runs-dir", str(runs_dir), "--arbitration-file", str(second_file), "--force"]
                )
            output = stdout.getvalue()
            report = ReviewArbitrationReport.model_validate_json(
                (run_dir / "REVIEW_ARBITRATION.json").read_text(encoding="utf-8")
            )

        self.assertEqual(exit_code, 0, output)
        self.assertEqual(output_value(output, "overall_decision"), "needs_rework")
        self.assertEqual(report.summary, "Second summary.")
        self.assertEqual(report.overall_decision, "needs_rework")

    def test_record_arbitration_fails_if_arbitrated_finding_id_not_in_review_findings(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            _run_dir, run_id = make_approved_source_run(runs_dir)
            record_findings_for_run(
                runs_dir,
                run_id,
                make_findings_payload(run_id=run_id, overall_decision="needs_rework", findings=[make_blocking_qa_finding()]),
            )
            arbitration_file = write_json_file(
                tmp / "arbitration.json",
                make_arbitration_payload(
                    run_id=run_id,
                    overall_decision="pass",
                    arbitrated_findings=[
                        {
                            "finding_id": "F999",
                            "source_reviewer": "qa",
                            "original_severity": "major",
                            "final_severity": "minor",
                            "original_blocking": True,
                            "final_blocking": False,
                            "status": "downgraded",
                            "reason": "unknown id",
                            "final_required_action": None,
                        }
                    ],
                ),
            )
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = record_arbitration_main(
                    [run_id, "--runs-dir", str(runs_dir), "--arbitration-file", str(arbitration_file)]
                )
            output = stdout.getvalue()

        self.assertEqual(exit_code, 1, output)
        self.assertEqual(output_value(output, "error"), "arbitrated finding_id not found in REVIEW_FINDINGS: F999")

    def test_record_arbitration_fails_if_source_reviewer_mismatch(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            _run_dir, run_id = make_approved_source_run(runs_dir)
            record_findings_for_run(
                runs_dir,
                run_id,
                make_findings_payload(run_id=run_id, overall_decision="needs_rework", findings=[make_blocking_qa_finding()]),
            )
            arbitration_file = write_json_file(
                tmp / "arbitration.json",
                make_arbitration_payload(
                    run_id=run_id,
                    overall_decision="pass",
                    arbitrated_findings=[
                        {
                            "finding_id": "F001",
                            "source_reviewer": "security",
                            "original_severity": "major",
                            "final_severity": "minor",
                            "original_blocking": True,
                            "final_blocking": False,
                            "status": "downgraded",
                            "reason": "wrong reviewer",
                            "final_required_action": None,
                        }
                    ],
                ),
            )
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = record_arbitration_main(
                    [run_id, "--runs-dir", str(runs_dir), "--arbitration-file", str(arbitration_file)]
                )
            output = stdout.getvalue()

        self.assertEqual(exit_code, 1, output)
        self.assertEqual(
            output_value(output, "error"),
            "source reviewer mismatch for arbitrated finding F001: expected qa, got security",
        )

    def test_record_arbitration_fails_if_original_severity_mismatch(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            _run_dir, run_id = make_approved_source_run(runs_dir)
            record_findings_for_run(
                runs_dir,
                run_id,
                make_findings_payload(run_id=run_id, overall_decision="needs_rework", findings=[make_blocking_qa_finding()]),
            )
            arbitration_file = write_json_file(
                tmp / "arbitration.json",
                make_arbitration_payload(
                    run_id=run_id,
                    overall_decision="pass",
                    arbitrated_findings=[
                        {
                            "finding_id": "F001",
                            "source_reviewer": "qa",
                            "original_severity": "critical",
                            "final_severity": "minor",
                            "original_blocking": True,
                            "final_blocking": False,
                            "status": "downgraded",
                            "reason": "wrong severity",
                            "final_required_action": None,
                        }
                    ],
                ),
            )
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = record_arbitration_main(
                    [run_id, "--runs-dir", str(runs_dir), "--arbitration-file", str(arbitration_file)]
                )
            output = stdout.getvalue()

        self.assertEqual(exit_code, 1, output)
        self.assertEqual(
            output_value(output, "error"),
            "original severity mismatch for arbitrated finding F001: expected major, got critical",
        )

    def test_record_arbitration_fails_if_original_blocking_mismatch(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            _run_dir, run_id = make_approved_source_run(runs_dir)
            record_findings_for_run(
                runs_dir,
                run_id,
                make_findings_payload(run_id=run_id, overall_decision="needs_rework", findings=[make_blocking_qa_finding()]),
            )
            arbitration_file = write_json_file(
                tmp / "arbitration.json",
                make_arbitration_payload(
                    run_id=run_id,
                    overall_decision="pass",
                    arbitrated_findings=[
                        {
                            "finding_id": "F001",
                            "source_reviewer": "qa",
                            "original_severity": "major",
                            "final_severity": "minor",
                            "original_blocking": False,
                            "final_blocking": False,
                            "status": "downgraded",
                            "reason": "wrong blocking flag",
                            "final_required_action": None,
                        }
                    ],
                ),
            )
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = record_arbitration_main(
                    [run_id, "--runs-dir", str(runs_dir), "--arbitration-file", str(arbitration_file)]
                )
            output = stdout.getvalue()

        self.assertEqual(exit_code, 1, output)
        self.assertEqual(
            output_value(output, "error"),
            "original blocking mismatch for arbitrated finding F001: expected true, got false",
        )

    def test_record_arbitration_fails_if_deterministic_critical_or_major_lacks_hard_gate_flag(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            _run_dir, run_id = make_approved_source_run(runs_dir)
            record_findings_for_run(
                runs_dir,
                run_id,
                make_findings_payload(run_id=run_id, overall_decision="blocked", findings=[make_blocking_deterministic_finding()]),
            )
            arbitration_file = write_json_file(
                tmp / "arbitration.json",
                make_arbitration_payload(
                    run_id=run_id,
                    overall_decision="blocked",
                    arbitrated_findings=[
                        {
                            "finding_id": "F001",
                            "source_reviewer": "deterministic",
                            "original_severity": "critical",
                            "final_severity": "critical",
                            "original_blocking": True,
                            "final_blocking": True,
                            "status": "upheld",
                            "reason": "still blocking",
                            "final_required_action": "Restore the hard gate.",
                            "deterministic_hard_gate": False,
                        }
                    ],
                ),
            )
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = record_arbitration_main(
                    [run_id, "--runs-dir", str(runs_dir), "--arbitration-file", str(arbitration_file)]
                )
            output = stdout.getvalue()

        self.assertEqual(exit_code, 1, output)
        self.assertEqual(
            output_value(output, "error"),
            "deterministic critical/major findings must set deterministic_hard_gate=true: F001",
        )


class ReviewRunArbitrationGateTests(unittest.TestCase):
    def test_raw_blocking_findings_without_arbitration_still_block_approved_review(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            _run_dir, run_id = make_approved_source_run(runs_dir)
            record_findings_for_run(
                runs_dir,
                run_id,
                make_findings_payload(run_id=run_id, overall_decision="needs_rework", findings=[make_blocking_qa_finding()]),
            )
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = review_run_main([run_id, "--runs-dir", str(runs_dir), "--decision", "approved"])
            output = stdout.getvalue()

        self.assertEqual(exit_code, 1, output)
        self.assertEqual(
            output_value(output, "error"),
            "run has open blocking review findings; resolve findings or record rejected review for rework",
        )

    def test_arbitration_pass_with_no_final_blocking_allows_review_run_approved(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            _run_dir, run_id = make_approved_source_run(runs_dir)
            record_findings_for_run(
                runs_dir,
                run_id,
                make_findings_payload(run_id=run_id, overall_decision="needs_rework", findings=[make_blocking_qa_finding()]),
            )
            arbitration_file = write_json_file(
                tmp / "arbitration.json",
                make_arbitration_payload(
                    run_id=run_id,
                    overall_decision="pass",
                    arbitrated_findings=[
                        {
                            "finding_id": "F001",
                            "source_reviewer": "qa",
                            "original_severity": "major",
                            "final_severity": "minor",
                            "original_blocking": True,
                            "final_blocking": False,
                            "status": "downgraded",
                            "reason": "Non-blocking after arbitration.",
                            "final_required_action": None,
                        }
                    ],
                ),
            )
            with redirect_stdout(StringIO()):
                self.assertEqual(
                    record_arbitration_main([run_id, "--runs-dir", str(runs_dir), "--arbitration-file", str(arbitration_file)]),
                    0,
                )
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = review_run_main([run_id, "--runs-dir", str(runs_dir), "--decision", "approved"])
            output = stdout.getvalue()

        self.assertEqual(exit_code, 0, output)
        self.assertEqual(output_value(output, "status"), "review_recorded")
        self.assertEqual(output_value(output, "decision"), "approved")

    def test_review_run_approved_fails_with_stale_arbitration_even_if_decision_is_pass(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            _run_dir, run_id = make_approved_source_run(runs_dir)
            record_findings_for_run(
                runs_dir,
                run_id,
                make_findings_payload(run_id=run_id, overall_decision="needs_rework", findings=[make_blocking_qa_finding()]),
            )
            arbitration_file = write_json_file(
                tmp / "arbitration.json",
                make_arbitration_payload(
                    run_id=run_id,
                    overall_decision="pass",
                    arbitrated_findings=[
                        {
                            "finding_id": "F001",
                            "source_reviewer": "qa",
                            "original_severity": "major",
                            "final_severity": "minor",
                            "original_blocking": True,
                            "final_blocking": False,
                            "status": "downgraded",
                            "reason": "Non-blocking after arbitration.",
                            "final_required_action": None,
                        }
                    ],
                ),
            )
            with redirect_stdout(StringIO()):
                self.assertEqual(
                    record_arbitration_main([run_id, "--runs-dir", str(runs_dir), "--arbitration-file", str(arbitration_file)]),
                    0,
                )
            record_findings_for_run(
                runs_dir,
                run_id,
                make_findings_payload(
                    run_id=run_id,
                    overall_decision="needs_rework",
                    summary="Updated findings after arbitration.",
                    findings=[
                        {
                            "id": "F001",
                            "reviewer": "qa",
                            "category": "qa",
                            "severity": "major",
                            "title": "Missing regression test",
                            "evidence": "Updated evidence after arbitration.",
                            "required_action": "Add a regression test.",
                            "status": "open",
                        }
                    ],
                ),
                force=True,
            )
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = review_run_main([run_id, "--runs-dir", str(runs_dir), "--decision", "approved"])
            output = stdout.getvalue()

        self.assertEqual(exit_code, 1, output)
        self.assertEqual(
            output_value(output, "error"),
            "run has stale review arbitration; re-run record-arbitration for current findings",
        )

    def test_arbitration_final_blocking_blocks_review_run_approved(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            _run_dir, run_id = make_approved_source_run(runs_dir)
            record_findings_for_run(
                runs_dir,
                run_id,
                make_findings_payload(run_id=run_id, overall_decision="needs_rework", findings=[make_blocking_qa_finding()]),
            )
            arbitration_file = write_json_file(
                tmp / "arbitration.json",
                make_arbitration_payload(
                    run_id=run_id,
                    overall_decision="needs_rework",
                    arbitrated_findings=[
                        {
                            "finding_id": "F001",
                            "source_reviewer": "qa",
                            "original_severity": "major",
                            "final_severity": "major",
                            "original_blocking": True,
                            "final_blocking": True,
                            "status": "upheld",
                            "reason": "Still blocking after arbitration.",
                            "final_required_action": "Add the regression test.",
                        }
                    ],
                ),
            )
            with redirect_stdout(StringIO()):
                self.assertEqual(
                    record_arbitration_main([run_id, "--runs-dir", str(runs_dir), "--arbitration-file", str(arbitration_file)]),
                    0,
                )
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = review_run_main([run_id, "--runs-dir", str(runs_dir), "--decision", "approved"])
            output = stdout.getvalue()

        self.assertEqual(exit_code, 1, output)
        self.assertEqual(
            output_value(output, "error"),
            "run has final blocking arbitration findings; record rejected review for rework",
        )

    def test_arbitration_human_escalation_blocks_review_run_approved(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            _run_dir, run_id = make_approved_source_run(runs_dir)
            record_findings_for_run(
                runs_dir,
                run_id,
                make_findings_payload(run_id=run_id, overall_decision="needs_rework", findings=[make_blocking_qa_finding()]),
            )
            arbitration_file = write_json_file(
                tmp / "arbitration.json",
                make_arbitration_payload(
                    run_id=run_id,
                    overall_decision="human_escalation",
                    arbitrated_findings=[
                        {
                            "finding_id": "F001",
                            "source_reviewer": "qa",
                            "original_severity": "major",
                            "final_severity": "major",
                            "original_blocking": True,
                            "final_blocking": False,
                            "status": "accepted_risk",
                            "reason": "Escalate accepted risk to a human decision.",
                            "final_required_action": None,
                            "human_escalation_required": True,
                        }
                    ],
                ),
            )
            with redirect_stdout(StringIO()):
                self.assertEqual(
                    record_arbitration_main([run_id, "--runs-dir", str(runs_dir), "--arbitration-file", str(arbitration_file)]),
                    0,
                )
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = review_run_main([run_id, "--runs-dir", str(runs_dir), "--decision", "approved"])
            output = stdout.getvalue()

        self.assertEqual(exit_code, 1, output)
        self.assertEqual(output_value(output, "error"), "run requires human escalation before approval")

    def test_review_run_rejected_still_allowed_with_arbitration_blocking(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            _run_dir, run_id = make_approved_source_run(runs_dir)
            record_findings_for_run(
                runs_dir,
                run_id,
                make_findings_payload(run_id=run_id, overall_decision="needs_rework", findings=[make_blocking_qa_finding()]),
            )
            arbitration_file = write_json_file(
                tmp / "arbitration.json",
                make_arbitration_payload(
                    run_id=run_id,
                    overall_decision="needs_rework",
                    arbitrated_findings=[
                        {
                            "finding_id": "F001",
                            "source_reviewer": "qa",
                            "original_severity": "major",
                            "final_severity": "major",
                            "original_blocking": True,
                            "final_blocking": True,
                            "status": "upheld",
                            "reason": "Still blocking after arbitration.",
                            "final_required_action": "Add the regression test.",
                        }
                    ],
                ),
            )
            feedback_path = tmp / "review_feedback.md"
            write_text(feedback_path, "Please fix the arbitration-confirmed blocking issue.\n")
            with redirect_stdout(StringIO()):
                self.assertEqual(
                    record_arbitration_main([run_id, "--runs-dir", str(runs_dir), "--arbitration-file", str(arbitration_file)]),
                    0,
                )
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = review_run_main(
                    [run_id, "--runs-dir", str(runs_dir), "--decision", "rejected", "--feedback", str(feedback_path)]
                )
            output = stdout.getvalue()

        self.assertEqual(exit_code, 0, output)
        self.assertEqual(output_value(output, "status"), "review_recorded")
        self.assertEqual(output_value(output, "decision"), "rejected")

    def test_review_run_rejected_still_succeeds_with_stale_arbitration(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            _run_dir, run_id = make_approved_source_run(runs_dir)
            record_findings_for_run(
                runs_dir,
                run_id,
                make_findings_payload(run_id=run_id, overall_decision="needs_rework", findings=[make_blocking_qa_finding()]),
            )
            arbitration_file = write_json_file(
                tmp / "arbitration.json",
                make_arbitration_payload(
                    run_id=run_id,
                    overall_decision="pass",
                    arbitrated_findings=[
                        {
                            "finding_id": "F001",
                            "source_reviewer": "qa",
                            "original_severity": "major",
                            "final_severity": "minor",
                            "original_blocking": True,
                            "final_blocking": False,
                            "status": "downgraded",
                            "reason": "Non-blocking after arbitration.",
                            "final_required_action": None,
                        }
                    ],
                ),
            )
            feedback_path = tmp / "review_feedback.md"
            write_text(feedback_path, "Rejecting the run until the current findings are re-arbitrated.\n")
            with redirect_stdout(StringIO()):
                self.assertEqual(
                    record_arbitration_main([run_id, "--runs-dir", str(runs_dir), "--arbitration-file", str(arbitration_file)]),
                    0,
                )
            record_findings_for_run(
                runs_dir,
                run_id,
                make_findings_payload(
                    run_id=run_id,
                    overall_decision="needs_rework",
                    summary="Updated findings after arbitration.",
                    findings=[
                        {
                            "id": "F001",
                            "reviewer": "qa",
                            "category": "qa",
                            "severity": "major",
                            "title": "Missing regression test",
                            "evidence": "Updated evidence after arbitration.",
                            "required_action": "Add a regression test.",
                            "status": "open",
                        }
                    ],
                ),
                force=True,
            )
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = review_run_main(
                    [run_id, "--runs-dir", str(runs_dir), "--decision", "rejected", "--feedback", str(feedback_path)]
                )
            output = stdout.getvalue()

        self.assertEqual(exit_code, 0, output)
        self.assertEqual(output_value(output, "status"), "review_recorded")
        self.assertEqual(output_value(output, "decision"), "rejected")


class ShowStatusArbitrationTests(unittest.TestCase):
    def test_show_run_displays_arbitration_fields(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            _run_dir, run_id = make_approved_source_run(runs_dir)
            record_findings_for_run(
                runs_dir,
                run_id,
                make_findings_payload(run_id=run_id, overall_decision="needs_rework", findings=[make_blocking_qa_finding()]),
            )
            arbitration_file = write_json_file(
                tmp / "arbitration.json",
                make_arbitration_payload(
                    run_id=run_id,
                    overall_decision="pass",
                    arbitrated_findings=[
                        {
                            "finding_id": "F001",
                            "source_reviewer": "qa",
                            "original_severity": "major",
                            "final_severity": "minor",
                            "original_blocking": True,
                            "final_blocking": False,
                            "status": "downgraded",
                            "reason": "Visible but not blocking.",
                            "final_required_action": None,
                        }
                    ],
                ),
            )
            with redirect_stdout(StringIO()):
                self.assertEqual(
                    record_arbitration_main([run_id, "--runs-dir", str(runs_dir), "--arbitration-file", str(arbitration_file)]),
                    0,
                )
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = show_run_main([run_id, "--runs-dir", str(runs_dir), "--show-paths"])
            output = stdout.getvalue()

        self.assertEqual(exit_code, 0, output)
        self.assertEqual(output_value(output, "arbitration_exists"), "true")
        self.assertEqual(output_value(output, "review_arbitration_decision"), "pass")
        self.assertEqual(output_value(output, "arbitration_final_blocking"), "0")
        self.assertEqual(output_value(output, "arbitration_human_escalation_required"), "false")
        self.assertEqual(output_value(output, "arbitration_stale"), "false")
        self.assertTrue(output_value(output, "review_arbitration_source_findings_sha256"))
        self.assertIn("review_arbitration=", output)
        self.assertIn("review_arbitration_markdown=", output)

    def test_show_run_next_action_arbitrate_findings_when_blocking_findings_exist_without_arbitration(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            _run_dir, run_id = make_approved_source_run(runs_dir)
            record_findings_for_run(
                runs_dir,
                run_id,
                make_findings_payload(run_id=run_id, overall_decision="needs_rework", findings=[make_blocking_qa_finding()]),
            )
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = show_run_main([run_id, "--runs-dir", str(runs_dir)])
            output = stdout.getvalue()

        self.assertEqual(exit_code, 0, output)
        self.assertEqual(output_value(output, "next_action"), "arbitrate_findings")

    def test_show_run_next_action_human_escalation_when_arbitration_requires_it(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            _run_dir, run_id = make_approved_source_run(runs_dir)
            record_findings_for_run(
                runs_dir,
                run_id,
                make_findings_payload(run_id=run_id, overall_decision="needs_rework", findings=[make_blocking_qa_finding()]),
            )
            arbitration_file = write_json_file(
                tmp / "arbitration.json",
                make_arbitration_payload(
                    run_id=run_id,
                    overall_decision="human_escalation",
                    arbitrated_findings=[
                        {
                            "finding_id": "F001",
                            "source_reviewer": "qa",
                            "original_severity": "major",
                            "final_severity": "major",
                            "original_blocking": True,
                            "final_blocking": False,
                            "status": "accepted_risk",
                            "reason": "Escalate to a human owner.",
                            "final_required_action": None,
                            "human_escalation_required": True,
                        }
                    ],
                ),
            )
            with redirect_stdout(StringIO()):
                self.assertEqual(
                    record_arbitration_main([run_id, "--runs-dir", str(runs_dir), "--arbitration-file", str(arbitration_file)]),
                    0,
                )
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = show_run_main([run_id, "--runs-dir", str(runs_dir)])
            output = stdout.getvalue()

        self.assertEqual(exit_code, 0, output)
        self.assertEqual(output_value(output, "next_action"), "human_escalation")

    def test_show_run_displays_stale_arbitration_after_findings_change(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            _run_dir, run_id = make_approved_source_run(runs_dir)
            record_findings_for_run(
                runs_dir,
                run_id,
                make_findings_payload(run_id=run_id, overall_decision="needs_rework", findings=[make_blocking_qa_finding()]),
            )
            arbitration_file = write_json_file(
                tmp / "arbitration.json",
                make_arbitration_payload(
                    run_id=run_id,
                    overall_decision="pass",
                    arbitrated_findings=[
                        {
                            "finding_id": "F001",
                            "source_reviewer": "qa",
                            "original_severity": "major",
                            "final_severity": "minor",
                            "original_blocking": True,
                            "final_blocking": False,
                            "status": "downgraded",
                            "reason": "Non-blocking after arbitration.",
                            "final_required_action": None,
                        }
                    ],
                ),
            )
            with redirect_stdout(StringIO()):
                self.assertEqual(
                    record_arbitration_main([run_id, "--runs-dir", str(runs_dir), "--arbitration-file", str(arbitration_file)]),
                    0,
                )
            record_findings_for_run(
                runs_dir,
                run_id,
                make_findings_payload(
                    run_id=run_id,
                    overall_decision="needs_rework",
                    summary="Updated findings after arbitration.",
                    findings=[
                        {
                            "id": "F001",
                            "reviewer": "qa",
                            "category": "qa",
                            "severity": "major",
                            "title": "Missing regression test",
                            "evidence": "Updated evidence after arbitration.",
                            "required_action": "Add a regression test.",
                            "status": "open",
                        }
                    ],
                ),
                force=True,
            )
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = show_run_main([run_id, "--runs-dir", str(runs_dir)])
            output = stdout.getvalue()

        self.assertEqual(exit_code, 0, output)
        self.assertEqual(output_value(output, "arbitration_stale"), "true")

    def test_show_run_next_action_arbitrate_findings_when_arbitration_is_stale(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            _run_dir, run_id = make_approved_source_run(runs_dir)
            record_findings_for_run(
                runs_dir,
                run_id,
                make_findings_payload(run_id=run_id, overall_decision="needs_rework", findings=[make_blocking_qa_finding()]),
            )
            arbitration_file = write_json_file(
                tmp / "arbitration.json",
                make_arbitration_payload(
                    run_id=run_id,
                    overall_decision="pass",
                    arbitrated_findings=[
                        {
                            "finding_id": "F001",
                            "source_reviewer": "qa",
                            "original_severity": "major",
                            "final_severity": "minor",
                            "original_blocking": True,
                            "final_blocking": False,
                            "status": "downgraded",
                            "reason": "Non-blocking after arbitration.",
                            "final_required_action": None,
                        }
                    ],
                ),
            )
            with redirect_stdout(StringIO()):
                self.assertEqual(
                    record_arbitration_main([run_id, "--runs-dir", str(runs_dir), "--arbitration-file", str(arbitration_file)]),
                    0,
                )
            record_findings_for_run(
                runs_dir,
                run_id,
                make_findings_payload(
                    run_id=run_id,
                    overall_decision="needs_rework",
                    summary="Updated findings after arbitration.",
                    findings=[
                        {
                            "id": "F001",
                            "reviewer": "qa",
                            "category": "qa",
                            "severity": "major",
                            "title": "Missing regression test",
                            "evidence": "Updated evidence after arbitration.",
                            "required_action": "Add a regression test.",
                            "status": "open",
                        }
                    ],
                ),
                force=True,
            )
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = show_run_main([run_id, "--runs-dir", str(runs_dir)])
            output = stdout.getvalue()

        self.assertEqual(exit_code, 0, output)
        self.assertEqual(output_value(output, "next_action"), "arbitrate_findings")

    def test_show_run_next_action_review_rejected_when_final_blocking_arbitration_exists(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            _run_dir, run_id = make_approved_source_run(runs_dir)
            record_findings_for_run(
                runs_dir,
                run_id,
                make_findings_payload(run_id=run_id, overall_decision="needs_rework", findings=[make_blocking_qa_finding()]),
            )
            arbitration_file = write_json_file(
                tmp / "arbitration.json",
                make_arbitration_payload(
                    run_id=run_id,
                    overall_decision="needs_rework",
                    arbitrated_findings=[
                        {
                            "finding_id": "F001",
                            "source_reviewer": "qa",
                            "original_severity": "major",
                            "final_severity": "major",
                            "original_blocking": True,
                            "final_blocking": True,
                            "status": "upheld",
                            "reason": "Still blocking.",
                            "final_required_action": "Add the regression test.",
                        }
                    ],
                ),
            )
            with redirect_stdout(StringIO()):
                self.assertEqual(
                    record_arbitration_main([run_id, "--runs-dir", str(runs_dir), "--arbitration-file", str(arbitration_file)]),
                    0,
                )
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = show_run_main([run_id, "--runs-dir", str(runs_dir)])
            output = stdout.getvalue()

        self.assertEqual(exit_code, 0, output)
        self.assertEqual(output_value(output, "next_action"), "review_rejected")

    def test_show_pipeline_aggregates_arbitration_counts_and_next_action(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            _run_dir_a, run_id_a = make_approved_source_run(runs_dir)
            _run_dir_b, run_id_b = make_approved_source_run(runs_dir)
            record_findings_for_run(
                runs_dir,
                run_id_a,
                make_findings_payload(run_id=run_id_a, overall_decision="needs_rework", findings=[make_blocking_qa_finding()]),
            )
            record_findings_for_run(
                runs_dir,
                run_id_b,
                make_findings_payload(run_id=run_id_b, overall_decision="needs_rework", findings=[make_blocking_qa_finding()]),
            )
            arbitration_file = write_json_file(
                tmp / "arbitration.json",
                make_arbitration_payload(
                    run_id=run_id_a,
                    overall_decision="needs_rework",
                    arbitrated_findings=[
                        {
                            "finding_id": "F001",
                            "source_reviewer": "qa",
                            "original_severity": "major",
                            "final_severity": "major",
                            "original_blocking": True,
                            "final_blocking": True,
                            "status": "upheld",
                            "reason": "Still blocking after arbitration.",
                            "final_required_action": "Add the regression test.",
                        }
                    ],
                ),
            )
            with redirect_stdout(StringIO()):
                self.assertEqual(
                    record_arbitration_main(
                        [run_id_a, "--runs-dir", str(runs_dir), "--arbitration-file", str(arbitration_file)]
                    ),
                    0,
                )
            create_pipeline_fixture(
                tmp,
                pipeline_id="pipeline_arbitration_1",
                tasks=[
                    build_pipeline_task_result("task-a", run_id_a, runs_dir, title="Task A"),
                    build_pipeline_task_result("task-b", run_id_b, runs_dir, title="Task B"),
                ],
            )
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = show_pipeline_main(["pipeline_arbitration_1", "--runs-dir", str(runs_dir)])
            output = stdout.getvalue()

        self.assertEqual(exit_code, 0, output)
        self.assertEqual(output_value(output, "tasks_with_arbitration"), "1")
        self.assertEqual(output_value(output, "tasks_waiting_arbitration"), "1")
        self.assertEqual(output_value(output, "tasks_with_final_blocking_arbitration"), "1")
        self.assertEqual(output_value(output, "next_action"), "review_rejected")

    def test_show_pipeline_counts_stale_arbitration_and_prefers_rearbitration(self) -> None:
        with temporary_test_dir() as tmp:
            runs_dir = tmp / ".runs"
            _run_dir_a, run_id_a = make_approved_source_run(runs_dir)
            _run_dir_b, run_id_b = make_approved_source_run(runs_dir)
            record_findings_for_run(
                runs_dir,
                run_id_a,
                make_findings_payload(run_id=run_id_a, overall_decision="needs_rework", findings=[make_blocking_qa_finding()]),
            )
            record_findings_for_run(
                runs_dir,
                run_id_b,
                make_findings_payload(run_id=run_id_b, overall_decision="needs_rework", findings=[make_blocking_qa_finding()]),
            )
            arbitration_file = write_json_file(
                tmp / "arbitration.json",
                make_arbitration_payload(
                    run_id=run_id_a,
                    overall_decision="pass",
                    arbitrated_findings=[
                        {
                            "finding_id": "F001",
                            "source_reviewer": "qa",
                            "original_severity": "major",
                            "final_severity": "minor",
                            "original_blocking": True,
                            "final_blocking": False,
                            "status": "downgraded",
                            "reason": "Non-blocking after arbitration.",
                            "final_required_action": None,
                        }
                    ],
                ),
            )
            with redirect_stdout(StringIO()):
                self.assertEqual(
                    record_arbitration_main([run_id_a, "--runs-dir", str(runs_dir), "--arbitration-file", str(arbitration_file)]),
                    0,
                )
            record_findings_for_run(
                runs_dir,
                run_id_a,
                make_findings_payload(
                    run_id=run_id_a,
                    overall_decision="needs_rework",
                    summary="Updated findings after arbitration.",
                    findings=[
                        {
                            "id": "F001",
                            "reviewer": "qa",
                            "category": "qa",
                            "severity": "major",
                            "title": "Missing regression test",
                            "evidence": "Updated evidence after arbitration.",
                            "required_action": "Add a regression test.",
                            "status": "open",
                        }
                    ],
                ),
                force=True,
            )
            create_pipeline_fixture(
                tmp,
                pipeline_id="pipeline_arbitration_stale_1",
                tasks=[
                    build_pipeline_task_result("task-a", run_id_a, runs_dir, title="Task A"),
                    build_pipeline_task_result("task-b", run_id_b, runs_dir, title="Task B"),
                ],
            )
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = show_pipeline_main(["pipeline_arbitration_stale_1", "--runs-dir", str(runs_dir)])
            output = stdout.getvalue()

        self.assertEqual(exit_code, 0, output)
        self.assertEqual(output_value(output, "tasks_with_stale_arbitration"), "1")
        self.assertEqual(output_value(output, "next_action"), "arbitrate_findings")


if __name__ == "__main__":
    unittest.main()
