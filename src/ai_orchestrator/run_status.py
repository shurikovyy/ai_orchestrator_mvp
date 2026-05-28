from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

from ai_orchestrator.apply import load_run_state
from ai_orchestrator.review_arbitration import is_arbitration_stale, load_run_arbitration
from ai_orchestrator.review_findings import load_run_findings
from ai_orchestrator.reviewer_prompts import load_reviewer_prompt_manifest
from ai_orchestrator.risk_classification import load_run_risk_classification


@dataclass(frozen=True)
class RunStatusSummary:
    run_id: str
    validator_status: str
    backend: str | None
    human_review_decision: str | None
    findings_exists: bool
    review_findings_decision: str
    blocking_findings: int
    review_findings_source_profile: str | None
    review_findings_source_kind: str | None
    arbitration_exists: bool
    review_arbitration_decision: str
    arbitration_final_blocking: int
    arbitration_human_escalation_required: bool
    arbitration_stale: bool
    review_arbitration_source_findings_sha256: str | None
    findings_feedback_exists: bool
    findings_feedback_count: int
    reviewer_prompts_exists: bool
    reviewer_prompts_count: int
    risk_classification_exists: bool
    risk_level: str | None
    change_type: str | None
    required_review_profiles: list[str]
    optional_review_profiles: list[str]
    acceptance_status: str
    application_status: str
    is_rework: bool
    source_run_id: str | None
    feedback_present: bool
    final_report_exists: bool
    review_packet_exists: bool
    review_decision_exists: bool
    apply_report_exists: bool
    acceptance_exists: bool
    next_action: str
    artifacts: dict[str, str]
    exists: dict[str, bool]


def _bool_text(value: bool) -> str:
    return str(value).lower()


def _build_artifact_paths(run_dir: Path) -> dict[str, Path]:
    return {
        "state": run_dir / "state.json",
        "final_report": run_dir / "final_report.md",
        "review_packet": run_dir / "REVIEW_PACKET.md",
        "review_findings": run_dir / "REVIEW_FINDINGS.json",
        "review_findings_markdown": run_dir / "REVIEW_FINDINGS.md",
        "review_arbitration": run_dir / "REVIEW_ARBITRATION.json",
        "review_arbitration_markdown": run_dir / "REVIEW_ARBITRATION.md",
        "findings_feedback": run_dir / "REVIEW_FEEDBACK_FROM_FINDINGS.md",
        "risk_classification": run_dir / "RISK_CLASSIFICATION.json",
        "risk_classification_markdown": run_dir / "RISK_CLASSIFICATION.md",
        "reviewer_prompts_dir": run_dir / "reviewer_prompts",
        "reviewer_prompts_manifest": run_dir / "reviewer_prompts" / "MANIFEST.json",
        "review_decision": run_dir / "REVIEW_DECISION.json",
        "review_decision_md": run_dir / "REVIEW_DECISION.md",
        "review_feedback": run_dir / "REVIEW_FEEDBACK.md",
        "rework_feedback": run_dir / "REWORK_FEEDBACK.md",
        "apply_report": run_dir / "APPLY_REPORT.md",
        "apply_report_json": run_dir / "APPLY_REPORT.json",
        "acceptance": run_dir / "ACCEPTANCE.md",
    }


def _compute_next_action(
    *,
    validator_status: str,
    human_review_decision: str | None,
    blocking_findings: int,
    arbitration_exists: bool,
    review_arbitration_decision: str,
    arbitration_final_blocking: int,
    arbitration_human_escalation_required: bool,
    arbitration_stale: bool,
    findings_exists: bool,
    risk_classification_exists: bool,
    required_review_profiles: list[str],
    prepared_review_profiles: list[str],
    application_status: str,
    acceptance_exists: bool,
) -> str:
    if validator_status != "approved":
        return "rework_or_inspect_failure"
    if human_review_decision == "rejected":
        return "rework_run"
    if arbitration_exists and arbitration_stale:
        return "arbitrate_findings"
    if arbitration_exists and arbitration_human_escalation_required:
        return "human_escalation"
    if arbitration_exists and arbitration_final_blocking > 0:
        return "review_rejected"
    if arbitration_exists and review_arbitration_decision != "pass":
        return "review_rejected"
    arbitration_resolves_blocking_findings = arbitration_exists and review_arbitration_decision == "pass"
    if blocking_findings > 0 and not arbitration_resolves_blocking_findings:
        return "arbitrate_findings"
    if acceptance_exists:
        return "done"
    if application_status == "applied":
        return "manual_commit"
    if human_review_decision == "approved":
        return "apply_run"
    if arbitration_resolves_blocking_findings:
        return "review_run"
    if not risk_classification_exists:
        return "classify_run"
    if required_review_profiles:
        prepared_profiles = set(prepared_review_profiles)
        missing_required = [profile for profile in required_review_profiles if profile not in prepared_profiles]
        if missing_required:
            return "prepare_required_reviews"
        if not findings_exists:
            return "run_external_reviewer_or_record_findings"
    if not findings_exists:
        return "run_review_checks"
    return "review_run"


