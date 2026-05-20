# AI Orchestrator MVP 0.1.9.1

Минимальный workflow-first инструмент для управляемого цикла:

```text
user task -> planner -> executor -> validator -> retry/rework -> final_report
```

Ключевая идея: **Codex/LLM выполняет работу, но не принимает решение о приемке**. Приемка остается в детерминированном Python-коде.

## Что умеет 0.1.9.1

- хранит состояние запуска в `.runs/<run_id>/state.json`;
- сохраняет логи и артефакты в `.runs/<run_id>/artifacts/`;
- поддерживает offline `mock` backend;
- поддерживает `codex_cli` backend через локальный Codex CLI;
- передает prompt в `codex exec` через `stdin`, чтобы не ломаться на Windows `.cmd` и multiline prompt;
- требует structured report через `EXECUTION_REPORT.json` при флаге `--require-structured-report`;
- валидирует `EXECUTION_REPORT.json` через Pydantic-схему;
- опционально повторно запускает test-команды из `EXECUTION_REPORT.json` через `--rerun-report-test-commands`;
- опционально сверяет `changed_files` с фактическими файлами workspace через `--validate-workspace-manifest`;
- поддерживает `--seed-workspace <path>`: копирует существующий toy/project workspace в isolated run workspace;
- при seed workspace сохраняет baseline manifest до Codex и проверяет `changed_files` как diff относительно baseline;
- создает `REVIEW_PACKET.md` после каждого run и пишет в него финальный, а не промежуточный статус;
- поддерживает controlled accept/commit через `ai-orchestrator accept-run <run_id>`;
- поддерживает console progress logs через `--verbose`;
- поддерживает live Codex CLI streaming через `--stream-codex-output`;
- для disposable/toy seed workspace может инициализировать git через `accept-run --init-target-git`.

## Что не коммитить и не архивировать

```text
.venv/
.runs/
.codex_home/
.codex_temp/
.tmp_tests/
node_modules/
codex_smoke_workspace/
```

`node_modules` тяжелый. `.runs` и `.codex_home` могут содержать локальные runtime-данные и логи.

## Установка с нуля: Windows + Git Bash + portable Node

### 1. Клонировать репозиторий

```bash
git clone <REPO_URL> ai_orchestrator_mvp
cd ai_orchestrator_mvp
```

Проверь, что ты в корне проекта:

```bash
ls pyproject.toml
ls src/ai_orchestrator/cli.py
```

### 2. Создать Python venv и установить пакет

```bash
python -m venv .venv
./.venv/Scripts/python.exe -m pip install --upgrade pip
./.venv/Scripts/python.exe -m pip install -e .
```

Проверка:

```bash
./.venv/Scripts/python.exe -m unittest discover -s tests
```

Ожидаемо:

```text
Ran 31 tests ... OK
```

### 3. Подключить portable Node только для текущей Git Bash-сессии

Предполагаемый путь:

```text
C:\Users\Slivin.Aleksandr\Tools\node
```

Команды:

```bash
NODE_HOME="/c/Users/Slivin.Aleksandr/Tools/node"
export PATH="$NODE_HOME:$PATH"
hash -r

node --version
npm --version
cmd //c "where node"
```

`where node` должен показать примерно:

```text
C:\Users\Slivin.Aleksandr\Tools\node\node.exe
```

Это не меняет системный `PATH`. После закрытия терминала команды надо выполнить заново.

### 4. Установить Codex CLI локально в проект

```bash
/c/Users/Slivin.Aleksandr/Tools/node/npm.cmd install
```

Проверить локальный Codex:

```bash
CODEX_CMD="$(pwd -W)/node_modules/.bin/codex.cmd"
"$CODEX_CMD" --version
```

### 5. Не использовать пустой локальный `CODEX_HOME`

```bash
unset CODEX_HOME
```

Если оставить пустой локальный `CODEX_HOME`, Codex может вернуть:

```text
401 Unauthorized: Missing bearer or basic authentication in header
```

Рабочий сценарий использует уже авторизованный профиль Codex:

```text
C:\Users\Slivin.Aleksandr\.codex
```

## Smoke-test: mock backend

```bash
./.venv/Scripts/python.exe -m ai_orchestrator.cli \
  "Create a short architecture note for an AI task orchestrator" \
  --criteria "has title" \
  --criteria "mentions validation loop" \
  --max-retries 2
```

Ожидаемо:

```text
status=approved
backend=mock
```

## Smoke-test: прямой Codex CLI

```bash
unset CODEX_HOME
NODE_HOME="/c/Users/Slivin.Aleksandr/Tools/node"
export PATH="$NODE_HOME:$PATH"
hash -r

CODEX_CMD="$(pwd -W)/node_modules/.bin/codex.cmd"
mkdir -p codex_smoke_workspace

"$CODEX_CMD" exec \
  --cd codex_smoke_workspace \
  --sandbox workspace-write \
  --output-last-message codex_final.md \
  --skip-git-repo-check \
  "Create a file RESULT.md with the title '# Codex CLI smoke test'. The file must contain the exact token ORCHESTRATOR_SMOKE_TEST_OK. In your final response, include the exact token ORCHESTRATOR_SMOKE_TEST_OK."
```

