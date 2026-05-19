from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ai_orchestrator.backends import Backend
from ai_orchestrator.pipeline import PipelinePlan, PipelineRunResult, run_pipeline
from ai_orchestrator.review import accept_run
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


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "accept-run":
        return accept_main(args[1:])
    if args and args[0] == "list-tasks":
        return list_tasks_main(args[1:])
    if args and args[0] == "run-task":
        return run_task_main(args[1:])
    if args and args[0] == "run-pipeline":
        return run_pipeline_main(args[1:])
    return run_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
