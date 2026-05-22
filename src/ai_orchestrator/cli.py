from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ai_orchestrator.apply import accept_run, apply_run
from ai_orchestrator.backends import Backend
from ai_orchestrator.deterministic_review import run_deterministic_review_checks
from ai_orchestrator.doctor import format_doctor_json, format_doctor_text, run_doctor
from ai_orchestrator.findings_feedback import create_findings_feedback_for_run
from ai_orchestrator.pipeline_status import (
    build_pipeline_status_summary,
    format_pipeline_status_json,
    format_pipeline_status_text,
)
from ai_orchestrator.pipeline import PipelinePlan, PipelineRunResult, run_pipeline
from ai_orchestrator.review_findings import record_review_findings
from ai_orchestrator.review_profiles import (
    format_review_profile_json,
    format_review_profile_text,
    format_review_profiles_json,
    format_review_profiles_text,
    get_review_profile,
    list_review_profiles,
)
from ai_orchestrator.risk_classification import classify_run_risk
from ai_orchestrator.reviewer_prompts import prepare_review_prompts
from ai_orchestrator.run_status import build_run_status_summary, format_run_status_json, format_run_status_text
from ai_orchestrator.task_drafts import create_task_draft_scaffold
from ai_orchestrator.task_draft_validation import validate_task_draft
from ai_orchestrator.rework import execute_rework_run
from ai_orchestrator.review_decision import load_review_target_run, record_review_decision
from ai_orchestrator.schemas import RunState, TaskSpec
from ai_orchestrator.task_queue import TaskSummaryList, list_task_summaries, load_task_queue_config, resolve_task_definition
from ai_orchestrator.task_runner import (
    RunCommandConfig,
    build_run_config_from_resolved_task,
    execute_run,
    get_run_artifact_paths,
)


def build_run_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ai-orchestrator",
        description="Run a deterministic plan/execute/validate/rework workflow.",
    )
    parser.add_argument("task", help="Task description to execute")
    parser.add_argument(
        "--criteria",
        action="append",
        default=[],
        help="Acceptance criterion. Repeat this option for multiple criteria.",
    )
    parser.add_argument(
        "--backend",
        choices=["mock", "codex_cli", "codex"],
        default="mock",
        help="Execution backend. `mock` is offline; `codex_cli` delegates execution to Codex CLI.",
    )
    parser.add_argument(
        "--runs-dir",
        default=".runs",
        help="Directory where run state, logs, and artifacts are stored.",
    )
    parser.add_argument("--max-retries", type=int, default=2, help="Maximum retries after the first failed attempt")
    parser.add_argument(
        "--codex-cmd",
        default=None,
        help=(
            "Command used by the codex_cli backend. Examples: 'codex', "
            "'./node_modules/.bin/codex', 'npx --yes @openai/codex'. "
            "Can also be set with AI_ORCHESTRATOR_CODEX_CMD."
        ),
    )
    parser.add_argument(
        "--require-structured-report",
        action="store_true",
        help="Require EXECUTION_REPORT.json and validate it with the Pydantic schema before approving the run.",
    )
    parser.add_argument(
        "--rerun-report-test-commands",
        action="store_true",
        help=(
            "After parsing EXECUTION_REPORT.json, rerun report.tests[*].command in the workspace. "
            "Only allowlisted test commands are executed."
        ),
    )
    parser.add_argument(
        "--validate-workspace-manifest",
        action="store_true",
        help=(
            "After parsing EXECUTION_REPORT.json, compare report.changed_files against reportable files "
            "that actually exist in the workspace. Runtime/cache files are ignored."
        ),
    )
    parser.add_argument(
        "--seed-workspace",
        default=None,
        help=(
            "Optional existing project directory to copy into the isolated run workspace before Codex executes. "
            "Use a Windows-style path when running Windows Python from Git Bash, for example: $(pwd -W)/toy_seed_repo."
        ),
    )
    parser.add_argument(
        "--validation-command-timeout",
        type=int,
        default=60,
        help="Timeout in seconds for each validator-rerun test command.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print orchestrator progress logs to the console while a run is executing.",
    )
    parser.add_argument(
        "--stream-codex-output",
        action="store_true",
        help="Stream Codex CLI stdout/stderr to the console while codex exec is running.",
    )
    return parser


def build_run_task_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ai-orchestrator run-task",
        description="Load one task from tasks.yaml and execute it through the existing orchestrator engine.",
    )
    parser.add_argument("task_id", help="Task id to load from tasks.yaml")
    parser.add_argument(
        "--tasks-file",
        required=True,
        help="Path to tasks.yaml containing project defaults and task definitions.",
    )
    parser.add_argument(
        "--backend",
        choices=["mock", "codex_cli", "codex"],
        default=None,
        help="Optional backend override. Takes priority over task/defaults.",
    )
    parser.add_argument(
        "--codex-cmd",
        default=None,
        help="Optional Codex CLI command override. Takes priority over task/defaults.",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=None,
        help="Optional retry override. Takes priority over task/defaults.",
    )
    parser.add_argument(
        "--runs-dir",
        default=".runs",
        help="Directory where run state, logs, and artifacts are stored.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        default=None,
        help="Force verbose orchestrator logging for this run-task execution.",
    )
    parser.add_argument(
        "--stream-codex-output",
        action="store_true",
        default=None,
        help="Force live Codex CLI stdout/stderr streaming for this run-task execution.",
    )
    return parser