Проверка:

```bash
cat codex_smoke_workspace/RESULT.md
cat codex_final.md
```

Оба файла должны содержать:

```text
ORCHESTRATOR_SMOKE_TEST_OK
```

## Smoke-test: orchestrator + Codex CLI + structured validation

```bash
unset CODEX_HOME
NODE_HOME="/c/Users/Slivin.Aleksandr/Tools/node"
export PATH="$NODE_HOME:$PATH"
hash -r

CODEX_CMD="$(pwd -W)/node_modules/.bin/codex.cmd"

TASK=$(cat <<'TASK_EOF'
In the isolated Codex executor workspace, create a tiny Python toy project.

Requirements:
1. Create src/toy_calc.py.
2. Implement:
   - add(a, b)
   - subtract(a, b)
   - multiply(a, b)
   - divide(a, b), raising ZeroDivisionError for division by zero.
3. Create tests/test_toy_calc.py using Python unittest.
4. Tests must cover:
   - add
   - subtract
   - multiply
   - divide
   - divide by zero
5. Run:
   python -m unittest discover -s tests
6. Create EXECUTION_REPORT.json using the required structured schema.
7. Do not create EXECUTION_REPORT.md.
8. Do not run git commands unless the workspace is a git repository.

Do not ask follow-up questions.
Do not modify files outside the isolated workspace.
TASK_EOF
)

./.venv/Scripts/python.exe -m ai_orchestrator.cli \
  "$TASK" \
  --criteria "report.status=completed" \
  --criteria "changed_files includes src/toy_calc.py" \
  --criteria "changed_files includes tests/test_toy_calc.py" \
  --criteria "commands_run includes python -m unittest discover -s tests" \
  --criteria "tests.status=passed" \
  --backend codex_cli \
  --codex-cmd "$CODEX_CMD" \
  --require-structured-report \
  --rerun-report-test-commands \
  --validate-workspace-manifest \
  --validation-command-timeout 60 \
  --verbose \
  --stream-codex-output \
  --max-retries 2
```

Ожидаемо:

```text
status=approved
backend=codex_cli
```

В `final_report.md` должны быть строки:

```text
Structured execution report parsed successfully
Validator re-ran test command successfully
Workspace file manifest matches structured report changed_files
Structured report and explicit acceptance criteria passed
```

## Structured execution report contract

При `--require-structured-report` executor обязан создать `EXECUTION_REPORT.json` в корне isolated workspace.

Минимальная схема:

```json
{
  "schema_version": "1.0",
  "status": "completed",
  "summary": "short summary of what changed",
  "changed_files": ["relative/path.ext"],
  "commands_run": [
    {
      "command": "python -m unittest discover -s tests",
      "exit_code": 0,
      "status": "passed",
      "summary": "All tests passed."
    }
  ],
  "tests": [
    {
      "name": "unittest",
      "command": "python -m unittest discover -s tests",
      "status": "passed",
      "total": 5,
      "passed": 5,
      "failed": 0,
      "output": "Ran 5 tests ... OK"
    }
  ],
  "risks": [],
  "assumptions": [],
  "validation_notes": []
}
```

Validator проверяет:

- `EXECUTION_REPORT.json` существует;
- JSON валиден;
- JSON соответствует Pydantic-схеме;
- `report.status == completed`;
- все `tests[*].status == passed`, если тесты указаны;
- если задача выглядит как test-задача, `tests` не пустой;
- `changed_files` и `commands_run` не пустые;
- explicit criteria проходят;
- при `--rerun-report-test-commands` validator сам повторно запускает test-команды;
- при `--validate-workspace-manifest` validator сверяет `changed_files` с workspace manifest.

## Structured criteria DSL

Поддерживаются criteria:

```text
report.status=completed
changed_files includes src/toy_calc.py
changed_files:src/toy_calc.py
commands_run includes python -m unittest discover -s tests
commands_run:python -m unittest discover -s tests
tests.status=passed
tests passed
```

Неизвестные criteria проверяются fallback-способом: как substring в `ExecutionResult.content`.

## Повторный запуск тестов validator-ом

Флаг:

```bash
--rerun-report-test-commands
```

Validator запускает только команды из:

```text
tests[*].command
```

Allowlist:

```text
python -m unittest ...
python -m pytest ...
pytest ...
```

Команды с shell-control операторами блокируются:

```text
&&  ||  ;  |  >  <  `  $(...)
```

Python-команды запускаются через тот же interpreter, которым запущен orchestrator: `sys.executable`.

## Workspace manifest validation

Флаг:

```bash
--validate-workspace-manifest
```

### Без seed workspace

Validator требует, чтобы `EXECUTION_REPORT.json.changed_files` совпадал со всеми reportable-файлами, которые появились в пустом workspace.

Reportable extensions:

```text
.txt .md .py .json .yaml .yml .toml .sql .csv
```

Игнорируются:

```text
__pycache__/
*.pyc
*.pyo
.pytest_cache/
.mypy_cache/
.ruff_cache/
.git/
```

### С seed workspace

Флаг:

```bash
--seed-workspace <path>
```

Логика:

```text
1. orchestrator копирует seed project в .runs/<run_id>/artifacts/workspace
2. сохраняет baseline manifest до запуска Codex
3. Codex меняет isolated workspace
4. validator снимает after manifest
5. validator сравнивает after manifest с baseline manifest
6. changed_files должен совпадать с added/modified/deleted reportable files
```

Из seed workspace при копировании исключаются runtime-heavy директории:

```text
.git/
.venv/
venv/
node_modules/
.runs/
.tmp_tests/
.codex_home/
.codex_temp/
__pycache__/
.pytest_cache/
.mypy_cache/
.ruff_cache/
build/
dist/
*.egg-info/
```

Важно: при seed workspace `changed_files` должен содержать **только добавленные, измененные или удаленные файлы относительно baseline**, а не все файлы проекта.

## Seed workspace smoke-test

Этот тест проверяет, что Codex работает не в пустой папке, а с уже существующим маленьким проектом.

### 1. Создать seed project

```bash
rm -rf toy_seed_project
mkdir -p toy_seed_project/src toy_seed_project/tests

cat > toy_seed_project/src/toy_calc.py <<'PY'
def add(a, b):
    return a + b


def subtract(a, b):
    # BUG: this should subtract b from a
    return a + b
PY

cat > toy_seed_project/tests/test_toy_calc.py <<'PY'
import unittest

from src.toy_calc import add, subtract


class ToyCalcTests(unittest.TestCase):
    def test_add(self):
        self.assertEqual(add(2, 3), 5)

    def test_subtract(self):
        self.assertEqual(subtract(10, 4), 6)


if __name__ == "__main__":
    unittest.main()
PY
```

Сделай seed project git-репозиторием, чтобы потом проверить controlled accept/commit:

```bash
git -C toy_seed_project init
git -C toy_seed_project config user.email "local@example.com"
git -C toy_seed_project config user.name "Local User"
git -C toy_seed_project add .
git -C toy_seed_project commit -m "seed toy project with subtract bug"
```

Проверь, что seed действительно содержит баг:

```bash
python -m unittest discover -s toy_seed_project/tests -t toy_seed_project
```

Ожидаемо тест `test_subtract` должен упасть.

### 2. Запустить orchestrator с seed workspace

В Git Bash под Windows Python передавай seed path в Windows-формате через `pwd -W`:

```bash
unset CODEX_HOME
NODE_HOME="/c/Users/Slivin.Aleksandr/Tools/node"
export PATH="$NODE_HOME:$PATH"
hash -r

CODEX_CMD="$(pwd -W)/node_modules/.bin/codex.cmd"
SEED_WORKSPACE="$(pwd -W)/toy_seed_project"

TASK=$(cat <<'TASK_EOF'
You are working in an isolated copy of a seeded Python toy project.

Fix the bug in src/toy_calc.py so that subtract(a, b) returns a - b.
Do not rewrite the project from scratch.
Do not create unrelated files.
Run:
  python -m unittest discover -s tests -t .
Create EXECUTION_REPORT.json using the required structured schema.
Do not create EXECUTION_REPORT.md.
Do not run git commands unless the workspace is a git repository.

The structured report changed_files must include only files added, modified, or deleted relative to the seed baseline.
TASK_EOF
)

./.venv/Scripts/python.exe -m ai_orchestrator.cli \
  "$TASK" \
  --criteria "report.status=completed" \
  --criteria "changed_files includes src/toy_calc.py" \
  --criteria "commands_run includes python -m unittest discover -s tests -t ." \
  --criteria "tests.status=passed" \
  --backend codex_cli \
  --codex-cmd "$CODEX_CMD" \
  --seed-workspace "$SEED_WORKSPACE" \
  --require-structured-report \
  --rerun-report-test-commands \
  --validate-workspace-manifest \
  --validation-command-timeout 60 \
  --verbose \
  --stream-codex-output \
  --max-retries 2
