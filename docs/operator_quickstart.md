# Operator Quickstart

Use this flow for day-to-day local operator work when you want `ai_orchestrator` to prepare changes, but you want the final diff review, test run, and commit to stay manual.

## Operator Flow

1. Start from a clean git working tree in the target repository.
2. Run `doctor` before starting a real pipeline.
3. Inspect available tasks with `list-tasks`.
4. Run `run-pipeline` for one task or multiple tasks.
5. Inspect the overall pipeline with `show-pipeline`.
6. Inspect a specific run with `show-run`.
7. Open `.runs/<run_id>/REVIEW_PACKET.md` and review it manually.
8. Record the human decision with `review-run`.
9. If the run is rejected, use `rework-run` and review the new run again.
10. If the run is approved, use `apply-run`.
11. Inspect the resulting `git diff` manually.
12. Run tests manually.
13. Stage and commit manually.

## Important Boundaries

- `run-pipeline` creates run and pipeline artifacts only. It does not apply changes and does not create a commit.
- `review-run` records the human decision only. It does not apply changes and does not create a commit.
- `apply-run` copies approved changes back into the target workspace, but it does not run `git add` and does not create a commit.
- `accept-run` applies changes and creates a git commit. For a manual workflow, prefer `apply-run`.

## Happy Path

Inspect the task set:

```bash
python -m ai_orchestrator.cli doctor \
  --tasks-file tasks.yaml \
  --task-id <task_id> \
  --codex-cmd "$CODEX_CMD"

python -m ai_orchestrator.cli list-tasks --tasks-file tasks.yaml
```

Run one task:

```bash
python -m ai_orchestrator.cli run-pipeline \
  --tasks-file tasks.yaml \
  --only <task_id> \
  --codex-cmd "$CODEX_CMD" \
  --verbose \
  --stream-codex-output
```

Run multiple tasks:

```bash
python -m ai_orchestrator.cli run-pipeline \
  --tasks-file tasks.yaml \
  --only <task_a> \
  --only <task_b> \
  --codex-cmd "$CODEX_CMD" \
  --verbose \
  --stream-codex-output
```

Inspect, approve, apply, and finish manually:

```bash
python -m ai_orchestrator.cli show-pipeline <pipeline_id> --runs-dir .runs
python -m ai_orchestrator.cli show-run <run_id> --runs-dir .runs --show-paths
# Manually read .runs/<run_id>/REVIEW_PACKET.md
python -m ai_orchestrator.cli review-run <run_id> --runs-dir .runs --decision approved
python -m ai_orchestrator.cli apply-run <run_id> --runs-dir .runs
git diff --stat
git diff
python -m unittest discover -s tests
git add <files>
git commit -m "describe the approved change"
```

## Rejected Review

```bash
python -m ai_orchestrator.cli show-run <run_id> --runs-dir .runs --show-paths
# Manually read .runs/<run_id>/REVIEW_PACKET.md
python -m ai_orchestrator.cli review-run <run_id> \
  --runs-dir .runs \
  --decision rejected \
  --feedback review_feedback.md

python -m ai_orchestrator.cli rework-run <run_id> \
  --runs-dir .runs \
  --codex-cmd "$CODEX_CMD" \
  --verbose \
  --stream-codex-output

python -m ai_orchestrator.cli show-run <new_run_id> --runs-dir .runs --show-paths
```

`rework-run` creates a new run. It does not modify the old run, does not apply changes, and does not create a commit.

## Preflight Failed

### Dirty git tree

`doctor` fails when tracked files are already modified. Clean the repo first, then rerun `doctor`.

### Failing unit tests

`doctor` runs `python -m unittest discover -s tests` by default. Fix the failing tests before running a real pipeline.

### Missing task id

If `doctor --task-id ...` fails, confirm the task exists in `tasks.yaml` and is enabled. `list-tasks --tasks-file tasks.yaml` is the quickest check.

### Missing or invalid codex command

If the Codex command is missing or invalid, `doctor` fails the `codex_cmd` check. Pass a working command with `--codex-cmd`, or fix the configured command before running the pipeline.

### Nested Codex warning

`doctor` always warns that nested Codex sessions are unsafe. Run `codex_cli` pipelines from a normal terminal, not from inside an active Codex agent session.
