from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ai_orchestrator.schemas import RunState, StructuredExecutionReport
from ai_orchestrator.validation import (
    _normalize_manifest_path,
    is_manifest_ignored_path,
    is_manifest_reportable_file,
    load_structured_report,
)

_RUNTIME_REPORT_FILES = {"EXECUTION_REPORT.json"}


@dataclass(frozen=True)
class ApplyFileEntry:
    path: str
    status: str
    apply_to_target: bool
    reason: str = ""


@dataclass(frozen=True)
class RunApplicationContext:
    run_dir: Path
    state: RunState
    report: StructuredExecutionReport | None
    report_source: str | None
    workspace_dir: Path | None
    target_workspace: Path | None
    changed_files: list[ApplyFileEntry] = field(default_factory=list)


@dataclass(frozen=True)
class ReviewGateResult:
    review_gate: str
    review_decision: str
    review_decision_path: str | None
    review_gate_bypassed: bool
    review_gate_reason: str | None


@dataclass(frozen=True)
class PreparedApplyOperation:
    run_id: str
    context: RunApplicationContext
    target_workspace: Path
    review_gate: ReviewGateResult


@dataclass(frozen=True)
class AcceptRunResult:
    run_id: str
    target_workspace: Path
    applied_files: list[str]
    skipped_files: list[str]
    deleted_files: list[str]
    commit_hash: str | None
    acceptance_path: Path
    review_gate: str
    review_decision: str
    review_decision_path: str | None
    review_gate_bypassed: bool
    no_target_changes: bool = False


@dataclass(frozen=True)
class ApplyRunResult:
    run_id: str
    target_workspace: Path
    applied_files: list[str]
    skipped_files: list[str]
    deleted_files: list[str]
    review_gate: str
    review_decision: str
    review_decision_path: str | None
    review_gate_bypassed: bool
    apply_report_path: Path | None
    apply_report_json_path: Path | None
    target_status: str


def _safe_relative_path(value: str) -> str:
    normalized = _normalize_manifest_path(value)
    if not normalized:
        raise ValueError("changed file path is empty")
    path = Path(normalized)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"unsafe changed file path: {value}")
    return normalized


def _is_runtime_report_file(path: str) -> bool:
    return Path(path).name in _RUNTIME_REPORT_FILES


def _latest_execution(state: RunState):
    if not state.executions:
        raise ValueError("run has no executions")
    return state.executions[-1]


def load_run_state(run_dir: Path) -> RunState:
    state_path = run_dir / "state.json"
    if not state_path.exists():
        raise FileNotFoundError(f"state.json not found: {state_path}")
    return RunState.model_validate_json(state_path.read_text(encoding="utf-8-sig"))


def _target_workspace_from_state(state: RunState, override: str | None = None) -> Path | None:
    raw = override or state.task.seed_workspace_path
    if not raw:
        return None
    return Path(raw).expanduser().resolve()


def build_run_application_context(
    run_dir: Path,
    *,
    target_workspace_override: str | None = None,
) -> RunApplicationContext:
    state = load_run_state(run_dir)
    execution = _latest_execution(state)
    loaded_report = load_structured_report(execution)
    report = loaded_report.report
    workspace_dir = loaded_report.source_path.parent if loaded_report.source_path else None
    target_workspace = _target_workspace_from_state(state, target_workspace_override)

    entries: list[ApplyFileEntry] = []
    if report is not None and workspace_dir is not None:
        for raw_path in report.changed_files:
            safe_path = _safe_relative_path(raw_path)
            rel = Path(safe_path)
            is_runtime_report = _is_runtime_report_file(safe_path)
            is_ignored = is_manifest_ignored_path(rel)
            is_reportable = is_manifest_reportable_file(rel)
            source_path = workspace_dir / rel
            target_path = target_workspace / rel if target_workspace is not None else None

            if source_path.exists() and target_path is not None and target_path.exists():
                status = "modified"
            elif source_path.exists():
                status = "added"
            elif target_path is not None and target_path.exists():
                status = "deleted"
            else:
                status = "missing"

            apply_to_target = bool(
                target_workspace is not None
                and not is_runtime_report
                and not is_ignored
                and is_reportable
                and status in {"added", "modified", "deleted"}
            )
            reason = ""
            if is_runtime_report:
                reason = "run report artifact; not applied to target repo"
            elif not is_reportable or is_ignored:
                reason = "not a reportable source/config file"
            elif target_workspace is None:
                reason = "no target workspace available"
            elif status == "missing":
                reason = "reported file is missing in both workspace and target"

            entries.append(ApplyFileEntry(path=safe_path, status=status, apply_to_target=apply_to_target, reason=reason))

    return RunApplicationContext(
        run_dir=run_dir,
        state=state,
        report=report,
        report_source=loaded_report.source,
        workspace_dir=workspace_dir,
        target_workspace=target_workspace,
        changed_files=entries,
    )