```

Ожидаемо:

```text
status=approved
backend=codex_cli
```

Проверка:

```bash
RUN_ID="<run_id>"
RUN=".runs/$RUN_ID"
cat "$RUN/final_report.md"
cat "$RUN/REVIEW_PACKET.md"
cat "$RUN/artifacts/workspace/EXECUTION_REPORT.json"
cat "$RUN/artifacts/workspace/src/toy_calc.py"
cat "$RUN/artifacts/step_1_attempt_1_codex_log.md"
```

В `final_report.md` должна быть строка:

```text
Workspace file manifest matches structured report changed_files.
```

А `EXECUTION_REPORT.json.changed_files` должен содержать только реальные baseline-relative изменения, например:

```json
["src/toy_calc.py", "tests/__init__.py", "EXECUTION_REPORT.json"]
```

`tests/__init__.py` может появиться, если точная команда `python -m unittest discover -s tests -t .` требует импортируемый каталог `tests/`. Неизмененные seed-файлы перечисляться не должны.


## Task queue / run-task

`run-task` lets you keep repeatable task definitions in `tasks.yaml` instead of pasting a long multiline `TASK` string and repeating the same CLI flags on every run.

## Task queue quickstart

1. Copy the committed template into a local working file:

```bash
cp tasks.yaml.example tasks.yaml
```

2. `tasks.yaml.example` is safe by default: only `mock-smoke` is enabled, while `toy-fix` and `disabled-example` stay disabled.

3. A fresh `tasks.yaml.example` or copied `tasks.yaml` can be used with `run-pipeline --dry-run` without creating `toy_seed_project_0172` first.

```bash
python -m ai_orchestrator.cli run-pipeline --tasks-file tasks.yaml.example --dry-run
```

4. Edit your local `tasks.yaml` with the tasks you want to run.

5. Run the safe mock smoke test:

```bash
python -m ai_orchestrator.cli run-task mock-smoke --tasks-file tasks.yaml --verbose
```

6. Preview the pipeline plan without executing anything:

```bash
python -m ai_orchestrator.cli run-pipeline --tasks-file tasks.yaml --dry-run
```

7. Run the pipeline for all enabled tasks:

```bash
python -m ai_orchestrator.cli run-pipeline --tasks-file tasks.yaml --verbose
```

## Enable toy-fix after creating a seed workspace

Keep `toy-fix` as `enabled: false` until the local `seed_workspace` exists. If `toy_seed_project_0172` is missing, leave the task disabled.

Once you have created `toy_seed_project_0172`, set `enabled: true` in your local `tasks.yaml` or run that task separately after editing the file.

For a `codex_cli` task on Windows Python + Git Bash, one workable example is:

```bash
unset CODEX_HOME
NODE_HOME="/c/Users/Slivin.Aleksandr/Tools/node"
export PATH="$NODE_HOME:$PATH"
hash -r
CODEX_CMD="$(pwd -W)/node_modules/.bin/codex.cmd"

python -m ai_orchestrator.cli run-task toy-fix \
  --tasks-file tasks.yaml \
  --codex-cmd "$CODEX_CMD" \
  --verbose \
  --stream-codex-output
```

This is only an environment example. Keep `seed_workspace` and other task paths relative inside `tasks.yaml` whenever possible.

### Listing tasks

`list-tasks` is a read-only introspection command. It only reads `tasks.yaml` or `tasks.yaml.example` and prints task metadata.

Examples:

```bash
python -m ai_orchestrator.cli list-tasks --tasks-file tasks.yaml.example
python -m ai_orchestrator.cli list-tasks --tasks-file tasks.yaml --enabled-only
python -m ai_orchestrator.cli list-tasks --tasks-file tasks.yaml --disabled-only
python -m ai_orchestrator.cli list-tasks --tasks-file tasks.yaml --format json
```

Safety notes:

- `list-tasks` does not run tasks.
- `list-tasks` does not create `.runs`.
- `list-tasks` does not call Codex.
- `list-tasks` does not call `accept-run`.
- `list-tasks` does not create a commit.
- `list-tasks` is safe to use before creating a seed workspace such as `toy_seed_project_0172`.

### Structured task-defined plans

Default planner behavior is still the legacy one-step flow: one task description becomes one executor step with the task-level `criteria`.

Tasks may now define deterministic `plan_steps` directly in `tasks.yaml`:

```yaml
tasks:
  - id: "structured-plan-example"
    title: "Structured plan example"
    prompt: |
      Overall task description.
    plan_steps:
      - id: "inspect"
        title: "Inspect current state"
        description: |
          Inspect the repository and summarize the relevant files.
        criteria:
          - "inspection summary"
      - id: "implement"
        title: "Implement requested change"
        description: |
          Implement the requested change.
        criteria:
          - "implementation completed"
      - id: "verify"
        title: "Verify and report"
        description: |
          Run tests and create the required execution report.
        criteria:
          - "tests.status=passed"
```

Behavior:

- `plan_steps` are deterministic task definitions, not an LLM planner output.
- Each step has its own `criteria`; they map to internal `PlanStep.acceptance_criteria`.
- Task-level `criteria` remain useful for legacy one-step tasks and are not copied into every structured step automatically.
- The executor still receives only the current step description plus that step's criteria and prior validator feedback.
- The validator evaluates each structured step independently.
- Retries remain per step.
- If a step exhausts retries and still fails, later steps do not run.

### Explicit rework loop

`rework-run` creates a brand new run from an older run plus explicit human review feedback. It does not modify the old run, does not call `accept-run`, and does not create a commit.

Examples:

```bash
python -m ai_orchestrator.cli rework-run run_20260519_120000_abcd12 \
  --runs-dir .runs \
  --feedback review_feedback.md \
  --backend codex_cli \
  --codex-cmd "$CODEX_CMD" \
  --verbose \
  --stream-codex-output