def build_run_pipeline_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ai-orchestrator run-pipeline",
        description="Run multiple tasks from tasks.yaml in declaration order.",
    )
    parser.add_argument(
        "--tasks-file",
        required=True,
        help="Path to tasks.yaml containing project defaults and task definitions.",
    )
    parser.add_argument(
        "--from-task",
        default=None,
        help="Start pipeline execution from the specified task id, inclusive.",
    )
    parser.add_argument(
        "--only",
        action="append",
        default=None,
        help="Run only the specified task id. Repeat to select multiple tasks.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the task execution plan without creating pipeline artifacts or executing tasks.",
    )
    parser.add_argument(
        "--continue-on-failure",
        action="store_true",
        help="Continue executing later tasks even if an earlier task fails.",
    )
    parser.add_argument(
        "--backend",
        choices=["mock", "codex_cli", "codex"],
        default=None,
        help="Optional backend override. Takes priority over task/defaults.",
    )
    parser.add_argument(
        "--codex-cmd",
        default=None,
        help="Optional Codex CLI command override. Takes priority over task/defaults.",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=None,
        help="Optional retry override. Takes priority over task/defaults.",
    )
    parser.add_argument(
        "--runs-dir",
        default=".runs",
        help="Directory where run state, logs, and pipeline artifacts are stored.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        default=None,
        help="Print pipeline and orchestrator progress logs to the console.",
    )
    parser.add_argument(
        "--stream-codex-output",
        action="store_true",
        default=None,
        help="Force live Codex CLI stdout/stderr streaming for every selected task.",
    )
    return parser


def build_list_tasks_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ai-orchestrator list-tasks",
        description="List task definitions from tasks.yaml without executing anything.",
    )
    parser.add_argument(
        "--tasks-file",
        required=True,
        help="Path to tasks.yaml or tasks.yaml.example containing task definitions.",
    )
    parser.add_argument(
        "--enabled-only",
        action="store_true",
        help="Show only enabled tasks.",
    )
    parser.add_argument(
        "--disabled-only",
        action="store_true",
        help="Show only disabled tasks.",
    )
    parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format. Defaults to text.",
    )
    return parser


def build_accept_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ai-orchestrator accept-run",
        description="Apply an approved run's changed files to the seed/target git repo and commit them.",
    )
    parser.add_argument("run_id", help="Run id to accept, for example run_20260516_122835_a9e9c2")
    parser.add_argument("--runs-dir", default=".runs", help="Directory containing run state and artifacts.")
    parser.add_argument(
        "--target-workspace",
        default=None,
        help="Optional git repo to apply changes to. Defaults to the run's seed_workspace_path.",
    )
    parser.add_argument(
        "--commit-message",
        default=None,
        help="Git commit message. Defaults to 'chore: accept orchestrator run <run_id>'.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and print what would be applied, but do not modify or commit the target repo.",
    )
    parser.add_argument(
        "--init-target-git",
        action="store_true",
        help=(
            "If the target workspace is not a git repo, initialize it and create a baseline commit before applying. "
            "Use this only for disposable/toy seed workspaces, not production repos."
        ),
    )
    parser.add_argument(
        "--allow-unreviewed",
        action="store_true",
        help=(
            "Allow accepting a validator-approved run without a recorded human review approval. "
            "This does not override rejected human reviews."
        ),
    )
    return parser


def build_apply_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ai-orchestrator apply-run",
        description="Apply an approved run's changed files to the seed/target git repo without staging or committing.",
    )
    parser.add_argument("run_id", help="Run id to apply, for example run_20260516_122835_a9e9c2")
    parser.add_argument("--runs-dir", default=".runs", help="Directory containing run state and artifacts.")
    parser.add_argument(
        "--target-workspace",
        default=None,
        help="Optional git repo to apply changes to. Defaults to the run's seed_workspace_path.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and print what would be applied, but do not modify the target repo.",
    )
    parser.add_argument(
        "--allow-unreviewed",
        action="store_true",
        help=(
            "Allow applying a validator-approved run without a recorded human review approval. "
            "This does not override rejected human reviews."
        ),
    )
    return parser


def build_show_run_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ai-orchestrator show-run",
        description="Show the lifecycle/status summary for one existing run without modifying any artifacts.",
    )
    parser.add_argument("run_id", help="Run id to inspect.")
    parser.add_argument("--runs-dir", default=".runs", help="Directory containing run state and artifacts.")
    parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format. Defaults to text.",
    )
    parser.add_argument(
        "--show-paths",
        action="store_true",
        help="Include key artifact paths in text output. JSON always includes artifact paths.",
    )
    return parser


def build_show_pipeline_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ai-orchestrator show-pipeline",
        description="Show the lifecycle/status summary for one existing pipeline without modifying any artifacts.",
    )
    parser.add_argument("pipeline_id", help="Pipeline id to inspect.")
    parser.add_argument("--runs-dir", default=".runs", help="Directory containing run and pipeline artifacts.")
    parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format. Defaults to text.",
    )
    parser.add_argument(
        "--show-paths",
        action="store_true",
        help="Include key pipeline artifact paths in text output. JSON always includes artifact paths.",
    )
    return parser


def build_rework_run_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ai-orchestrator rework-run",
        description="Create a new run from an existing run plus explicit human review feedback.",
    )
    parser.add_argument("source_run_id", help="Existing run id to rework.")
    parser.add_argument("--runs-dir", default=".runs", help="Directory containing run state and artifacts.")
    parser.add_argument(
        "--feedback",
        default=None,
        help=(
            "Optional path to a markdown/text file with human review feedback. "
            "If omitted, rework-run will try to use stored feedback from a rejected human review decision."
        ),
    )
    parser.add_argument(
        "--backend",
        choices=["mock", "codex_cli", "codex"],
        default=None,
        help="Optional backend override. Takes priority over the source run backend.",
    )
    parser.add_argument(
        "--codex-cmd",
        default=None,
        help="Optional Codex CLI command override for codex_cli rework runs.",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=None,
        help="Optional retry override for the new rework run.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print orchestrator progress logs to the console while the rework run is executing.",
    )
    parser.add_argument(
        "--stream-codex-output",
        action="store_true",
        help="Stream Codex CLI stdout/stderr to the console while codex exec is running.",
    )
    return parser