def _count_reviewer_prompts(prompts_dir: Path) -> int:
    if not prompts_dir.exists() or not prompts_dir.is_dir():
        return 0
    return sum(1 for path in prompts_dir.glob("*_review_prompt.md") if path.is_file())


def build_run_status_summary(*, run_id: str, runs_dir: str | Path) -> RunStatusSummary:
    runs_dir_path = Path(runs_dir)
    run_dir = runs_dir_path / run_id
    state_path = run_dir / "state.json"
    if not run_dir.exists() or not state_path.exists():
        raise FileNotFoundError(f"run not found: {run_id}")

    state = load_run_state(run_dir)
    artifact_paths = _build_artifact_paths(run_dir)
    if state.findings_feedback_path:
        artifact_paths["findings_feedback"] = Path(state.findings_feedback_path)
    exists = {
        "final_report": artifact_paths["final_report"].exists(),
        "review_packet": artifact_paths["review_packet"].exists(),
        "review_findings": artifact_paths["review_findings"].exists(),
        "review_findings_markdown": artifact_paths["review_findings_markdown"].exists(),
        "review_arbitration": artifact_paths["review_arbitration"].exists(),
        "review_arbitration_markdown": artifact_paths["review_arbitration_markdown"].exists(),
        "findings_feedback": artifact_paths["findings_feedback"].exists(),
        "risk_classification": artifact_paths["risk_classification"].exists(),
        "risk_classification_markdown": artifact_paths["risk_classification_markdown"].exists(),
        "reviewer_prompts_manifest": artifact_paths["reviewer_prompts_manifest"].exists(),
        "review_decision": artifact_paths["review_decision"].exists(),
        "review_decision_md": artifact_paths["review_decision_md"].exists(),
        "review_feedback": artifact_paths["review_feedback"].exists(),
        "rework_feedback": artifact_paths["rework_feedback"].exists(),
        "apply_report": artifact_paths["apply_report"].exists(),
        "apply_report_json": artifact_paths["apply_report_json"].exists(),
        "acceptance": artifact_paths["acceptance"].exists(),
    }
    acceptance_exists = exists["acceptance"]
    application_status = "applied" if state.apply_status == "applied" or exists["apply_report"] or exists["apply_report_json"] else "not_applied"
    human_review_decision = state.human_review_decision
    findings_report = load_run_findings(run_dir)
    findings_exists = findings_report is not None or exists["review_findings"]
    review_findings_decision = findings_report.overall_decision if findings_report is not None else (state.review_findings_decision or "empty")
    blocking_findings = findings_report.counts.blocking_open if findings_report is not None else (state.review_findings_blocking_count or 0)
    review_findings_source_profile = (
        findings_report.source_profile if findings_report is not None else state.review_findings_source_profile
    )
    review_findings_source_kind = (
        findings_report.source_kind if findings_report is not None else state.review_findings_source_kind
    )
    arbitration_report = load_run_arbitration(run_dir)
    arbitration_exists = arbitration_report is not None or exists["review_arbitration"]
    review_arbitration_decision = (
        arbitration_report.overall_decision
        if arbitration_report is not None
        else (state.review_arbitration_decision or "empty")
    )
    arbitration_final_blocking = (
        arbitration_report.counts.final_blocking
        if arbitration_report is not None
        else (state.review_arbitration_final_blocking_count or 0)
    )
    arbitration_stale = (
        is_arbitration_stale(run_dir, arbitration_report)
        if arbitration_report is not None
        else state.review_arbitration_stale
    )
    arbitration_human_escalation_required = (
        arbitration_report.counts.human_escalation_required > 0
        if arbitration_report is not None
        else state.review_arbitration_human_escalation_required
    )
    review_arbitration_source_findings_sha256 = (
        arbitration_report.source_findings_sha256
        if arbitration_report is not None
        else state.review_arbitration_source_findings_sha256
    )
    risk_classification = load_run_risk_classification(run_dir)
    risk_classification_exists = risk_classification is not None or exists["risk_classification"]
    risk_level = risk_classification.risk_level if risk_classification is not None else state.risk_level
    change_type = risk_classification.change_type if risk_classification is not None else state.change_type
    required_review_profiles = (
        list(risk_classification.required_review_profiles)
        if risk_classification is not None
        else list(state.required_review_profiles)
    )
    optional_review_profiles = (
        list(risk_classification.optional_review_profiles)
        if risk_classification is not None
        else list(state.optional_review_profiles)
    )
    prompt_manifest = load_reviewer_prompt_manifest(run_dir)
    prepared_review_profiles = prompt_manifest.profiles if prompt_manifest is not None else []
    reviewer_prompts_count = _count_reviewer_prompts(artifact_paths["reviewer_prompts_dir"])
    next_action = _compute_next_action(
        validator_status=state.final_status,
        human_review_decision=human_review_decision,
        blocking_findings=blocking_findings,
        arbitration_exists=arbitration_exists,
        review_arbitration_decision=review_arbitration_decision,
        arbitration_final_blocking=arbitration_final_blocking,
        arbitration_human_escalation_required=arbitration_human_escalation_required,
        arbitration_stale=arbitration_stale,
        findings_exists=findings_exists,
        risk_classification_exists=risk_classification_exists,
        required_review_profiles=required_review_profiles,
        prepared_review_profiles=prepared_review_profiles,
        application_status=application_status,
        acceptance_exists=acceptance_exists,
    )
    feedback_present = bool(
        state.human_review_feedback
        or state.task.rework_feedback
        or exists["review_feedback"]
        or exists["rework_feedback"]
    )

    return RunStatusSummary(
        run_id=state.run_id,
        validator_status=state.final_status,
        backend=state.backend_name,
        human_review_decision=human_review_decision,
        findings_exists=findings_exists,
        review_findings_decision=review_findings_decision,
        blocking_findings=blocking_findings,
        review_findings_source_profile=review_findings_source_profile,
        review_findings_source_kind=review_findings_source_kind,
        arbitration_exists=arbitration_exists,
        review_arbitration_decision=review_arbitration_decision,
        arbitration_final_blocking=arbitration_final_blocking,
        arbitration_human_escalation_required=arbitration_human_escalation_required,
        arbitration_stale=arbitration_stale,
        review_arbitration_source_findings_sha256=review_arbitration_source_findings_sha256,
        findings_feedback_exists=exists["findings_feedback"],
        findings_feedback_count=state.findings_feedback_count,
        reviewer_prompts_exists=reviewer_prompts_count > 0,
        reviewer_prompts_count=reviewer_prompts_count,
        risk_classification_exists=risk_classification_exists,
        risk_level=risk_level,
        change_type=change_type,
        required_review_profiles=required_review_profiles,
        optional_review_profiles=optional_review_profiles,
        acceptance_status="accepted" if acceptance_exists else "not_accepted",
        application_status=application_status,
        is_rework=bool(state.task.rework_of_run_id),
        source_run_id=state.task.rework_of_run_id,
        feedback_present=feedback_present,
        final_report_exists=exists["final_report"],
        review_packet_exists=exists["review_packet"],
        review_decision_exists=exists["review_decision"],
        apply_report_exists=exists["apply_report"],
        acceptance_exists=acceptance_exists,
        next_action=next_action,
        artifacts={name: str(path.resolve()) for name, path in artifact_paths.items()},
        exists=exists,
    )