```

```bash
python -m ai_orchestrator.cli rework-run run_20260519_120000_abcd12 \
  --runs-dir .runs \
  --feedback review_feedback.md \
  --backend mock \
  --verbose
```

Behavior:

- `rework-run` reads `.runs/<source_run_id>/state.json`.
- It requires a non-empty human feedback file.
- It creates a new run with the same task context plus `rework_of_run_id`, `rework_feedback`, and `rework_feedback_path`.
- The executor receives the human feedback as authoritative correction guidance.
- The new run gets its own `final_report.md`, `REVIEW_PACKET.md`, and `state.json`.
- A copy of the feedback is stored as `.runs/<new_run_id>/REWORK_FEEDBACK.md`.
- The old run is preserved unchanged for audit/review history.

Safety notes:

- `rework-run` does not call `accept-run`.
- `rework-run` does not create a commit.
- Commit or accept remains a manual review gate.
- Commit the feedback file only if that is intentional.
- Feedback may contain sensitive review notes.
- `.runs/<new_run_id>/REWORK_FEEDBACK.md` is stored under ignored runtime artifacts.

### Human review decisions

Validator approval is a technical approval gate, not a human acceptance decision. `review-run` records the explicit human reviewer decision after `final_report.md` and `REVIEW_PACKET.md` already exist.

Approved review:

```bash
python -m ai_orchestrator.cli review-run run_20260519_120000_abcd12 \
  --runs-dir .runs \
  --decision approved
```

Rejected review with feedback:

```bash
python -m ai_orchestrator.cli review-run run_20260519_120000_abcd12 \
  --runs-dir .runs \
  --decision rejected \
  --feedback review_feedback.md
```

Rework from stored rejected feedback:

```bash
python -m ai_orchestrator.cli rework-run run_20260519_120000_abcd12 \
  --runs-dir .runs \
  --backend codex_cli \
  --codex-cmd "$CODEX_CMD" \
  --verbose \
  --stream-codex-output
```

Behavior:

- validator-approved means the deterministic validator accepted the run technically;
- human-review approved means a reviewer accepts the generated run artifacts;
- human-review rejected means the reviewer provides feedback for rework;
- `review-run` writes `REVIEW_DECISION.json` and `REVIEW_DECISION.md`;
- if feedback is provided, `review-run` stores it in `.runs/<run_id>/REVIEW_FEEDBACK.md`;
- `rework-run` can reuse stored feedback from a rejected human review decision when `--feedback` is omitted.

Safety notes:

- `review-run` does not call `accept-run`;
- `review-run` does not create a commit;
- feedback may contain sensitive notes;
- `.runs` is ignored runtime storage;
- commit remains manual;
- `accept-run` remains a separate explicit step.

### Inspecting run lifecycle status

`show-run` is a read-only inspection command. It aggregates status from `state.json` plus existing artifacts such as `final_report.md`, `REVIEW_PACKET.md`, `REVIEW_DECISION.json`, `REWORK_FEEDBACK.md`, and `ACCEPTANCE.md`.

Examples:

```bash
python -m ai_orchestrator.cli show-run run_20260519_120000_abcd12 --runs-dir .runs
```

```bash
python -m ai_orchestrator.cli show-run run_20260519_120000_abcd12 --runs-dir .runs --show-paths
```

```bash
python -m ai_orchestrator.cli show-run run_20260519_120000_abcd12 --runs-dir .runs --format json
```

Behavior:

- `show-run` is read-only;
- it does not run agents or Codex;
- it does not re-run validation;
- it does not call `accept-run`;
- it does not call `rework-run`;
- it does not create a commit;
- it helps decide the next manual action.

`next_action` values:

- `review_run`: validator approved the run, but no human review decision is recorded yet;
- `rework_run`: human review rejected the run and feedback/rework is the next step;
- `accept_run`: validator approved the run and human review approved it, but acceptance has not been recorded yet;
- `done`: the run already has `ACCEPTANCE.md`;
- `rework_or_inspect_failure`: validator did not approve the run, so inspect `final_report.md` / validation feedback first.

### Inspecting pipeline lifecycle status

`show-pipeline` is a read-only inspection command for pipeline-level lifecycle state. It aggregates per-run status via the same `show-run` logic and combines it with `pipeline_state.json` and `PIPELINE_REPORT.md`.

Examples:

```bash
python -m ai_orchestrator.cli show-pipeline pipeline_20260520_120000_abcd12 --runs-dir .runs
```

```bash
python -m ai_orchestrator.cli show-pipeline pipeline_20260520_120000_abcd12 --runs-dir .runs --show-paths
```

```bash
python -m ai_orchestrator.cli show-pipeline pipeline_20260520_120000_abcd12 --runs-dir .runs --format json
```

Behavior:

- `show-pipeline` is read-only;
- it does not run agents or Codex;
- it does not re-run validation;
- it does not call `review-run`, `rework-run`, or `accept-run`;
- it does not create a commit;
- it recommends the next safe pipeline-level action.

`next_action` values:

- `review_runs`: at least one validator-approved run still needs human review;
- `rework_run`: at least one run has a rejected human review decision;
- `accept_runs`: at least one run is human-approved and waiting for explicit `accept-run`;
- `done`: all executed runs already have `ACCEPTANCE.md`;
- `rework_or_inspect_failure`: at least one run did not pass validator approval;
- `inspect_pipeline`: inspect `pipeline_state.json` / run references first, for example when a referenced run is missing.

`show-pipeline` works after `run-pipeline`; it is an inspection/triage command, not a replacement for pipeline execution.

### Accept gate requires human review approval

Validator approval is only a technical approval. By default, `accept-run` now requires a recorded human review approval before it can apply files back to a target repo and create a commit.

Normal approved flow:

```bash
python -m ai_orchestrator.cli review-run run_20260519_120000_abcd12 \
  --runs-dir .runs \
  --decision approved

