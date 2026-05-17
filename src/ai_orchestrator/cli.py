from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ai_orchestrator.backends import get_backend
from ai_orchestrator.engine import TaskExecutionEngine
from ai_orchestrator.review import accept_run
from ai_orchestrator.schemas import TaskSpec


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


def run_main(argv: list[str] | None = None) -> int:
    parser = build_run_parser()
    args = parser.parse_args(argv)
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
    if args.backend in {"codex", "codex_cli"} and (args.codex_cmd or args.stream_codex_output):
        from ai_orchestrator.backends.codex_cli import CodexCliBackend

        backend = CodexCliBackend(codex_cmd=args.codex_cmd, stream_output=args.stream_codex_output)
    else:
        backend = get_backend(args.backend)
    engine = TaskExecutionEngine(backend=backend, runs_dir=Path(args.runs_dir), verbose=args.verbose)
    state = engine.run(task)
    run_dir = Path(args.runs_dir) / state.run_id
    final_report = run_dir / "final_report.md"
    review_packet = run_dir / "REVIEW_PACKET.md"
    print(f"run_id={state.run_id}")
    print(f"status={state.final_status}")
    print(f"backend={backend.name}")
    print(f"final_report={final_report}")
    print(f"review_packet={review_packet}")
    print(f"state={run_dir / 'state.json'}")
    return 0 if state.final_status == "approved" else 1


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
        print(f"accept_status=failed")
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
    return run_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