def check_review_gate(
    state: RunState,
    *,
    allow_unreviewed: bool,
    action_name: str,
) -> ReviewGateResult:
    decision = state.human_review_decision
    decision_path = state.human_review_decision_path
    if decision == "approved":
        return ReviewGateResult("human_approved", "approved", decision_path, False, None)
    if decision == "rejected":
        raise ValueError(f"run has rejected human review decision; run rework-run before {action_name}")
    if decision is None:
        if allow_unreviewed:
            return ReviewGateResult("bypassed_unreviewed", "missing", decision_path, True, "--allow-unreviewed")
        raise ValueError(
            "run has no approved human review decision; run review-run --decision approved first or pass --allow-unreviewed"
        )
    raise ValueError(f"run has invalid human review decision: {decision}")


def _run_git(target_workspace: Path, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ["git", "-C", str(target_workspace), *args],
        text=True,
        capture_output=True,
        check=False,
    )
    if check and completed.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed with exit_code={completed.returncode}: "
            f"{completed.stderr.strip() or completed.stdout.strip()}"
        )
    return completed


def _ensure_target_workspace_is_git_repo(
    *,
    target_workspace: Path,
    run_id: str,
    dry_run: bool,
    init_target_git: bool,
    action_name: str,
) -> None:
    if (target_workspace / ".git").exists():
        return
    if not init_target_git:
        if action_name == "accept-run":
            raise ValueError(
                f"target workspace is not a git repository: {target_workspace}. "
                "Initialize it first with `git init && git add . && git commit -m baseline`, "
                "or rerun accept-run with --init-target-git for disposable/toy workspaces."
            )
        raise ValueError(
            f"target workspace is not a git repository: {target_workspace}. "
            "Initialize it first with `git init && git add . && git commit -m baseline`."
        )
    if dry_run:
        raise ValueError("--dry-run cannot initialize a missing target git repository")
    _run_git(target_workspace, ["init"], check=True)
    if not _run_git(target_workspace, ["config", "user.email"], check=False).stdout.strip():
        _run_git(target_workspace, ["config", "user.email", "ai-orchestrator@example.invalid"], check=True)
    if not _run_git(target_workspace, ["config", "user.name"], check=False).stdout.strip():
        _run_git(target_workspace, ["config", "user.name", "AI Orchestrator"], check=True)
    _run_git(target_workspace, ["add", "--", "."], check=True)
    baseline_status = _run_git(target_workspace, ["status", "--porcelain"], check=True).stdout.strip()
    if baseline_status:
        _run_git(
            target_workspace,
            ["commit", "-m", f"chore: seed baseline before accepting {run_id}"],
            check=True,
        )