python -m ai_orchestrator.cli accept-run run_20260519_120000_abcd12 \
  --runs-dir .runs \
  --commit-message "fix: apply approved run"
```

Rejected flow:

```bash
python -m ai_orchestrator.cli review-run run_20260519_120000_abcd12 \
  --runs-dir .runs \
  --decision rejected \
  --feedback review_feedback.md

python -m ai_orchestrator.cli rework-run run_20260519_120000_abcd12 \
  --runs-dir .runs \
  --backend codex_cli \
  --codex-cmd "$CODEX_CMD"
```

Emergency / backward-compatibility bypass:

```bash
python -m ai_orchestrator.cli accept-run run_20260519_120000_abcd12 \
  --runs-dir .runs \
  --allow-unreviewed
```

Policy:

- human review `approved` allows `accept-run`;
- human review `rejected` blocks `accept-run`;
- missing human review blocks `accept-run` by default;
- `--allow-unreviewed` only bypasses a missing review decision;
- `--allow-unreviewed` does not override a rejected human review;
- `--allow-unreviewed` is for emergency/backward-compatibility cases and should not be the normal workflow.

## Local files policy

- `tasks.yaml` is a local working file and is usually not committed.
- `tasks.yaml.example` is the committed template that teammates can copy and edit.
- `.runs/` contains runtime artifacts and should not be committed.
- `toy_seed_project*` directories are disposable local test repos/workspaces and should not be committed.
- `node_modules/` and `.venv/` are local dependency directories and should not be committed.

## Safety policy

- `run-task` does not perform a git commit.
- `run-pipeline` does not perform a git commit.
- `run-pipeline` does not call `accept-run`.
- `accept-run` is a separate manual step after review.
- The final commit is created by the user after reviewing the generated artifacts.

## YAML gotchas

- Quote task ids, for example `id: "mock-smoke"`.
- Quote strings like `"on"`, `"off"`, `"yes"`, and `"no"` when you mean strings, because YAML may coerce them to booleans.
- Relative `seed_workspace` paths are resolved relative to the `tasks.yaml` file location, not the current shell directory.

Example `tasks.yaml`:

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
  - id: "0.1.8"
    title: "Add task queue runner"
    prompt: |
      Implement task queue support for ai_orchestrator_mvp.
      Add run-task command that loads a task from tasks.yaml by id.
      Do not commit.
    criteria:
      - "report.status=completed"
      - "tests.status=passed"
    seed_workspace: null
    commit_message: "feat: add task queue runner"

  - id: "toy-fix"
    enabled: false
    title: "Fix toy subtract bug"
    prompt: |
      You are working in an isolated copy of a seeded Python toy project.

      Fix the bug in src/toy_calc.py so that subtract(a, b) returns a - b.
      Run:
        python -m unittest discover -s tests -t .
      Create EXECUTION_REPORT.json using the required structured schema.
      Do not create EXECUTION_REPORT.md.
    criteria:
      - "report.status=completed"
      - "changed_files includes src/toy_calc.py"
      - "commands_run includes python -m unittest discover -s tests -t ."
      - "tests.status=passed"
    seed_workspace: "toy_seed_project_0172"
    commit_message: "fix: correct toy subtract implementation"
```

This example keeps `toy-fix` disabled until `toy_seed_project_0172` exists locally.

Run one task by id:

```bash
./.venv/Scripts/python.exe -m ai_orchestrator.cli run-task toy-fix \
  --tasks-file tasks.yaml
```