def build_review_run_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ai-orchestrator review-run",
        description="Record an explicit human review decision for an existing approved run.",
    )
    parser.add_argument("run_id", help="Existing run id to review.")
    parser.add_argument("--runs-dir", default=".runs", help="Directory containing run state and artifacts.")
    parser.add_argument(
        "--decision",
        required=True,
        choices=["approved", "rejected"],
        help="Human review decision to record.",
    )
    parser.add_argument(
        "--feedback",
        default=None,
        help="Optional feedback file for approved decisions; required for rejected decisions.",
    )
    parser.add_argument(
        "--from-findings",
        action="store_true",
        help="Generate or reuse rejected-review feedback from REVIEW_FINDINGS.json and use it as the feedback source.",
    )
    parser.add_argument(
        "--force-feedback",
        action="store_true",
        help="With --from-findings, regenerate REVIEW_FEEDBACK_FROM_FINDINGS.md if it already exists.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing human review decision for this run.",
    )
    return parser


def build_record_findings_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ai-orchestrator record-findings",
        description="Record structured review findings for an existing run without modifying target repos.",
    )
    parser.add_argument("run_id", help="Existing run id to attach findings to.")
    parser.add_argument("--runs-dir", default=".runs", help="Directory containing run state and artifacts.")
    parser.add_argument(
        "--findings-file",
        required=True,
        help="Path to a REVIEW_FINDINGS-like JSON file.",
    )
    parser.add_argument(
        "--profile",
        default=None,
        help="Optional reviewer profile id. When provided, enforce reviewer/category constraints for that profile.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing REVIEW_FINDINGS.json/REVIEW_FINDINGS.md for this run.",
    )
    return parser


def build_findings_feedback_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ai-orchestrator findings-feedback",
        description="Generate rework feedback markdown from REVIEW_FINDINGS.json without changing target repos.",
    )
    parser.add_argument("run_id", help="Existing run id to generate findings feedback for.")
    parser.add_argument("--runs-dir", default=".runs", help="Directory containing run state and artifacts.")
    parser.add_argument(
        "--output",
        default=None,
        help="Optional output path. Defaults to .runs/<run_id>/REVIEW_FEEDBACK_FROM_FINDINGS.md.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing findings feedback file.",
    )
    parser.add_argument(
        "--include-non-blocking",
        action="store_true",
        help="Include open minor/nit findings as secondary suggestions.",
    )
    parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format. Defaults to text.",
    )
    return parser


def build_run_review_checks_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ai-orchestrator run-review-checks",
        description="Run deterministic review checks and record REVIEW_FINDINGS artifacts for an existing run.",
    )
    parser.add_argument("run_id", help="Existing run id to review deterministically.")
    parser.add_argument("--runs-dir", default=".runs", help="Directory containing run state and artifacts.")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing REVIEW_FINDINGS artifacts for this run.",
    )
    parser.add_argument(
        "--profile",
        action="append",
        default=None,
        choices=["default", "docs-only", "code-safety"],
        help="Deterministic review profile to apply. Repeat to combine multiple profiles.",
    )
    return parser


def build_classify_run_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ai-orchestrator classify-run",
        description="Classify run risk deterministically and record required reviewer profiles without modifying target repos.",
    )
    parser.add_argument("run_id", help="Existing run id to classify.")
    parser.add_argument("--runs-dir", default=".runs", help="Directory containing run state and artifacts.")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing risk classification artifacts for this run.",
    )
    parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format. Defaults to text.",
    )
    return parser


def build_doctor_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ai-orchestrator doctor",
        description="Run read-only preflight checks for repository and environment readiness.",
    )
    parser.add_argument(
        "--tasks-file",
        default=None,
        help="Optional tasks.yaml/tasks.yaml.example path to validate.",
    )
    parser.add_argument(
        "--task-id",
        default=None,
        help="Optional task id to validate within --tasks-file.",
    )
    parser.add_argument(
        "--codex-cmd",
        default=None,
        help="Optional Codex CLI command to verify via `<command> --version`.",
    )
    parser.add_argument(
        "--skip-tests",
        action="store_true",
        help="Skip `python -m unittest discover -s tests` during doctor checks.",
    )
    parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format. Defaults to text.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Treat warnings as nonzero exit status.",
    )
    return parser


def build_prepare_review_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ai-orchestrator prepare-review",
        description="Prepare reviewer prompt packets for one run without running any reviewer agents.",
    )
    parser.add_argument("run_id", help="Existing run id to prepare reviewer prompt packets for.")
    parser.add_argument("--runs-dir", default=".runs", help="Directory containing run state and artifacts.")
    profile_group = parser.add_mutually_exclusive_group(required=True)
    profile_group.add_argument(
        "--profile",
        action="append",
        default=None,
        help="Reviewer profile id to prepare. Repeat to generate multiple prompt packets.",
    )
    profile_group.add_argument(
        "--all-profiles",
        action="store_true",
        help="Prepare prompt packets for all built-in profiles except deterministic.",
    )
    profile_group.add_argument(
        "--required-profiles",
        action="store_true",
        help="Prepare prompt packets for required_review_profiles from RISK_CLASSIFICATION.json.",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Optional output directory. Defaults to .runs/<run_id>/reviewer_prompts.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite selected prompt files if they already exist.",
    )
    parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format. Defaults to text.",
    )
    return parser


def build_list_review_profiles_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ai-orchestrator list-review-profiles",
        description="List built-in reviewer profile contracts without running any reviewer agents.",
    )
    parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format. Defaults to text.",
    )
    return parser


def build_show_review_profile_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ai-orchestrator show-review-profile",
        description="Show one built-in reviewer profile contract without executing any review logic.",
    )
    parser.add_argument("profile_id", help="Review profile id to inspect, for example qa or security.")
    parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format. Defaults to text.",
    )
    return parser


