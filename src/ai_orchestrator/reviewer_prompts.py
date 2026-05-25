from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, Field

from ai_orchestrator.apply import build_run_application_context, load_run_state
from ai_orchestrator.review import build_review_packet_data
from ai_orchestrator.review_findings import load_run_findings
from ai_orchestrator.review_findings_schemas import ReviewFinding, ReviewFindingsReport
from ai_orchestrator.review_profiles import get_review_profile, list_review_profiles
from ai_orchestrator.risk_classification import load_run_risk_classification
from ai_orchestrator.schemas import RunState, StructuredExecutionReport
from ai_orchestrator.validation import load_structured_report

_REVIEW_PACKET_CHAR_CAP = 50_000
_FINAL_REPORT_CHAR_CAP = 20_000


class ReviewerPromptManifestEntry(BaseModel):
    profile: str
    path: str


class ReviewerPromptManifest(BaseModel):
    schema_version: str = "1.0"
    run_id: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    profiles: list[str] = Field(default_factory=list)
    prompts: list[ReviewerPromptManifestEntry] = Field(default_factory=list)


@dataclass(frozen=True)
class ReviewerPromptPacket:
    run_id: str
    run_dir: Path
    profile_id: str
    profile_title: str
    profile_description: str
    reviewer_type: str
    focus_areas: list[str]
    finding_categories: list[str]
    severity_guidance: list[str]
    required_evidence: list[str]
    non_goals: list[str]
    output_contract: str
    prompt_template: str
    state: RunState
    report: StructuredExecutionReport | None
    report_source: str | None
    changed_files: list[str]
    review_packet_path: Path
    review_packet_content: str
    review_packet_clipped: bool
    final_report_path: Path
    final_report_content: str
    final_report_clipped: bool
    execution_report_path: Path | None
    execution_report_json: str
    diff_preview: str
    review_findings_decision: str
    blocking_findings: int


@dataclass(frozen=True)
class PreparedReviewerPrompt:
    profile: str
    path: Path


@dataclass(frozen=True)
class PrepareReviewResult:
    run_id: str
    profiles: tuple[str, ...]
    prompts_dir: Path
    prompts: tuple[PreparedReviewerPrompt, ...]
    manifest_path: Path | None
    message: str | None = None