Optional CLI overrides still work and take priority over `tasks.yaml`:

```bash
./.venv/Scripts/python.exe -m ai_orchestrator.cli run-task toy-fix \
  --tasks-file tasks.yaml \
  --backend codex_cli \
  --codex-cmd "$CODEX_CMD" \
  --max-retries 3 \
  --verbose \
  --stream-codex-output
```

`run-task` only prepares and executes the run. It does **not** call `accept-run`, does **not** apply changes back to the target repo, and does **not** create a git commit automatically.

After review, the next step stays manual:

- review `.runs/<run_id>/REVIEW_PACKET.md`
- either use `accept-run` explicitly
- or apply/commit manually with your normal git workflow

The optional `commit_message` field is stored as run metadata only. `run-task` never turns it into an automatic commit.

## Multi-task pipeline runner

`run-pipeline` builds on top of the same `tasks.yaml` / `run-task` mechanism, but executes multiple tasks sequentially in declaration order.

High-level difference:

- `run-task` executes one task by id
- `run-pipeline` selects multiple tasks, runs them one by one, and creates pipeline-level artifacts

`run-pipeline` does **not** call `accept-run`, does **not** apply changes back to the target repo, and does **not** create git commits. It only creates task run artifacts plus:

```text
.runs/pipelines/<pipeline_id>/pipeline_state.json
.runs/pipelines/<pipeline_id>/PIPELINE_REPORT.md
```

Example `tasks.yaml` for pipeline runs:

```yaml
project: ai_orchestrator_mvp
defaults:
  backend: mock
  max_retries: 2
  verbose: true

tasks:
  - id: "0.1.9"
    title: "Add multi-task pipeline runner"
    prompt: |
      Implement pipeline support for ai_orchestrator_mvp.
      Add run-pipeline command.
      Do not commit.
    criteria:
      - "deterministic demo artifact"

  - id: "docs-followup"
    title: "Document pipeline flow"
    prompt: |
      Update README examples for the new pipeline flow.
      Do not commit.
    criteria:
      - "deterministic demo artifact"

  - id: "future-disabled"
    title: "Disabled example task"
    enabled: false
    prompt: |
      This task is intentionally disabled for now.
```

Example 1: dry-run

```bash
python -m ai_orchestrator.cli run-pipeline \
  --tasks-file tasks.yaml \
  --dry-run
```

Example 2: run all enabled tasks

```bash
python -m ai_orchestrator.cli run-pipeline \
  --tasks-file tasks.yaml \
  --verbose
```

Example 3: run from task

```bash
python -m ai_orchestrator.cli run-pipeline \
  --tasks-file tasks.yaml \
  --from-task 0.1.9 \
  --verbose
```

Example 4: run only selected tasks

```bash
python -m ai_orchestrator.cli run-pipeline \
  --tasks-file tasks.yaml \
  --only task-a \
  --only task-c \
  --verbose
```

Useful flags:

- `--dry-run` prints the plan only and does not create `pipeline_id`, `pipeline_state.json`, `PIPELINE_REPORT.md`, or task run directories
- `--from-task <task_id>` starts with one task and includes every task after it
- `--only <task_id>` restricts execution to specific task ids while keeping declaration order from `tasks.yaml`
- `--continue-on-failure` keeps running later tasks, but final pipeline status is still failed if any task fails

`pipeline_state.json` stores the pipeline id, tasks file path, final status, selected tasks, executed task results, and per-task artifact paths. `PIPELINE_REPORT.md` stores the readable summary table and the explicit line:

```text
No accept-run or commit was performed by run-pipeline.
```

## Controlled accept/commit

После `status=approved` orchestrator создает:

```text
.runs/<run_id>/REVIEW_PACKET.md
```

Review packet содержит:

```text
run status
structured report summary
validation feedback
baseline-relative changed_files
apply plan
diff preview
accept command
```

Сначала смотри пакет:

```bash
RUN_ID="<run_id>"
cat ".runs/$RUN_ID/REVIEW_PACKET.md"
```

Если результат принят, применить изменения из isolated workspace обратно в seed git repo и сделать commit можно отдельной командой:

```bash
./.venv/Scripts/python.exe -m ai_orchestrator.cli accept-run "$RUN_ID" \
  --runs-dir .runs \
  --commit-message "fix: correct toy subtract implementation"
```

Ожидаемо:

```text
accept_status=accepted
commit_hash=<hash>
```

Проверка target repo:

```bash
git -C toy_seed_project log -1 --oneline
git -C toy_seed_project show --stat --oneline HEAD
git -C toy_seed_project show -- src/toy_calc.py
```

Что делает `accept-run`:

```text
1. требует final_status=approved;
2. требует валидный EXECUTION_REPORT.json;
3. берет target repo из seed_workspace_path или из --target-workspace;
4. отказывается работать с dirty git repo;
5. применяет только безопасные changed_files;
6. не переносит EXECUTION_REPORT.json в target repo;
7. делает git commit;
8. пишет .runs/<run_id>/ACCEPTANCE.md.
```