def build_draft_task_scaffold_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ai-orchestrator draft-task-scaffold",
        description="Create a deterministic task draft scaffold from a raw natural-language request without modifying tasks.yaml.",
    )
    parser.add_argument(
        "--request",
        required=True,
        help="Path to a markdown/text file containing the raw task request.",
    )
    parser.add_argument(
        "--output-dir",
        default=".task_drafts",
        help="Directory where draft folders will be created. Defaults to .task_drafts.",
    )
    parser.add_argument(
        "--task-id",
        default=None,
        help="Optional explicit target task id for the draft. If omitted, derive a safe id from the title or draft_id.",
    )
    parser.add_argument(
        "--title",
        default=None,
        help="Optional explicit draft title. If omitted, derive it from the first non-empty request line.",
    )
    parser.add_argument(
        "--risk-level",
        choices=["low", "medium", "high", "critical", "unknown"],
        default="unknown",
        help="Initial deterministic risk level placeholder. Defaults to unknown.",
    )
    parser.add_argument(
        "--prompt-language",
        default="ru",
        help="Language marker for generated prompt artifacts. Defaults to ru.",
    )
    parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format. Defaults to text.",
    )
    return parser


def build_validate_task_draft_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ai-orchestrator validate-task-draft",
        description="Validate one task draft deterministically without modifying tasks.yaml or running any execution backends.",
    )
    parser.add_argument("draft_id", help="Existing draft id to validate, for example draft_20260522_180244_31eb0d.")
    parser.add_argument(
        "--drafts-dir",
        default=".task_drafts",
        help="Directory containing draft folders. Defaults to .task_drafts.",
    )
    parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format. Defaults to text.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing task_draft_validator_report artifacts if they already exist.",
    )
    return parser


def _print_run_summary(
    *,
    task_id: str | None,
    state: RunState,
    backend: Backend,
    runs_dir: Path,
    absolute_paths: bool = False,
) -> None:
    final_report, review_packet, state_path = get_run_artifact_paths(runs_dir, state.run_id)
    if absolute_paths:
        final_report = final_report.resolve()
        review_packet = review_packet.resolve()
        state_path = state_path.resolve()
    if task_id is not None:
        print(f"task_id={task_id}")
    print(f"run_id={state.run_id}")
    print(f"status={state.final_status}")
    print(f"backend={backend.name}")
    print(f"final_report={final_report}")
    if review_packet.exists():
        print(f"review_packet={review_packet}")
    print(f"state={state_path}")


def build_run_command_config_from_args(args: argparse.Namespace) -> RunCommandConfig:
    task = TaskSpec(
        description=args.task,
        acceptance_criteria=args.criteria,
        max_retries=args.max_retries,
        require_structured_report=args.require_structured_report,
        rerun_report_test_commands=args.rerun_report_test_commands,
        validate_workspace_manifest=args.validate_workspace_manifest,
        seed_workspace_path=args.seed_workspace,
        validation_command_timeout_seconds=args.validation_command_timeout,
    )
    return RunCommandConfig(
        task=task,
        backend_name=args.backend,
        runs_dir=Path(args.runs_dir),
        codex_cmd=args.codex_cmd,
        verbose=args.verbose,
        stream_codex_output=args.stream_codex_output,
    )


def build_run_task_command_config_from_args(args: argparse.Namespace) -> tuple[str, RunCommandConfig]:
    queue_config = load_task_queue_config(args.tasks_file)
    resolved = resolve_task_definition(
        queue_config,
        task_id=args.task_id,
        tasks_file=args.tasks_file,
        backend=args.backend,
        codex_cmd=args.codex_cmd,
        max_retries=args.max_retries,
        verbose=args.verbose,
        stream_codex_output=args.stream_codex_output,
    )
    return resolved.task_id, build_run_config_from_resolved_task(resolved, runs_dir=args.runs_dir)


def run_main(argv: list[str] | None = None) -> int:
    parser = build_run_parser()
    args = parser.parse_args(argv)
    config = build_run_command_config_from_args(args)
    state, backend = execute_run(config)
    _print_run_summary(task_id=None, state=state, backend=backend, runs_dir=config.runs_dir)
    return 0 if state.final_status == "approved" else 1


def run_task_main(argv: list[str] | None = None) -> int:
    parser = build_run_task_parser()
    args = parser.parse_args(argv)
    try:
        task_id, config = build_run_task_command_config_from_args(args)
        state, backend = execute_run(config)
    except Exception as exc:  # noqa: BLE001 - CLI should print deterministic error text.
        print(f"task_id={args.task_id}")
        print("status=failed")
        print(f"error={exc}")
        return 1
    _print_run_summary(task_id=task_id, state=state, backend=backend, runs_dir=config.runs_dir, absolute_paths=True)
    return 0 if state.final_status == "approved" else 1


def _print_pipeline_dry_run(plan: PipelinePlan) -> None:
    print("dry_run=true")
    print(f"tasks_file={plan.tasks_file}")
    print(f"selected_tasks={','.join(task.task_id for task in plan.selected_tasks)}")
    for task in plan.selected_tasks:
        action = "run" if task.enabled else "skip_disabled"
        print(f"planned_task={task.task_id} action={action}")


def _print_pipeline_summary(result: PipelineRunResult) -> None:
    enabled_tasks_total = sum(1 for task in result.state.selected_tasks if task.enabled)
    tasks_approved = sum(1 for task in result.state.tasks if task.status == "approved")
    tasks_failed = sum(1 for task in result.state.tasks if task.status == "failed")
    print(f"pipeline_id={result.state.pipeline_id}")
    print(f"status={result.state.status}")
    print(f"tasks_total={enabled_tasks_total}")
    print(f"tasks_approved={tasks_approved}")
    print(f"tasks_failed={tasks_failed}")
    print(f"pipeline_report={result.pipeline_report.resolve()}")
    print(f"pipeline_state={result.pipeline_state_path.resolve()}")


