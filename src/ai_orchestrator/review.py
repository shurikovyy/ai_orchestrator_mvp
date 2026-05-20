from __future__ import annotations

"""Review packet generation and diff preview helpers.

Apply/commit internals live in ``ai_orchestrator.apply``.
"""

import difflib
from dataclasses import dataclass, field
from pathlib import Path

from ai_orchestrator.apply import (
    ApplyFileEntry as ReviewFileEntry,
    RunApplicationContext,
    build_run_application_context,
)
from ai_orchestrator.schemas import RunState, StructuredExecutionReport


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


def _build_rework_context_lines(state: RunState) -> list[str]:
    if not state.task.rework_of_run_id:
        return []
    excerpt = (state.task.rework_feedback or "").strip().replace("\r\n", "\n")
    if len(excerpt) > 1200:
        excerpt = excerpt[:1200].rstrip() + "\n... feedback excerpt clipped ..."
    return [
        "## Rework context",
        f"- Source run: `{state.task.rework_of_run_id}`",
        f"- Feedback file: `{state.task.rework_feedback_path or '(none)'}`",
        "- Feedback excerpt:",
        "",
        "```text",
        excerpt or "(none)",
        "```",
        "",
    ]


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


def _build_diff_text(context: RunApplicationContext) -> str:
    if context.workspace_dir is None:
        return ""
    diff_chunks: list[str] = []
    for entry in context.changed_files:
        if not entry.apply_to_target or entry.status not in {"added", "modified", "deleted"}:
            continue
        rel = Path(entry.path)
        source_path = context.workspace_dir / rel
        target_path = context.target_workspace / rel if context.target_workspace is not None else None
        diff = _build_file_diff(
            old_path=target_path if target_path is not None else None,
            new_path=source_path if source_path.exists() else None,
            display_path=entry.path,
        )
        if diff:
            diff_chunks.append(diff)
    return "\n\n".join(diff_chunks)


def build_review_packet_data(run_dir: Path, *, target_workspace_override: str | None = None) -> ReviewPacketData:
    context = build_run_application_context(run_dir, target_workspace_override=target_workspace_override)
    return ReviewPacketData(
        run_dir=run_dir,
        state=context.state,
        report=context.report,
        report_source=context.report_source,
        workspace_dir=context.workspace_dir,
        target_workspace=context.target_workspace,
        changed_files=context.changed_files,
        diff_text=_build_diff_text(context),
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
    ]
    lines.extend(_build_rework_context_lines(state))
    lines.append("## Structured report")
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