Dry-run без изменений и commit:

```bash
./.venv/Scripts/python.exe -m ai_orchestrator.cli accept-run "$RUN_ID" \
  --runs-dir .runs \
  --dry-run
```

Если `toy_seed_project` уже создан, но не был заранее инициализирован как git repo, для disposable/toy проверки можно выполнить:

```bash
./.venv/Scripts/python.exe -m ai_orchestrator.cli accept-run "$RUN_ID" \
  --runs-dir .runs \
  --commit-message "fix: correct toy subtract implementation" \
  --init-target-git
```

Для реальных проектов `--init-target-git` использовать не нужно: целевой проект должен быть уже существующим clean git repo.

## Troubleshooting

### `ModuleNotFoundError: No module named 'ai_orchestrator'`

Причина: проект не установлен в venv или запускается не тот Python.

```bash
./.venv/Scripts/python.exe -m pip install -e .
./.venv/Scripts/python.exe -m ai_orchestrator.cli "test"
```

### `codex: command not found`

```bash
NODE_HOME="/c/Users/Slivin.Aleksandr/Tools/node"
export PATH="$NODE_HOME:$PATH"
hash -r
/c/Users/Slivin.Aleksandr/Tools/node/npm.cmd install
CODEX_CMD="$(pwd -W)/node_modules/.bin/codex.cmd"
"$CODEX_CMD" --version
```

### `""node"" не является внутренней или внешней командой`

```bash
NODE_HOME="/c/Users/Slivin.Aleksandr/Tools/node"
export PATH="$NODE_HOME:$PATH"
hash -r
cmd //c "where node"
```

### `'.' is not recognized as an internal or external command`

Используй абсолютный Windows path:

```bash
CODEX_CMD="$(pwd -W)/node_modules/.bin/codex.cmd"
```

и передавай:

```bash
--codex-cmd "$CODEX_CMD"
```

### `401 Unauthorized: Missing bearer or basic authentication in header`

Частая причина: задан пустой локальный `CODEX_HOME`.

```bash
unset CODEX_HOME
```

### `seed workspace does not exist`

If `toy_seed_project_0172` or another local seed workspace does not exist yet, keep that task at `enabled: false` until the directory is created.

Если запускаешь Windows Python из Git Bash, не передавай `/c/Users/...` как seed path. Используй Windows-style path:

```bash
SEED_WORKSPACE="$(pwd -W)/toy_seed_project"
```

### Manifest validation failed: unreported files

Причина: Codex создал/изменил reportable-файл, но не указал его в `EXECUTION_REPORT.json.changed_files`.

Исправление: либо Codex должен удалить лишний файл, либо добавить его в `changed_files`, если изменение действительно нужно.

### Manifest validation failed: unchanged files relative to baseline

Причина: при `--seed-workspace` Codex указал в `changed_files` файл, который был в seed project и фактически не изменился.

Исправление: убрать неизмененный файл из `changed_files`.


### `accept-run` отказался: target workspace is not a git repository

Причина: target workspace из `--seed-workspace` не содержит `.git`, а `accept-run` делает именно git commit.

Правильный вариант для seed-проекта:

```bash
git -C toy_seed_project init
git -C toy_seed_project config user.email "local@example.com"
git -C toy_seed_project config user.name "Local User"
git -C toy_seed_project add .
git -C toy_seed_project commit -m "seed toy project baseline"
```

Затем повтори:

```bash
./.venv/Scripts/python.exe -m ai_orchestrator.cli accept-run "$RUN_ID" --runs-dir .runs
```

Для disposable/toy проверки можно вместо ручного init использовать:

```bash
./.venv/Scripts/python.exe -m ai_orchestrator.cli accept-run "$RUN_ID" --runs-dir .runs --init-target-git
```

### `accept-run` отказался: target git repository is dirty

Причина: в seed/target repo уже есть незакоммиченные или untracked изменения. Это защитное поведение.

Проверь:

```bash
git -C toy_seed_project status --short
```

Дальше либо закоммить/удали эти изменения, либо используй другой clean clone.

## Что дальше

0.1.8 закрывает ручной gate `review → run-task → review packet → accept-run/commit`. Следующий архитектурный шаг — multi-task queue/pipeline runner:

```text
0.1.9 — multi-task queue/pipeline orchestration
```

Идея:

```text
tasks.yaml
↓
run one task in isolated workspace
↓
validate structured report / tests / manifest
↓
generate REVIEW_PACKET.md
↓
manual assistant/human approval
↓
accept-run commit
↓
next task
```


## accept-run idempotent disposable note

For disposable toy workspaces using `--init-target-git`, `accept-run` can return `accept_status=accepted_noop` when the target already matches the accepted workspace contents. Normal existing git repositories still reject empty accepts with `accept-run found no target changes to commit`.