def _print_task_summary_text(summary_list: TaskSummaryList) -> None:
    print(f"tasks_file={summary_list.tasks_file}")
    print(f"tasks_total={summary_list.tasks_total}")
    print(f"tasks_enabled={summary_list.tasks_enabled}")
    print(f"tasks_disabled={summary_list.tasks_disabled}")
    for task in summary_list.tasks:
        print(
            " ".join(
                [
                    f"task_id={task.task_id}",
                    f"enabled={str(task.enabled).lower()}",
                    f"backend={task.backend}",
                    f"title={json.dumps(task.title, ensure_ascii=False)}",
                    f"seed_workspace={task.seed_workspace or ''}",
                ]
            )
        )


def _print_task_summary_json(summary_list: TaskSummaryList) -> None:
    payload = {
        "tasks_file": str(summary_list.tasks_file),
        "tasks_total": summary_list.tasks_total,
        "tasks_enabled": summary_list.tasks_enabled,
        "tasks_disabled": summary_list.tasks_disabled,
        "tasks": [
            {
                "id": task.task_id,
                "title": task.title,
                "enabled": task.enabled,
                "backend": task.backend,
                "seed_workspace": task.seed_workspace,
                "criteria_count": task.criteria_count,
            }
            for task in summary_list.tasks
        ],
    }
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def list_tasks_main(argv: list[str] | None = None) -> int:
    parser = build_list_tasks_parser()
    args = parser.parse_args(argv)
    try:
        summary_list = list_task_summaries(
            args.tasks_file,
            enabled_only=args.enabled_only,
            disabled_only=args.disabled_only,
        )
    except Exception as exc:  # noqa: BLE001 - CLI should print deterministic error text.
        print("status=failed")
        print(f"error={exc}")
        return 1

    if args.format == "json":
        _print_task_summary_json(summary_list)
    else:
        _print_task_summary_text(summary_list)
    return 0


def run_pipeline_main(argv: list[str] | None = None) -> int:
    parser = build_run_pipeline_parser()
    args = parser.parse_args(argv)
    try:
        result = run_pipeline(
            tasks_file=args.tasks_file,
            runs_dir=args.runs_dir,
            from_task=args.from_task,
            only=args.only,
            dry_run=args.dry_run,
            continue_on_failure=args.continue_on_failure,
            backend=args.backend,
            codex_cmd=args.codex_cmd,
            max_retries=args.max_retries,
            verbose=args.verbose,
            stream_codex_output=args.stream_codex_output,
        )
    except Exception as exc:  # noqa: BLE001 - CLI should print deterministic error text.
        print("status=failed")
        print(f"error={exc}")
        return 1
    if isinstance(result, PipelinePlan):
        _print_pipeline_dry_run(result)
        return 0
    _print_pipeline_summary(result)
    return 0 if result.state.status == "approved" else 1


def accept_main(argv: list[str] | None = None) -> int:
    parser = build_accept_parser()
    args = parser.parse_args(argv)
    try:
        result = accept_run(
            run_id=args.run_id,
            runs_dir=Path(args.runs_dir),
            target_workspace_override=args.target_workspace,
            commit_message=args.commit_message,
            dry_run=args.dry_run,
            init_target_git=args.init_target_git,
            allow_unreviewed=args.allow_unreviewed,
        )
    except Exception as exc:  # noqa: BLE001 - CLI should print deterministic error text.
        print("accept_status=failed")
        print(f"error={exc}")
        return 1
    if args.dry_run:
        print("accept_status=dry_run_ok")
    elif result.no_target_changes:
        print("accept_status=accepted_noop")
    else:
        print("accept_status=accepted")
    print(f"run_id={result.run_id}")
    print(f"review_gate={result.review_gate}")
    print(f"target_workspace={result.target_workspace}")
    print(f"commit_hash={result.commit_hash or '(none)'}")
    print(f"acceptance={result.acceptance_path}")
    if result.applied_files:
        print("applied_files=" + ",".join(result.applied_files))
    if result.deleted_files:
        print("deleted_files=" + ",".join(result.deleted_files))
    if result.skipped_files:
        print("skipped_files=" + ",".join(result.skipped_files))
    return 0


def apply_main(argv: list[str] | None = None) -> int:
    parser = build_apply_parser()
    args = parser.parse_args(argv)
    try:
        result = apply_run(
            run_id=args.run_id,
            runs_dir=Path(args.runs_dir),
            target_workspace_override=args.target_workspace,
            dry_run=args.dry_run,
            allow_unreviewed=args.allow_unreviewed,
        )
    except Exception as exc:  # noqa: BLE001 - CLI should print deterministic error text.
        print("apply_status=failed")
        print(f"run_id={args.run_id}")
        print(f"error={exc}")
        return 1
    if args.dry_run:
        print("apply_status=dry_run_ok")
    else:
        print("apply_status=applied")
    print(f"run_id={result.run_id}")
    print(f"target_workspace={result.target_workspace}")
    if args.dry_run:
        print("would_apply_files=" + ",".join(result.applied_files))
        print("would_delete_files=" + ",".join(result.deleted_files))
        print("would_skip_files=" + ",".join(result.skipped_files))
    else:
        print(f"apply_report={result.apply_report_path}")
        if result.applied_files:
            print("applied_files=" + ",".join(result.applied_files))
        if result.deleted_files:
            print("deleted_files=" + ",".join(result.deleted_files))
        if result.skipped_files:
            print("skipped_files=" + ",".join(result.skipped_files))
    print(f"review_gate={result.review_gate}")
    print(f"target_status={result.target_status}")
    return 0


def show_run_main(argv: list[str] | None = None) -> int:
    parser = build_show_run_parser()
    args = parser.parse_args(argv)
    try:
        summary = build_run_status_summary(run_id=args.run_id, runs_dir=args.runs_dir)
    except Exception as exc:  # noqa: BLE001 - CLI should print deterministic error text.
        print(f"run_id={args.run_id}")
        print("status=failed")
        print(f"error={exc}")
        return 1

    if args.format == "json":
        print(format_run_status_json(summary))
    else:
        print(format_run_status_text(summary, show_paths=args.show_paths))
    return 0


