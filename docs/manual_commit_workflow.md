# Manual Commit Workflow

This guide covers the recommended workflow when you want `ai_orchestrator` to prepare a change, but you want the final git review, test run, staging, and commit to remain a manual human step.

## Preconditions

- Start from a clean git working tree in the target repository.
- Keep task definitions in your local `tasks.yaml`.
- Run all commands from the repository root unless you intentionally use different paths.

## Recommended Workflow

1. Define one or more tasks in your local `tasks.yaml`.
2. Run `run-pipeline` for the task set you want to execute.
3. Inspect overall pipeline state with `show-pipeline`.
4. Inspect each specific run with `show-run`.
5. Read `.runs/<run_id>/REVIEW_PACKET.md` manually.
6. Record the human review decision with `review-run`.
7. If the review is rejected, use `rework-run` and review the new run again.
8. If the review is approved, use `apply-run`.
9. Inspect the resulting `git diff` manually.
10. Run tests manually.
11. Stage and commit manually.

## What Each Approval Means

- Validator approval: the deterministic validator accepted the run technically. This means the generated artifacts and required checks passed, but it is not a human acceptance decision.
- Human review approval: a reviewer examined the run artifacts, especially `REVIEW_PACKET.md`, and explicitly approved the run with `review-run --decision approved`.
- `apply-run`: copies approved changes from the isolated run workspace back into the target repository. It does not run `git add` and does not create a commit.
- `accept-run`: applies the changes and creates a git commit. It is a separate command from `apply-run`.

For the manual workflow, prefer `apply-run` because it preserves an explicit human gate for `git diff`, local test execution, staging, and the final commit message.

## Manual Workflow vs `accept-run`

- Use `apply-run` when you want the orchestrator to copy the approved files back, but you want to inspect the diff, run tests, and commit yourself.
- Use `accept-run` only when you want the orchestrator to handle the apply plus commit flow for you.
- `apply-run` does not `git add` and does not commit.
- `accept-run` applies and commits, so it is not the preferred command for the manual commit workflow.

## Concise Command Sequence

Run one task:

```bash
python -m ai_orchestrator.cli run-pipeline \
  --tasks-file tasks.yaml \
  --only <task_id> \
  --verbose \
  --stream-codex-output
```

Run multiple tasks:

```bash
python -m ai_orchestrator.cli run-pipeline \
  --tasks-file tasks.yaml \
  --verbose \
  --stream-codex-output
```

Inspect and review:

```bash
python -m ai_orchestrator.cli show-pipeline <pipeline_id> --runs-dir .runs
python -m ai_orchestrator.cli show-run <run_id> --runs-dir .runs --show-paths
python -m ai_orchestrator.cli review-run <run_id> --runs-dir .runs --decision approved
python -m ai_orchestrator.cli apply-run <run_id> --runs-dir .runs
git diff --stat
git diff
python -m unittest discover -s tests
git add <files>
git commit -m "describe the approved change"
```

Rejected review path:

```bash
python -m ai_orchestrator.cli review-run <run_id> \
  --runs-dir .runs \
  --decision rejected \
  --feedback review_feedback.md

python -m ai_orchestrator.cli rework-run <run_id> \
  --runs-dir .runs \
  --backend codex_cli \
  --codex-cmd "<your codex command>" \
  --verbose \
  --stream-codex-output
```

## Practical Notes

- `show-pipeline` is read-only and helps you decide which run needs review, rework, apply, or manual commit next.
- `show-run` is read-only and helps you inspect run artifacts before making a human decision.
- Review `REVIEW_PACKET.md` before `review-run`; validator approval alone is not enough for manual acceptance.
- `apply-run` requires validator approval and, by default, human review approval.
- After `apply-run`, the target repository is intentionally left for manual inspection and manual commit.
