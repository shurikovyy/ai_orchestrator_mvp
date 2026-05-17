from __future__ import annotations

import difflib
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from ai_orchestrator.schemas import RunState, StructuredExecutionReport
from ai_orchestrator.validation import (
    _normalize_manifest_path,
    find_workspace_baseline_manifest_path,
    is_manifest_ignored_path,
    is_manifest_reportable_file,
    load_structured_report,
)

_RUNTIME_REPORT_FILES = {"EXECUTION_REPORT.json"}


@dataclass(frozen=True)
class ReviewFileEntry:
    path: str
    status: str
    apply_to_target: bool
    reason: str = ""


@dataclass(frozen=True)
class ReviewPacketData:
    run_dir: Path
    state: RunState
    report: StructuredExecutionReport | None
    report_source: str | None
    workspace_dir: Path | None
    target_workspace: Path | None
    changed_files: list[ReviewFileEntry] = field(default_factory=list)
    diff_text: str = ""


@dataclass(frozen=True)
class AcceptRunResult:
    run_id: str
    target_workspace: Path
    applied_files: list[str]
    skipped_files: list[str]
    deleted_files: list[str]
    commit_hash: str | None
    acceptance_path: Path
    no_target_changes: bool = False


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
    return RunState.model_validate_json(state_path.read_text(encoding="utf-8"))


def _read_text_or_empty(path: Path) -> list[str]:
    if not path.exists() or not path.is_file():
        return []
    return path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)


def _build_file_diff(*, old_path: Path | None, new_path: Path | None, display_path: str, max_lines: int = 120) -> str:
    old_lines = _read_text_or_empty(old_path) if old_path else []
    new_lines = _read_text_or_empty(new_path) if new_path else []
    diff_lines = list(
        difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=f"baseline/{display_path}",
            tofile=f"workspace/{display_path}",
            lineterm="",
        )
    )
    if not diff_lines:
        return ""
    if len(diff_lines) > max_lines:
        clipped = diff_lines[:max_lines]
        clipped.append(f"... diff clipped after {max_lines} lines ...")
        diff_lines = clipped
    return "\n".join(diff_lines)


def _target_workspace_from_state(state: RunState, override: str | None = None) -> Path | None:
    raw = override or state.task.seed_workspace_path
    if not raw:
        return None
    return Path(raw).expanduser().resolve()


def build_review_packet_data(run_dir: Path, *, target_workspace_override: str | None = None) -> ReviewPacketData:
    state = load_run_state(run_dir)
    execution = _latest_execution(state)
    loaded_report = load_structured_report(execution)
    report = loaded_report.report
    workspace_dir = loaded_report.source_path.parent if loaded_report.source_path else None
    target_workspace = _target_workspace_from_state(state, target_workspace_override)

    entries: list[ReviewFileEntry] = []
    diff_chunks: list[str] = []
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

            entries.append(ReviewFileEntry(path=safe_path, status=status, apply_to_target=apply_to_target, reason=reason))
            if apply_to_target and status in {"added", "modified", "deleted"}:
                diff = _build_file_diff(
                    old_path=target_path if target_path is not None else None,
                    new_path=source_path if source_path.exists() else None,
                    display_path=safe_path,
                )
                if diff:
                    diff_chunks.append(diff)

    return ReviewPacketData(
        run_dir=run_dir,
        state=state,
        report=report,
        report_source=loaded_report.source,
        workspace_dir=workspace_dir,
        target_workspace=target_workspace,
        changed_files=entries,
        diff_text="\n\n".join(diff_chunks),
    )


def write_review_packet(run_dir: Path, *, target_workspace_override: str | None = None) -> Path:
    data = build_review_packet_data(run_dir, target_workspace_override=target_workspace_override)
    path = run_dir / "REVIEW_PACKET.md"
    state = data.state
    report = data.report

    lines: list[str] = [
        f"# Review Packet: {state.run_id}",
        "",
        f"Run status: `{state.final_status}`",
        f"Target workspace: `{data.target_workspace or '(none)'}`",
        f"Run workspace: `{data.workspace_dir or '(unknown)'}`",
        "",
        "## Task",
        state.task.description,
        "",
        "## Structured report",
    ]
    if report is None:
        lines.append("No valid EXECUTION_REPORT.json was found.")
    else:
        lines.extend([
            f"Source: `{data.report_source}`",
            f"Report status: `{report.status}`",
            f"Summary: {report.summary}",
            "",
            "### Tests",
        ])
        if report.tests:
            for test in report.tests:
                lines.append(
                    f"- `{test.command}` -> `{test.status}`"
                    + (f" ({test.passed}/{test.total} passed)" if test.total is not None and test.passed is not None else "")
                )
        else:
            lines.append("- none")
        lines.extend(["", "### Risks", *([f"- {item}" for item in report.risks] or ["- none"])])
        lines.extend(["", "### Assumptions", *([f"- {item}" for item in report.assumptions] or ["- none"])])

    lines.extend(["", "## Validation feedback"])
    if state.validations:
        for validation in state.validations:
            lines.append(f"- step={validation.step_id}, attempt={validation.attempt}, approved={validation.approved}, score={validation.score:.2f}")
            for item in validation.feedback:
                lines.append(f"  - {item}")
    else:
        lines.append("- none")

    lines.extend(["", "## Changed files and apply plan"])
    if data.changed_files:
        lines.append("| File | Status | Apply to target | Note |")
        lines.append("|---|---:|---:|---|")
        for entry in data.changed_files:
            lines.append(
                f"| `{entry.path}` | `{entry.status}` | `{'yes' if entry.apply_to_target else 'no'}` | {entry.reason or ''} |"
            )
    else:
        lines.append("No changed files were reported.")

    lines.extend([
        "",
        "## Diff preview",
        "",
    ])
    if data.diff_text:
        lines.extend(["```diff", data.diff_text, "```"])
    else:
        lines.append("No applicable target diff preview is available.")

    lines.extend([
        "",
        "## Accept command",
        "",
        "After review, apply and commit the approved files with:",
        "",
        "```bash",
        f"./.venv/Scripts/python.exe -m ai_orchestrator.cli accept-run {state.run_id} --runs-dir {data.run_dir.parent}",
        "```",
        "",
        "The command refuses non-approved runs, dirty target repos, missing reports, unsafe paths, and generated/runtime files.",
    ])

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


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