def show_pipeline_main(argv: list[str] | None = None) -> int:
    parser = build_show_pipeline_parser()
    args = parser.parse_args(argv)
    try:
        summary = build_pipeline_status_summary(pipeline_id=args.pipeline_id, runs_dir=args.runs_dir)
    except Exception as exc:  # noqa: BLE001 - CLI should print deterministic error text.
        print(f"pipeline_id={args.pipeline_id}")
        print("status=failed")
        print(f"error={exc}")
        return 1

    if args.format == "json":
        print(format_pipeline_status_json(summary))
    else:
        print(format_pipeline_status_text(summary, show_paths=args.show_paths))
    return 0


def rework_run_main(argv: list[str] | None = None) -> int:
    parser = build_rework_run_parser()
    args = parser.parse_args(argv)
    try:
        result = execute_rework_run(
            source_run_id=args.source_run_id,
            runs_dir=args.runs_dir,
            feedback_path=args.feedback,
            backend_name=args.backend,
            codex_cmd=args.codex_cmd,
            max_retries=args.max_retries,
            verbose=args.verbose,
            stream_codex_output=args.stream_codex_output,
        )
    except Exception as exc:  # noqa: BLE001 - CLI should print deterministic error text.
        print(f"source_run_id={args.source_run_id}")
        print("status=failed")
        print(f"error={exc}")
        return 1
    print(f"source_run_id={result.source_run_id}")
    print(f"rework_run_id={result.rework_run_id}")
    print(f"status={result.status}")
    print(f"backend={result.backend_name}")
    print(f"final_report={result.final_report}")
    print(f"review_packet={result.review_packet}")
    print(f"state={result.state_path}")
    print(f"rework_feedback={result.rework_feedback}")
    return 0 if result.status == "approved" else 1


def review_run_main(argv: list[str] | None = None) -> int:
    parser = build_review_run_parser()
    args = parser.parse_args(argv)
    feedback_path = args.feedback
    if args.force_feedback and not args.from_findings:
        print(f"run_id={args.run_id}")
        print("status=failed")
        print("error=--force-feedback is allowed only with --from-findings")
        return 1
    if args.from_findings:
        if args.decision != "rejected":
            print(f"run_id={args.run_id}")
            print("status=failed")
            print("error=--from-findings is allowed only with --decision rejected")
            return 1
        if args.feedback is not None:
            print(f"run_id={args.run_id}")
            print("status=failed")
            print("error=--from-findings and --feedback are mutually exclusive")
            return 1
        try:
            _run_dir, state = load_review_target_run(Path(args.runs_dir), args.run_id)
            if state.human_review_decision and not args.force:
                raise ValueError("Human review decision already recorded. Pass --force to overwrite it.")
            feedback_result = create_findings_feedback_for_run(
                run_id=args.run_id,
                runs_dir=args.runs_dir,
                force=args.force_feedback,
                include_non_blocking=False,
                reuse_existing=not args.force_feedback,
            )
        except Exception as exc:  # noqa: BLE001 - CLI should print deterministic error text.
            print(f"run_id={args.run_id}")
            print("status=failed")
            print(f"error={exc}")
            return 1
        feedback_path = feedback_result.feedback_path
    try:
        result = record_review_decision(
            run_id=args.run_id,
            runs_dir=args.runs_dir,
            decision=args.decision,
            feedback_path=feedback_path,
            force=args.force,
        )
    except Exception as exc:  # noqa: BLE001 - CLI should print deterministic error text.
        print(f"run_id={args.run_id}")
        print("status=failed")
        print(f"error={exc}")
        return 1
    print(f"run_id={result.run_id}")
    print("status=review_recorded")
    print(f"decision={result.decision}")
    print(f"review_decision={result.review_decision_path}")
    print(f"review_feedback={result.review_feedback_path or ''}")
    print(f"state={result.state_path}")
    return 0


def record_findings_main(argv: list[str] | None = None) -> int:
    parser = build_record_findings_parser()
    args = parser.parse_args(argv)
    try:
        result = record_review_findings(
            run_id=args.run_id,
            runs_dir=args.runs_dir,
            findings_file=args.findings_file,
            profile_id=args.profile,
            force=args.force,
        )
    except Exception as exc:  # noqa: BLE001 - CLI should print deterministic error text.
        print(f"run_id={args.run_id}")
        print("status=failed")
        print(f"error={exc}")
        return 1
    print(f"run_id={result.run_id}")
    print("status=findings_recorded")
    print(f"overall_decision={result.overall_decision}")
    print(f"blocking_findings={result.blocking_findings}")
    print(f"review_findings_source_profile={result.source_profile or ''}")
    print(f"review_findings_source_kind={result.source_kind or ''}")
    print(f"review_findings={result.review_findings_path}")
    print(f"review_findings_markdown={result.review_findings_markdown_path}")
    print(f"state={result.state_path}")
    return 0


def findings_feedback_main(argv: list[str] | None = None) -> int:
    parser = build_findings_feedback_parser()
    args = parser.parse_args(argv)
    try:
        result = create_findings_feedback_for_run(
            run_id=args.run_id,
            runs_dir=args.runs_dir,
            output_path=args.output,
            force=args.force,
            include_non_blocking=args.include_non_blocking,
        )
    except Exception as exc:  # noqa: BLE001 - CLI should print deterministic error text.
        if args.format == "json":
            print(
                json.dumps(
                    {
                        "run_id": args.run_id,
                        "status": "failed",
                        "error": str(exc),
                    },
                    indent=2,
                    ensure_ascii=False,
                )
            )
        else:
            print(f"run_id={args.run_id}")
            print("status=failed")
            print(f"error={exc}")
        return 1

    payload = {
        "run_id": result.run_id,
        "status": "findings_feedback_created",
        "feedback": str(result.feedback_path),
        "source_findings": str(result.source_findings_path),
        "findings_included": result.findings_included,
        "blocking_findings_included": result.blocking_findings_included,
        "state": str(result.state_path),
        "next_action": "review_rejected_or_rework",
    }
    if args.format == "json":
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"run_id={payload['run_id']}")
        print(f"status={payload['status']}")
        print(f"feedback={payload['feedback']}")
        print(f"source_findings={payload['source_findings']}")
        print(f"findings_included={payload['findings_included']}")
        print(f"blocking_findings_included={payload['blocking_findings_included']}")
        print(f"state={payload['state']}")
        print(f"next_action={payload['next_action']}")
    return 0