def format_run_status_text(summary: RunStatusSummary, *, show_paths: bool = False) -> str:
    lines = [
        f"run_id={summary.run_id}",
        f"validator_status={summary.validator_status}",
        f"backend={summary.backend or ''}",
        f"human_review_decision={summary.human_review_decision or ''}",
        f"findings_exists={_bool_text(summary.findings_exists)}",
        f"review_findings_decision={summary.review_findings_decision}",
        f"blocking_findings={summary.blocking_findings}",
        f"review_findings_source_profile={summary.review_findings_source_profile or ''}",
        f"review_findings_source_kind={summary.review_findings_source_kind or ''}",
        f"arbitration_exists={_bool_text(summary.arbitration_exists)}",
        f"review_arbitration_decision={summary.review_arbitration_decision}",
        f"arbitration_final_blocking={summary.arbitration_final_blocking}",
        f"arbitration_human_escalation_required={_bool_text(summary.arbitration_human_escalation_required)}",
        f"arbitration_stale={_bool_text(summary.arbitration_stale)}",
        f"review_arbitration_source_findings_sha256={summary.review_arbitration_source_findings_sha256 or ''}",
        f"findings_feedback_exists={_bool_text(summary.findings_feedback_exists)}",
        f"findings_feedback_count={summary.findings_feedback_count}",
        f"reviewer_prompts_exists={_bool_text(summary.reviewer_prompts_exists)}",
        f"reviewer_prompts_count={summary.reviewer_prompts_count}",
        f"risk_classification_exists={_bool_text(summary.risk_classification_exists)}",
        f"risk_level={summary.risk_level or ''}",
        f"change_type={summary.change_type or ''}",
        "required_review_profiles=" + ",".join(summary.required_review_profiles),
        "optional_review_profiles=" + ",".join(summary.optional_review_profiles),
        f"acceptance_status={summary.acceptance_status}",
        f"application_status={summary.application_status}",
        f"is_rework={_bool_text(summary.is_rework)}",
        f"source_run_id={summary.source_run_id or ''}",
        f"feedback_present={_bool_text(summary.feedback_present)}",
        f"final_report_exists={_bool_text(summary.final_report_exists)}",
        f"review_packet_exists={_bool_text(summary.review_packet_exists)}",
        f"review_decision_exists={_bool_text(summary.review_decision_exists)}",
        f"apply_report_exists={_bool_text(summary.apply_report_exists)}",
        f"acceptance_exists={_bool_text(summary.acceptance_exists)}",
        f"next_action={summary.next_action}",
    ]
    if show_paths:
        lines.extend(
            [
                f"final_report={summary.artifacts['final_report']}",
                f"review_packet={summary.artifacts['review_packet']}",
                f"review_findings={summary.artifacts['review_findings']}",
                f"review_findings_markdown={summary.artifacts['review_findings_markdown']}",
                f"review_arbitration={summary.artifacts['review_arbitration']}",
                f"review_arbitration_markdown={summary.artifacts['review_arbitration_markdown']}",
                f"findings_feedback={summary.artifacts['findings_feedback']}",
                f"risk_classification={summary.artifacts['risk_classification']}",
                f"risk_classification_markdown={summary.artifacts['risk_classification_markdown']}",
                f"reviewer_prompts_dir={summary.artifacts['reviewer_prompts_dir']}",
                f"reviewer_prompts_manifest={summary.artifacts['reviewer_prompts_manifest']}",
                f"review_decision={summary.artifacts['review_decision']}",
                f"apply_report={summary.artifacts['apply_report']}",
                f"apply_report_json={summary.artifacts['apply_report_json']}",
                f"acceptance={summary.artifacts['acceptance']}",
                f"state={summary.artifacts['state']}",
            ]
        )
    return "\n".join(lines)