def prepare_run_application(
    *,
    run_id: str,
    runs_dir: Path,
    target_workspace_override: str | None,
    dry_run: bool,
    allow_unreviewed: bool,
    action_name: str,
    init_target_git: bool = False,
) -> PreparedApplyOperation:
    context = build_run_application_context(runs_dir / run_id, target_workspace_override=target_workspace_override)
    state = context.state
    if state.final_status != "approved":
        raise ValueError(f"run {run_id} is not approved: {state.final_status}")
    if context.report is None:
        raise ValueError(f"cannot {action_name} without a valid EXECUTION_REPORT.json")
    if context.workspace_dir is None or not context.workspace_dir.exists():
        raise ValueError("cannot infer run workspace from EXECUTION_REPORT.json")
    if context.target_workspace is None:
        raise ValueError(f"{action_name} requires a seed workspace or --target-workspace")
    target_workspace = context.target_workspace
    if not target_workspace.exists() or not target_workspace.is_dir():
        raise FileNotFoundError(f"target workspace does not exist or is not a directory: {target_workspace}")

    _ensure_target_workspace_is_git_repo(
        target_workspace=target_workspace,
        run_id=run_id,
        dry_run=dry_run,
        init_target_git=init_target_git,
        action_name=action_name,
    )

    dirty_before = _run_git(target_workspace, ["status", "--porcelain"], check=True).stdout.strip()
    if dirty_before:
        raise ValueError(f"target git repository is dirty; commit/stash changes before {action_name}")

    review_gate = check_review_gate(state, allow_unreviewed=allow_unreviewed, action_name=action_name)
    return PreparedApplyOperation(
        run_id=run_id,
        context=context,
        target_workspace=target_workspace,
        review_gate=review_gate,
    )


def apply_run_changes(
    prepared: PreparedApplyOperation,
    *,
    dry_run: bool,
) -> tuple[list[str], list[str], list[str]]:
    if prepared.context.workspace_dir is None:
        raise ValueError("cannot apply changes without a run workspace")
    applied: list[str] = []
    skipped: list[str] = []
    deleted: list[str] = []
    for entry in prepared.context.changed_files:
        if not entry.apply_to_target:
            skipped.append(entry.path)
            continue
        rel = Path(_safe_relative_path(entry.path))
        source_path = prepared.context.workspace_dir / rel
        target_path = prepared.target_workspace / rel
        if dry_run:
            if source_path.exists() and source_path.is_file():
                applied.append(entry.path)
            elif target_path.exists():
                deleted.append(entry.path)
            else:
                raise FileNotFoundError(f"reported changed file is missing in run workspace and target: {entry.path}")
            continue
        if source_path.exists() and source_path.is_file():
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, target_path)
            applied.append(entry.path)
        elif target_path.exists():
            target_path.unlink()
            deleted.append(entry.path)
        else:
            raise FileNotFoundError(f"reported changed file is missing in run workspace and target: {entry.path}")
    return applied, skipped, deleted


def _build_apply_report_paths(run_dir: Path) -> tuple[Path, Path]:
    return run_dir / "APPLY_REPORT.md", run_dir / "APPLY_REPORT.json"


def write_apply_report(
    *,
    run_dir: Path,
    run_id: str,
    target_workspace: Path,
    review_gate: ReviewGateResult,
    applied_at: datetime,
    applied_files: list[str],
    deleted_files: list[str],
    skipped_files: list[str],
) -> tuple[Path, Path]:
    md_path, json_path = _build_apply_report_paths(run_dir)
    payload = {
        "schema_version": "1.0",
        "run_id": run_id,
        "status": "applied",
        "applied_at": applied_at.isoformat(),
        "target_workspace": str(target_workspace.resolve()),
        "review_gate": review_gate.review_gate,
        "applied_files": applied_files,
        "deleted_files": deleted_files,
        "skipped_files": skipped_files,
        "commit_created": False,
        "git_add_performed": False,
        "next_step": "Inspect git diff, run tests, then commit manually.",
    }
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    lines = [
        f"# Apply Report: {run_id}",
        "",
        "Status: `applied`",
        f"Target workspace: `{target_workspace.resolve()}`",
        f"Review gate: `{review_gate.review_gate}`",
        "",
        "## Applied files",
        *([f"- `{item}`" for item in applied_files] or ["- none"]),
        "",
        "## Deleted files",
        *([f"- `{item}`" for item in deleted_files] or ["- none"]),
        "",
        "## Skipped files",
        *([f"- `{item}`" for item in skipped_files] or ["- none"]),
        "",
        "## Next step",
        "",
        "Inspect:",
        "",
        "```bash",
        "git diff --stat",
        "git diff",
        "```",
        "",
        "Run tests:",
        "",
        "```bash",
        "python -m unittest discover -s tests",
        "```",
        "",
        "Then commit manually if accepted.",
    ]
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return md_path, json_path