def run_review_checks_main(argv: list[str] | None = None) -> int:
    parser = build_run_review_checks_parser()
    args = parser.parse_args(argv)
    try:
        result = run_deterministic_review_checks(
            run_id=args.run_id,
            runs_dir=args.runs_dir,
            profiles=args.profile,
            force=args.force,
        )
    except Exception as exc:  # noqa: BLE001 - CLI should print deterministic error text.
        print(f"run_id={args.run_id}")
        print("status=failed")
        print(f"error={exc}")
        return 1
    print(f"run_id={result.run_id}")
    print("status=review_checks_completed")
    print(f"overall_decision={result.report.overall_decision}")
    print(f"findings_total={result.report.counts.total}")
    print(f"blocking_findings={result.report.counts.blocking_open}")
    print(f"review_findings={result.persisted.review_findings_path}")
    print(f"review_findings_markdown={result.persisted.review_findings_markdown_path}")
    print(f"state={result.persisted.state_path}")
    return 0


def classify_run_main(argv: list[str] | None = None) -> int:
    parser = build_classify_run_parser()
    args = parser.parse_args(argv)
    try:
        result = classify_run_risk(
            run_id=args.run_id,
            runs_dir=args.runs_dir,
            force=args.force,
        )
    except Exception as exc:  # noqa: BLE001 - CLI should print deterministic error text.
        if args.format == "json":
            print(
                json.dumps(
                    {
                        "run_id": args.run_id,
                        "status": "failed",
                        "error": str(exc),
                    },
                    indent=2,
                    ensure_ascii=False,
                )
            )
        else:
            print(f"run_id={args.run_id}")
            print("status=failed")
            print(f"error={exc}")
        return 1

    payload = {
        "run_id": result.run_id,
        "status": "risk_classified",
        "risk_level": result.classification.risk_level,
        "change_type": result.classification.change_type,
        "required_review_profiles": result.classification.required_review_profiles,
        "optional_review_profiles": result.classification.optional_review_profiles,
        "risk_classification": str(result.risk_classification_path),
        "risk_classification_markdown": str(result.risk_classification_markdown_path),
        "state": str(result.state_path),
        "next_action": "prepare_required_reviews",
    }
    if args.format == "json":
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"run_id={payload['run_id']}")
        print(f"status={payload['status']}")
        print(f"risk_level={payload['risk_level']}")
        print(f"change_type={payload['change_type']}")
        print("required_review_profiles=" + ",".join(payload["required_review_profiles"]))
        print("optional_review_profiles=" + ",".join(payload["optional_review_profiles"]))
        print(f"risk_classification={payload['risk_classification']}")
        print(f"risk_classification_markdown={payload['risk_classification_markdown']}")
        print(f"state={payload['state']}")
        print(f"next_action={payload['next_action']}")
    return 0


def doctor_main(argv: list[str] | None = None) -> int:
    parser = build_doctor_parser()
    args = parser.parse_args(argv)
    try:
        result = run_doctor(
            tasks_file=args.tasks_file,
            task_id=args.task_id,
            codex_cmd=args.codex_cmd,
            skip_tests=args.skip_tests,
            strict=args.strict,
        )
    except Exception as exc:  # noqa: BLE001 - CLI should print deterministic error text.
        if args.format == "json":
            print(
                json.dumps(
                    {
                        "doctor_status": "failed",
                        "next_action": "fix_errors",
                        "checks": [],
                        "error": str(exc),
                    },
                    indent=2,
                    ensure_ascii=False,
                )
            )
        else:
            print("doctor_status=failed")
            print(f"error={exc}")
        return 1

    if args.format == "json":
        print(format_doctor_json(result))
    else:
        print(format_doctor_text(result))
    return result.exit_code


def prepare_review_main(argv: list[str] | None = None) -> int:
    parser = build_prepare_review_parser()
    args = parser.parse_args(argv)
    try:
        result = prepare_review_prompts(
            run_id=args.run_id,
            runs_dir=args.runs_dir,
            profile_ids=args.profile,
            all_profiles=args.all_profiles,
            required_profiles=args.required_profiles,
            output_dir=args.output_dir,
            force=args.force,
        )
    except Exception as exc:  # noqa: BLE001 - CLI should print deterministic error text.
        if args.format == "json":
            print(
                json.dumps(
                    {
                        "run_id": args.run_id,
                        "status": "failed",
                        "error": str(exc),
                    },
                    indent=2,
                    ensure_ascii=False,
                )
            )
        else:
            print(f"run_id={args.run_id}")
            print("status=failed")
            print(f"error={exc}")
        return 1

    payload = {
        "run_id": result.run_id,
        "status": "review_prompts_prepared",
        "profiles": list(result.profiles),
        "prompts_dir": str(result.prompts_dir),
        "prompts": [
            {"profile": prepared.profile, "path": str(prepared.path)}
            for prepared in result.prompts
        ],
        "manifest": str(result.manifest_path) if result.manifest_path is not None else "",
        "message": result.message or "",
        "next_action": "run_external_reviewer_or_record_findings",
    }
    if args.format == "json":
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"run_id={payload['run_id']}")
        print(f"status={payload['status']}")
        print("profiles=" + ",".join(payload["profiles"]))
        print(f"prompts_dir={payload['prompts_dir']}")
        for prompt in payload["prompts"]:
            print(f"prompt={prompt['profile']}:{prompt['path']}")
        print(f"manifest={payload['manifest']}")
        if payload["message"]:
            print(f"message={payload['message']}")
        print(f"next_action={payload['next_action']}")
    return 0


