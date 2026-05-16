from __future__ import annotations

import argparse
from pathlib import Path

from ai_orchestrator.backends import get_backend
from ai_orchestrator.engine import TaskExecutionEngine
from ai_orchestrator.schemas import TaskSpec


def build_parser() -> argparse.ArgumentParser:
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
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
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
    if args.backend in {"codex", "codex_cli"} and args.codex_cmd:
        from ai_orchestrator.backends.codex_cli import CodexCliBackend

        backend = CodexCliBackend(codex_cmd=args.codex_cmd)
    else:
        backend = get_backend(args.backend)
    engine = TaskExecutionEngine(backend=backend, runs_dir=Path(args.runs_dir))
    state = engine.run(task)
    run_dir = Path(args.runs_dir) / state.run_id
    final_report = run_dir / "final_report.md"
    print(f"run_id={state.run_id}")
    print(f"status={state.final_status}")
    print(f"backend={backend.name}")
    print(f"final_report={final_report}")
    print(f"state={run_dir / 'state.json'}")
    return 0 if state.final_status == "approved" else 1


if __name__ == "__main__":
    raise SystemExit(main())