def _record_apply_state(
    *,
    state: RunState,
    run_dir: Path,
    target_workspace: Path,
    applied_at: datetime,
    apply_report_path: Path,
    applied_files: list[str],
    deleted_files: list[str],
    skipped_files: list[str],
) -> None:
    state.apply_status = "applied"
    state.applied_at = applied_at
    state.apply_report_path = str(apply_report_path.resolve())
    state.apply_target_workspace = str(target_workspace.resolve())
    state.applied_files = list(applied_files)
    state.deleted_files = list(deleted_files)
    state.skipped_files = list(skipped_files)
    state.touch()
    state.save_json(run_dir / "state.json")


def write_acceptance_report(
    *,
    run_dir: Path,
    run_id: str,
    target_workspace: Path,
    dry_run: bool,
    commit_hash: str | None,
    no_target_changes: bool,
    review_gate: ReviewGateResult,
    applied_files: list[str],
    deleted_files: list[str],
    skipped_files: list[str],
) -> Path:
    acceptance_path = run_dir / "ACCEPTANCE.md"
    lines = [
        f"# Acceptance: {run_id}",
        "",
        f"Target workspace: `{target_workspace}`",
        f"Dry run: `{dry_run}`",
        f"Commit hash: `{commit_hash or '(none)'}`",
        f"No target changes: `{no_target_changes}`",
        "",
        "## Review gate",
        f"Decision: `{review_gate.review_decision}`",
        f"Bypassed: `{str(review_gate.review_gate_bypassed).lower()}`",
        f"Decision path: `{review_gate.review_decision_path or '(none)'}`",
        *([f"Reason: `{review_gate.review_gate_reason}`"] if review_gate.review_gate_reason else []),
        "",
        "## Applied files",
        *([f"- `{item}`" for item in applied_files] or ["- none"]),
        "",
        "## Deleted files",
        *([f"- `{item}`" for item in deleted_files] or ["- none"]),
        "",
        "## Skipped files",
        *([f"- `{item}`" for item in skipped_files] or ["- none"]),
    ]
    acceptance_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return acceptance_path