def list_review_profiles_main(argv: list[str] | None = None) -> int:
    parser = build_list_review_profiles_parser()
    args = parser.parse_args(argv)
    profiles = list_review_profiles()
    if args.format == "json":
        print(format_review_profiles_json(profiles))
    else:
        print(format_review_profiles_text(profiles))
    return 0


def show_review_profile_main(argv: list[str] | None = None) -> int:
    parser = build_show_review_profile_parser()
    args = parser.parse_args(argv)
    profile = get_review_profile(args.profile_id)
    if profile is None:
        error = f"review profile not found: {args.profile_id}"
        if args.format == "json":
            print(json.dumps({"status": "failed", "error": error}, indent=2, ensure_ascii=False))
        else:
            print("status=failed")
            print(f"error={error}")
        return 1
    if args.format == "json":
        print(format_review_profile_json(profile))
    else:
        print(format_review_profile_text(profile))
    return 0


def draft_task_scaffold_main(argv: list[str] | None = None) -> int:
    parser = build_draft_task_scaffold_parser()
    args = parser.parse_args(argv)
    try:
        result = create_task_draft_scaffold(
            request_path=args.request,
            output_dir=args.output_dir,
            task_id=args.task_id,
            title=args.title,
            risk_level=args.risk_level,
            prompt_language=args.prompt_language,
        )
    except Exception as exc:  # noqa: BLE001 - CLI should print deterministic error text.
        if args.format == "json":
            print(
                json.dumps(
                    {
                        "status": "failed",
                        "error": str(exc),
                    },
                    indent=2,
                    ensure_ascii=False,
                )
            )
        else:
            print("status=failed")
            print(f"error={exc}")
        return 1

    payload = {
        "draft_id": result.draft.draft_id,
        "status": "draft_created",
        "draft_dir": str(result.draft_dir),
        "task_draft": str(result.task_draft_path),
        "codex_prompt": str(result.codex_prompt_path),
        "task_review": str(result.task_review_path),
        "manifest": str(result.manifest_path),
        "next_action": "validate_task_draft",
    }
    if args.format == "json":
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"draft_id={payload['draft_id']}")
        print(f"status={payload['status']}")
        print(f"draft_dir={payload['draft_dir']}")
        print(f"task_draft={payload['task_draft']}")
        print(f"codex_prompt={payload['codex_prompt']}")
        print(f"task_review={payload['task_review']}")
        print(f"manifest={payload['manifest']}")
        print(f"next_action={payload['next_action']}")
    return 0


def validate_task_draft_main(argv: list[str] | None = None) -> int:
    parser = build_validate_task_draft_parser()
    args = parser.parse_args(argv)
    try:
        result = validate_task_draft(
            draft_id=args.draft_id,
            drafts_dir=args.drafts_dir,
            force=args.force,
        )
    except Exception as exc:  # noqa: BLE001 - CLI should print deterministic error text.
        if args.format == "json":
            print(
                json.dumps(
                    {
                        "draft_id": args.draft_id,
                        "status": "failed",
                        "error": str(exc),
                    },
                    indent=2,
                    ensure_ascii=False,
                )
            )
        else:
            print(f"draft_id={args.draft_id}")
            print("status=failed")
            print(f"error={exc}")
        return 1

    payload = {
        "draft_id": result.draft_id,
        "status": "validated",
        "validation_status": result.report.validation_status,
        "valid_for_promotion": result.report.valid_for_promotion,
        "blocking_findings": result.report.counts.blocking,
        "warnings": result.report.counts.warnings,
        "validator_report": str(result.report_path),
        "validator_report_markdown": str(result.report_markdown_path),
        "manifest": str(result.manifest_path),
        "next_action": "promote_task_draft" if result.report.valid_for_promotion else "revise_task_draft",
    }
    if args.format == "json":
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(f"draft_id={payload['draft_id']}")
        print(f"status={payload['status']}")
        print(f"validation_status={payload['validation_status']}")
        print(f"valid_for_promotion={str(payload['valid_for_promotion']).lower()}")
        print(f"blocking_findings={payload['blocking_findings']}")
        print(f"warnings={payload['warnings']}")
        print(f"validator_report={payload['validator_report']}")
        print(f"validator_report_markdown={payload['validator_report_markdown']}")
        print(f"manifest={payload['manifest']}")
        print(f"next_action={payload['next_action']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "validate-task-draft":
        return validate_task_draft_main(args[1:])
    if args and args[0] == "draft-task-scaffold":
        return draft_task_scaffold_main(args[1:])
    if args and args[0] == "prepare-review":
        return prepare_review_main(args[1:])
    if args and args[0] == "show-review-profile":
        return show_review_profile_main(args[1:])
    if args and args[0] == "list-review-profiles":
        return list_review_profiles_main(args[1:])
    if args and args[0] == "findings-feedback":
        return findings_feedback_main(args[1:])
    if args and args[0] == "run-review-checks":
        return run_review_checks_main(args[1:])
    if args and args[0] == "classify-run":
        return classify_run_main(args[1:])
    if args and args[0] == "doctor":
        return doctor_main(args[1:])
    if args and args[0] == "record-findings":
        return record_findings_main(args[1:])
    if args and args[0] == "show-pipeline":
        return show_pipeline_main(args[1:])
    if args and args[0] == "show-run":
        return show_run_main(args[1:])
    if args and args[0] == "apply-run":
        return apply_main(args[1:])
    if args and args[0] == "accept-run":
        return accept_main(args[1:])
    if args and args[0] == "review-run":
        return review_run_main(args[1:])
    if args and args[0] == "rework-run":
        return rework_run_main(args[1:])
    if args and args[0] == "list-tasks":
        return list_tasks_main(args[1:])
    if args and args[0] == "run-task":
        return run_task_main(args[1:])
    if args and args[0] == "run-pipeline":
        return run_pipeline_main(args[1:])
    return run_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