def accept_run(
    *,
    run_id: str,
    runs_dir: Path,
    target_workspace_override: str | None = None,
    commit_message: str | None = None,
    dry_run: bool = False,
    init_target_git: bool = False,
) -> AcceptRunResult:
    run_dir = runs_dir / run_id
    data = build_review_packet_data(run_dir, target_workspace_override=target_workspace_override)
    state = data.state
    if state.final_status != "approved":
        raise ValueError(f"run {run_id} is not approved: {state.final_status}")
    if data.report is None:
        raise ValueError("cannot accept run without a valid EXECUTION_REPORT.json")
    if data.workspace_dir is None or not data.workspace_dir.exists():
        raise ValueError("cannot infer run workspace from EXECUTION_REPORT.json")
    if data.target_workspace is None:
        raise ValueError("accept-run requires a seed workspace or --target-workspace")
    target_workspace = data.target_workspace
    if not target_workspace.exists() or not target_workspace.is_dir():
        raise FileNotFoundError(f"target workspace does not exist or is not a directory: {target_workspace}")
    if not (target_workspace / ".git").exists():
        if not init_target_git:
            raise ValueError(
                f"target workspace is not a git repository: {target_workspace}. "
                "Initialize it first with `git init && git add . && git commit -m baseline`, "
                "or rerun accept-run with --init-target-git for disposable/toy workspaces."
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

    dirty_before = _run_git(target_workspace, ["status", "--porcelain"], check=True).stdout.strip()
    if dirty_before:
        raise ValueError("target git repository is dirty; commit/stash changes before accept-run")

    applied: list[str] = []
    skipped: list[str] = []
    deleted: list[str] = []
    for entry in data.changed_files:
        if not entry.apply_to_target:
            skipped.append(entry.path)
            continue
        rel = Path(_safe_relative_path(entry.path))
        source_path = data.workspace_dir / rel
        target_path = target_workspace / rel
        if dry_run:
            applied.append(entry.path)
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

    commit_hash: str | None = None
    no_target_changes = False
    if not dry_run:
        paths_to_stage = applied + deleted
        if paths_to_stage:
            _run_git(target_workspace, ["add", "--", *paths_to_stage], check=True)
        status_after_apply = _run_git(target_workspace, ["status", "--porcelain"], check=True).stdout.strip()
        if not status_after_apply:
            # Disposable --init-target-git runs can legitimately become no-ops when the
            # target already contains the accepted file contents (for example after a
            # previous manual apply or a rerun of the same accepted run). Treat this as
            # idempotent acceptance, but keep the stricter error for normal target repos.
            if not init_target_git:
                raise ValueError("accept-run found no target changes to commit")
            no_target_changes = True
            head = _run_git(target_workspace, ["rev-parse", "--short", "HEAD"], check=False)
            commit_hash = head.stdout.strip() or None
        else:
            message = commit_message or f"chore: accept orchestrator run {run_id}"
            _run_git(target_workspace, ["commit", "-m", message], check=True)
            commit_hash = _run_git(target_workspace, ["rev-parse", "--short", "HEAD"], check=True).stdout.strip()

    acceptance_path = run_dir / "ACCEPTANCE.md"
    lines = [
        f"# Acceptance: {run_id}",
        "",
        f"Target workspace: `{target_workspace}`",
        f"Dry run: `{dry_run}`",
        f"Commit hash: `{commit_hash or '(none)'}`",
        f"No target changes: `{no_target_changes}`",
        "",
        "## Applied files",
        *([f"- `{item}`" for item in applied] or ["- none"]),
        "",
        "## Deleted files",
        *([f"- `{item}`" for item in deleted] or ["- none"]),
        "",
        "## Skipped files",
        *([f"- `{item}`" for item in skipped] or ["- none"]),
    ]
    acceptance_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return AcceptRunResult(
        run_id=run_id,
        target_workspace=target_workspace,
        applied_files=applied,
        skipped_files=skipped,
        deleted_files=deleted,
        commit_hash=commit_hash,
        acceptance_path=acceptance_path,
        no_target_changes=no_target_changes,
    )