def apply_run(
    *,
    run_id: str,
    runs_dir: Path,
    target_workspace_override: str | None = None,
    dry_run: bool = False,
    allow_unreviewed: bool = False,
) -> ApplyRunResult:
    prepared = prepare_run_application(
        run_id=run_id,
        runs_dir=runs_dir,
        target_workspace_override=target_workspace_override,
        dry_run=dry_run,
        allow_unreviewed=allow_unreviewed,
        action_name="apply-run",
    )
    applied, skipped, deleted = apply_run_changes(prepared, dry_run=dry_run)
    if dry_run:
        return ApplyRunResult(
            run_id=run_id,
            target_workspace=prepared.target_workspace,
            applied_files=applied,
            skipped_files=skipped,
            deleted_files=deleted,
            review_gate=prepared.review_gate.review_gate,
            review_decision=prepared.review_gate.review_decision,
            review_decision_path=prepared.review_gate.review_decision_path,
            review_gate_bypassed=prepared.review_gate.review_gate_bypassed,
            apply_report_path=None,
            apply_report_json_path=None,
            target_status="clean",
        )

    status_after_apply = _run_git(prepared.target_workspace, ["status", "--porcelain"], check=True).stdout.strip()
    if not status_after_apply:
        raise ValueError("apply-run found no target changes to inspect")

    applied_at = datetime.now(timezone.utc)
    apply_report_path, apply_report_json_path = write_apply_report(
        run_dir=prepared.context.run_dir,
        run_id=run_id,
        target_workspace=prepared.target_workspace,
        review_gate=prepared.review_gate,
        applied_at=applied_at,
        applied_files=applied,
        deleted_files=deleted,
        skipped_files=skipped,
    )
    _record_apply_state(
        state=prepared.context.state,
        run_dir=prepared.context.run_dir,
        target_workspace=prepared.target_workspace,
        applied_at=applied_at,
        apply_report_path=apply_report_path,
        applied_files=applied,
        deleted_files=deleted,
        skipped_files=skipped,
    )
    return ApplyRunResult(
        run_id=run_id,
        target_workspace=prepared.target_workspace,
        applied_files=applied,
        skipped_files=skipped,
        deleted_files=deleted,
        review_gate=prepared.review_gate.review_gate,
        review_decision=prepared.review_gate.review_decision,
        review_decision_path=prepared.review_gate.review_decision_path,
        review_gate_bypassed=prepared.review_gate.review_gate_bypassed,
        apply_report_path=apply_report_path,
        apply_report_json_path=apply_report_json_path,
        target_status="dirty",
    )


def accept_run(
    *,
    run_id: str,
    runs_dir: Path,
    target_workspace_override: str | None = None,
    commit_message: str | None = None,
    dry_run: bool = False,
    init_target_git: bool = False,
    allow_unreviewed: bool = False,
) -> AcceptRunResult:
    prepared = prepare_run_application(
        run_id=run_id,
        runs_dir=runs_dir,
        target_workspace_override=target_workspace_override,
        dry_run=dry_run,
        allow_unreviewed=allow_unreviewed,
        action_name="accept-run",
        init_target_git=init_target_git,
    )
    applied, skipped, deleted = apply_run_changes(prepared, dry_run=dry_run)

    commit_hash: str | None = None
    no_target_changes = False
    if not dry_run:
        paths_to_stage = applied + deleted
        if paths_to_stage:
            _run_git(prepared.target_workspace, ["add", "--", *paths_to_stage], check=True)
        status_after_apply = _run_git(prepared.target_workspace, ["status", "--porcelain"], check=True).stdout.strip()
        if not status_after_apply:
            if not init_target_git:
                raise ValueError("accept-run found no target changes to commit")
            no_target_changes = True
            head = _run_git(prepared.target_workspace, ["rev-parse", "--short", "HEAD"], check=False)
            commit_hash = head.stdout.strip() or None
        else:
            message = commit_message or f"chore: accept orchestrator run {run_id}"
            _run_git(prepared.target_workspace, ["commit", "-m", message], check=True)
            commit_hash = _run_git(prepared.target_workspace, ["rev-parse", "--short", "HEAD"], check=True).stdout.strip()

    acceptance_path = write_acceptance_report(
        run_dir=prepared.context.run_dir,
        run_id=run_id,
        target_workspace=prepared.target_workspace,
        dry_run=dry_run,
        commit_hash=commit_hash,
        no_target_changes=no_target_changes,
        review_gate=prepared.review_gate,
        applied_files=applied,
        deleted_files=deleted,
        skipped_files=skipped,
    )
    return AcceptRunResult(
        run_id=run_id,
        target_workspace=prepared.target_workspace,
        applied_files=applied,
        skipped_files=skipped,
        deleted_files=deleted,
        commit_hash=commit_hash,
        acceptance_path=acceptance_path,
        review_gate=prepared.review_gate.review_gate,
        review_decision=prepared.review_gate.review_decision,
        review_decision_path=prepared.review_gate.review_decision_path,
        review_gate_bypassed=prepared.review_gate.review_gate_bypassed,
        no_target_changes=no_target_changes,
    )