def _clip_text(text: str, *, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    clipped = text[:limit].rstrip() + f"\n\n... clipped after {limit} characters ..."
    return clipped, True


def _read_text_if_exists(path: Path, *, limit: int) -> tuple[str, bool]:
    if not path.exists() or not path.is_file():
        return "(missing)", False
    text = path.read_text(encoding="utf-8", errors="replace")
    return _clip_text(text, limit=limit)


def _resolve_profiles(*, profile_ids: list[str] | tuple[str, ...] | None, all_profiles: bool) -> tuple[str, ...]:
    if all_profiles:
        return tuple(profile.id for profile in list_review_profiles() if profile.id != "deterministic")
    selected = tuple(dict.fromkeys(item.strip() for item in (profile_ids or []) if item and item.strip()))
    if not selected:
        raise ValueError("at least one --profile is required unless --all-profiles is used")
    for profile_id in selected:
        if get_review_profile(profile_id) is None:
            raise ValueError(f"review profile not found: {profile_id}")
    return selected


def _resolve_required_profiles(*, run_id: str, runs_dir: str | Path) -> tuple[str, ...]:
    run_dir = Path(runs_dir) / run_id
    classification = load_run_risk_classification(run_dir)
    if classification is None:
        raise ValueError("risk classification not found; run classify-run first")
    return tuple(classification.required_review_profiles)


def _expected_output_template(*, run_id: str, profile_id: str) -> str:
    payload = {
        "schema_version": "1.0",
        "run_id": run_id,
        "summary": "...",
        "overall_decision": "pass | needs_rework | blocked",
        "findings": [
            {
                "id": "F001",
                "reviewer": profile_id,
                "category": "...",
                "severity": "critical | major | minor | nit",
                "title": "...",
                "evidence": "...",
                "required_action": "...",
                "file": None,
                "line": None,
                "status": "open",
            }
        ],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


def _strict_instructions() -> list[str]:
    return [
        "Base every finding on evidence.",
        "Do not invent files.",
        "Do not output prose outside JSON when asked for machine-readable output.",
        "Use critical/major only for actionable blockers.",
        "Use minor/nit for non-blocking issues.",
        "If no findings, return findings: [] and overall_decision: pass.",
        "Do not approve or reject the run.",
        "Do not ask for follow-up.",
        "Do not modify files.",
    ]


def build_reviewer_prompt_packet(
    *,
    run_id: str,
    runs_dir: str | Path,
    profile_id: str,
) -> ReviewerPromptPacket:
    profile = get_review_profile(profile_id)
    if profile is None:
        raise ValueError(f"review profile not found: {profile_id}")

    runs_dir_path = Path(runs_dir)
    run_dir = runs_dir_path / run_id
    if not run_dir.exists():
        raise FileNotFoundError(f"run does not exist: {run_id}")

    state = load_run_state(run_dir)
    if not state.executions:
        raise ValueError("run has no executions")

    loaded_report = load_structured_report(state.executions[-1])
    report = loaded_report.report
    execution_report_path = loaded_report.source_path
    changed_files = list(report.changed_files) if report is not None else []
    review_packet_path = run_dir / "REVIEW_PACKET.md"
    final_report_path = run_dir / "final_report.md"
    review_packet_content, review_packet_clipped = _read_text_if_exists(review_packet_path, limit=_REVIEW_PACKET_CHAR_CAP)
    final_report_content, final_report_clipped = _read_text_if_exists(final_report_path, limit=_FINAL_REPORT_CHAR_CAP)
    execution_report_json = (
        json.dumps(report.model_dump(mode="json"), indent=2, ensure_ascii=False)
        if report is not None
        else "(missing)\nIf EXECUTION_REPORT.json is missing, the reviewer should record a finding with evidence."
    )
    try:
        review_packet_data = build_review_packet_data(run_dir)
        diff_preview = review_packet_data.diff_text or "(no diff preview available)"
    except Exception as exc:  # noqa: BLE001 - prompt generation should degrade gracefully
        diff_preview = f"(diff preview unavailable: {exc})"

    findings_report = load_run_findings(run_dir)
    review_findings_decision = findings_report.overall_decision if findings_report is not None else (state.review_findings_decision or "")
    blocking_findings = findings_report.counts.blocking_open if findings_report is not None else (state.review_findings_blocking_count or 0)

    return ReviewerPromptPacket(
        run_id=run_id,
        run_dir=run_dir,
        profile_id=profile.id,
        profile_title=profile.title,
        profile_description=profile.description,
        reviewer_type=profile.reviewer_type,
        focus_areas=list(profile.focus_areas),
        finding_categories=list(profile.finding_categories),
        severity_guidance=list(profile.default_severity_guidance),
        required_evidence=list(profile.required_evidence),
        non_goals=list(profile.non_goals),
        output_contract=profile.output_contract,
        prompt_template=profile.prompt_template,
        state=state,
        report=report,
        report_source=loaded_report.source,
        changed_files=changed_files,
        review_packet_path=review_packet_path,
        review_packet_content=review_packet_content,
        review_packet_clipped=review_packet_clipped,
        final_report_path=final_report_path,
        final_report_content=final_report_content,
        final_report_clipped=final_report_clipped,
        execution_report_path=execution_report_path,
        execution_report_json=execution_report_json,
        diff_preview=diff_preview,
        review_findings_decision=review_findings_decision or "empty",
        blocking_findings=blocking_findings,
    )


def render_reviewer_prompt_markdown(packet: ReviewerPromptPacket) -> str:
    criteria_block = [f"- {item}" for item in packet.state.task.acceptance_criteria] or ["- (none)"]
    changed_files_block = [f"- `{item}`" for item in packet.changed_files] or ["- (none reported)"]
    lines = [
        f"# Reviewer Prompt Packet: {packet.profile_id} for {packet.run_id}",
        "",
        "## Role",
        "",
        packet.profile_title,
        packet.profile_description,
        "",
        "## Reviewer type",
        "",
        f"`{packet.reviewer_type}`",
        "",
        "## Focus areas",
        *[f"- {item}" for item in packet.focus_areas],
        "",
        "## Finding categories",
        *[f"- `{item}`" for item in packet.finding_categories],
        "",
        "## Severity guidance",
        *[f"- {item}" for item in packet.severity_guidance],
        "",
        "## Required evidence",
        *[f"- {item}" for item in packet.required_evidence],
        "",
        "## Non-goals",
        *[f"- {item}" for item in packet.non_goals],
        "- Do not approve or reject the run.",
        "- Do not apply changes.",
        "- Do not commit.",
        "- Do not modify files.",
        "- Produce findings only.",
        "",
        "## Output contract",
        "",
        packet.output_contract,
        "",
        "## Future prompt template contract",
        "",
        packet.prompt_template,
        "",
        "## Task context",
        "",
        packet.state.task.description,
        "",
        "### Acceptance criteria",
        *criteria_block,
        "",
        "## Run status",
        "",
        f"- run_id: `{packet.run_id}`",
        f"- validator_status: `{packet.state.final_status}`",
        f"- backend: `{packet.state.backend_name or ''}`",
        f"- human_review_decision: `{packet.state.human_review_decision or ''}`",
        f"- existing_review_findings_decision: `{packet.review_findings_decision}`",
        f"- existing_blocking_findings: `{packet.blocking_findings}`",
        "",
        "## Changed files",
        *changed_files_block,
        "",
        "## Existing artifacts",
        "",
        f"- final_report.md: `{packet.final_report_path.resolve()}`",
        f"- REVIEW_PACKET.md: `{packet.review_packet_path.resolve()}`",
        f"- EXECUTION_REPORT.json: `{packet.execution_report_path.resolve() if packet.execution_report_path else '(missing)'}`",
        "",
        "## Diff preview",
        "",
        "```diff",
        packet.diff_preview,
        "```",
        "",
        "## Final report",
        "",
    ]
    if packet.final_report_clipped:
        lines.append("_Final report excerpt was clipped for packet size safety._")
        lines.append("")
    lines.extend(["```markdown", packet.final_report_content, "```", "", "## Review packet", ""])
    if packet.review_packet_clipped:
        lines.append("_Review packet excerpt was clipped for packet size safety._")
        lines.append("")
    lines.extend(["```markdown", packet.review_packet_content, "```", "", "## Execution report", ""])
    if packet.report_source:
        lines.append(f"Source: `{packet.report_source}`")
        lines.append("")
    lines.extend(["```json", packet.execution_report_json, "```", "", "## Expected output", "", "Reviewer must return JSON compatible with ReviewFindingsReport.", "", "```json", _expected_output_template(run_id=packet.run_id, profile_id=packet.profile_id), "```", "", "## Strict instructions", *[f"- {item}" for item in _strict_instructions()]])
    return "\n".join(lines) + "\n"


def _load_manifest(path: Path, *, run_id: str) -> ReviewerPromptManifest:
    if not path.exists():
        return ReviewerPromptManifest(run_id=run_id)
    manifest = ReviewerPromptManifest.model_validate_json(path.read_text(encoding="utf-8-sig"))
    if manifest.run_id != run_id:
        raise ValueError(f"reviewer prompt manifest run_id mismatch: expected {run_id}, got {manifest.run_id}")
    return manifest


def load_reviewer_prompt_manifest(run_dir: str | Path) -> ReviewerPromptManifest | None:
    manifest_path = Path(run_dir) / "reviewer_prompts" / "MANIFEST.json"
    if not manifest_path.exists():
        return None
    return ReviewerPromptManifest.model_validate_json(manifest_path.read_text(encoding="utf-8-sig"))


def write_reviewer_prompt_manifest(
    *,
    run_id: str,
    prompts_dir: Path,
    prepared_prompts: tuple[PreparedReviewerPrompt, ...],
) -> Path:
    manifest_path = prompts_dir / "MANIFEST.json"
    manifest = _load_manifest(manifest_path, run_id=run_id)
    entries = {entry.profile: entry for entry in manifest.prompts}
    for prepared in prepared_prompts:
        entries[prepared.profile] = ReviewerPromptManifestEntry(profile=prepared.profile, path=str(prepared.path.resolve()))
    ordered_profiles = sorted(entries)
    new_manifest = ReviewerPromptManifest(
        run_id=run_id,
        created_at=datetime.now(timezone.utc),
        profiles=ordered_profiles,
        prompts=[entries[profile_id] for profile_id in ordered_profiles],
    )
    manifest_path.write_text(new_manifest.model_dump_json(indent=2), encoding="utf-8")
    return manifest_path.resolve()


def prepare_review_prompts(
    *,
    run_id: str,
    runs_dir: str | Path,
    profile_ids: list[str] | tuple[str, ...] | None = None,
    all_profiles: bool = False,
    required_profiles: bool = False,
    output_dir: str | Path | None = None,
    force: bool = False,
) -> PrepareReviewResult:
    runs_dir_path = Path(runs_dir)
    run_dir = runs_dir_path / run_id
    if not run_dir.exists():
        raise FileNotFoundError(f"run does not exist: {run_id}")
    if required_profiles:
        profiles = _resolve_required_profiles(run_id=run_id, runs_dir=runs_dir_path)
    else:
        profiles = _resolve_profiles(profile_ids=profile_ids, all_profiles=all_profiles)

    prompts_dir = Path(output_dir).expanduser().resolve() if output_dir is not None else (run_dir / "reviewer_prompts").resolve()
    if required_profiles and not profiles:
        return PrepareReviewResult(
            run_id=run_id,
            profiles=tuple(),
            prompts_dir=prompts_dir,
            prompts=tuple(),
            manifest_path=None,
            message="no required review profiles for this run",
        )
    prompt_targets = {
        profile_id: prompts_dir / f"{profile_id}_review_prompt.md"
        for profile_id in profiles
    }
    existing = [profile_id for profile_id, path in prompt_targets.items() if path.exists()]
    if existing and not force:
        raise ValueError(
            "reviewer prompt already exists for: " + ", ".join(existing) + ". Pass --force to overwrite selected prompt files."
        )

    prompts_dir.mkdir(parents=True, exist_ok=True)
    prepared: list[PreparedReviewerPrompt] = []
    for profile_id in profiles:
        packet = build_reviewer_prompt_packet(run_id=run_id, runs_dir=runs_dir_path, profile_id=profile_id)
        prompt_path = prompt_targets[profile_id]
        prompt_path.write_text(render_reviewer_prompt_markdown(packet), encoding="utf-8")
        prepared.append(PreparedReviewerPrompt(profile=profile_id, path=prompt_path.resolve()))

    manifest_path = write_reviewer_prompt_manifest(
        run_id=run_id,
        prompts_dir=prompts_dir,
        prepared_prompts=tuple(prepared),
    )
    return PrepareReviewResult(
        run_id=run_id,
        profiles=profiles,
        prompts_dir=prompts_dir,
        prompts=tuple(prepared),
        manifest_path=manifest_path,
        message=None,
    )