def format_run_status_json(summary: RunStatusSummary) -> str:
    payload = {
        "run_id": summary.run_id,
        "validator_status": summary.validator_status,
        "backend": summary.backend,
        "human_review_decision": summary.human_review_decision,
        "findings_exists": summary.findings_exists,
        "review_findings_decision": summary.review_findings_decision,
        "blocking_findings": summary.blocking_findings,
        "review_findings_source_profile": summary.review_findings_source_profile,
        "review_findings_source_kind": summary.review_findings_source_kind,
        "arbitration_exists": summary.arbitration_exists,
        "review_arbitration_decision": summary.review_arbitration_decision,
        "arbitration_final_blocking": summary.arbitration_final_blocking,
        "arbitration_human_escalation_required": summary.arbitration_human_escalation_required,
        "arbitration_stale": summary.arbitration_stale,
        "review_arbitration_source_findings_sha256": summary.review_arbitration_source_findings_sha256,
        "findings_feedback_exists": summary.findings_feedback_exists,
        "findings_feedback_count": summary.findings_feedback_count,
        "reviewer_prompts_exists": summary.reviewer_prompts_exists,
        "reviewer_prompts_count": summary.reviewer_prompts_count,
        "risk_classification_exists": summary.risk_classification_exists,
        "risk_level": summary.risk_level,
        "change_type": summary.change_type,
        "required_review_profiles": summary.required_review_profiles,
        "optional_review_profiles": summary.optional_review_profiles,
        "acceptance_status": summary.acceptance_status,
        "application_status": summary.application_status,
        "is_rework": summary.is_rework,
        "source_run_id": summary.source_run_id,
        "feedback_present": summary.feedback_present,
        "next_action": summary.next_action,
        "artifacts": summary.artifacts,
        "exists": summary.exists,
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)
