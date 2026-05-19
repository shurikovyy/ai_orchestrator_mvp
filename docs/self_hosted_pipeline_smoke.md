# First Self-Hosted Pipeline Smoke

## Purpose

This smoke test verifies the first end-to-end self-hosted pipeline path for `ai_orchestrator_mvp`:

- a local `codex_cli` task is selected from `tasks.yaml`
- `run-pipeline` executes it in an isolated workspace copied from this repository
- the run produces task-level and pipeline-level review artifacts
- no changes are applied back to the repo automatically

Use it to confirm the pipeline wiring, review flow, and artifact layout before relying on larger self-hosted tasks.

## Preconditions

- Run from the repository root.
- Install project dependencies so `python -m ai_orchestrator.cli ...` works. If needed:

```bash
python -m pip install -e .
```

- Have a working Codex CLI command available, either on `PATH` or via an explicit `codex_cmd` / `--codex-cmd` value.
- Start from a local `tasks.yaml`; do not edit `tasks.yaml.example` directly.
- For a true self-hosted run, point `seed_workspace` at this repository. If `tasks.yaml` is in the repo root, `seed_workspace: "."` resolves to the current repo.
- If you may later run `accept-run`, keep the target repo clean first. `accept-run` refuses dirty target repositories.

## Create Or Update `tasks.yaml`

Copy the committed example once and keep your edits local:

```bash
cp tasks.yaml.example tasks.yaml
```

PowerShell equivalent:

```powershell
Copy-Item tasks.yaml.example tasks.yaml
```

`tasks.yaml.example` is a safe template and does not already define a self-hosted smoke task for this repo. Update your local `tasks.yaml` so it contains one enabled `codex_cli` task that targets `seed_workspace: "."`.

Example:

```yaml
project: ai_orchestrator_mvp

defaults:
  backend: codex_cli
  max_retries: 2
  require_structured_report: true
  rerun_report_test_commands: true
  validate_workspace_manifest: true
  validation_command_timeout: 60
  stream_codex_output: true
  verbose: true

tasks:
  - id: "self-hosted-smoke"
    title: "First self-hosted pipeline smoke"
    enabled: true
    seed_workspace: "."
    prompt: |
      You are working in an isolated copy of the ai_orchestrator_mvp repository.

      Add or update a small documentation-only artifact.
      Do not modify source code unless absolutely necessary.
      Do not modify tests unless absolutely necessary.
      Run:
        python -m unittest discover -s tests
      Create EXECUTION_REPORT.json using the required structured schema.
      Do not create EXECUTION_REPORT.md.
      Do not run git commands unless the workspace is a git repository.
    criteria:
      - "report.status=completed"
      - "commands_run includes python -m unittest discover -s tests"
      - "tests.status=passed"
```

Notes:

- Keep the task id quoted, for example `id: "self-hosted-smoke"`.
- Relative `seed_workspace` paths are resolved relative to the `tasks.yaml` file location.
- For a first smoke, keep the change surface small and prefer documentation-only work.

## Run `run-pipeline` For One Task

Use `--only` to force pipeline mode to run exactly one task:

```bash
python -m ai_orchestrator.cli run-pipeline \
  --tasks-file tasks.yaml \
  --only self-hosted-smoke \
  --verbose \
  --stream-codex-output
```

If Codex CLI is not on `PATH`, pass it explicitly:

```bash
python -m ai_orchestrator.cli run-pipeline \
  --tasks-file tasks.yaml \
  --only self-hosted-smoke \
  --codex-cmd ./node_modules/.bin/codex.cmd \
  --verbose \
  --stream-codex-output
```

Expected behavior:

- `run-pipeline` creates a `pipeline_id`
- it prints the pipeline report and pipeline state paths
- it prints the task `run_id`
- it does not call `accept-run`
- it does not apply files back to the target repo
- it does not create a commit automatically

## Review `REVIEW_PACKET.md`

After the run, open the task-level review packet:

```bash
cat ".runs/<run_id>/REVIEW_PACKET.md"
```

Review it in this order:

1. Confirm `Run status` and the structured report status are both what you expect.
2. Check the recorded test command and test result summary.
3. Check `Changed files and apply plan` to make sure only intended files would be applied.
4. Read the diff preview.
5. Verify the suggested `accept-run` command matches the run you intend to approve.

For a smoke pass, the review packet should give you enough information to decide whether the run is safe to accept or whether it should be discarded and rerun.

## Manual Gates

`run-pipeline` stops at artifact generation. Two gates stay manual:

- `accept-run` is manual. Nothing is applied back to the target repo unless you invoke it yourself after review.
- commit is manual. `run-pipeline` never creates a commit by itself. If you choose to use `accept-run`, that is an explicit manual accept/commit step. You can also skip it and apply or commit changes with your normal workflow instead.

## Artifacts To Inspect

Inspect both pipeline-level and task-level artifacts after the run.

Pipeline-level:

- `.runs/pipelines/<pipeline_id>/PIPELINE_REPORT.md`
- `.runs/pipelines/<pipeline_id>/pipeline_state.json`

Task-level:

- `.runs/<run_id>/final_report.md`
- `.runs/<run_id>/REVIEW_PACKET.md`
- `.runs/<run_id>/artifacts/workspace/EXECUTION_REPORT.json`
- `.runs/<run_id>/artifacts/workspace/<changed file paths>`
- `.runs/<run_id>/artifacts/step_*_attempt_*_codex_log.md` when Codex logging is enabled

What to look for:

- `PIPELINE_REPORT.md` should confirm which task ran and remind you that no automatic accept or commit happened.
- `pipeline_state.json` should record the selected task, final pipeline status, and per-task artifact paths.
- `final_report.md` should summarize the final validation outcome.
- `REVIEW_PACKET.md` should summarize report status, tests, changed files, diff preview, and the manual accept command.
- `artifacts/workspace/EXECUTION_REPORT.json` should contain the structured execution report produced inside the isolated workspace.
- `artifacts/workspace/...` should contain only the files you expect the task to have changed.
