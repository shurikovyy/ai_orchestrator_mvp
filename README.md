# AI Orchestrator MVP

Минимальный workflow-first инструмент для управляемого цикла:

```text
task intake -> isolated execution -> deterministic validation -> review findings/arbitration -> human approval -> apply-run -> manual commit
```

Ключевая идея: **Codex/LLM может выполнять работу, но не принимает решение о приемке и не должен владеть финальным переходом в target repo**.

Текущая safety-модель:

- deterministic validator проверяет технические инварианты, но **не равен human approval**;
- review findings и review arbitration могут блокировать `review-run --decision approved`;
- human approval записывается явно через `review-run`;
- recommended path после approval: `apply-run`, затем ручной `git diff`, тесты, `git add` и `git commit`;
- `accept-run` остается advanced explicit path для случаев, когда оператор осознанно хочет delegated apply + git commit.

## Local web MVP

Run:

```bash
python -m ai_orchestrator_web
```

Open:

```text
http://127.0.0.1:8765
```

Current scope:

- read-only dashboard;
- read-only drafts list at `/drafts`;
- read-only draft detail at `/drafts/<draft_id>`;
- read-only tasks list at `/tasks`;
- task detail and explicit enable/disable gate at `/tasks/<task_id>`;
- read-only runs list at `/runs`;
- read-only run detail at `/runs/<run_id>`;
- read-only pipelines list at `/pipelines`;
- read-only pipeline detail at `/pipelines/<pipeline_id>`;
- local allowlisted jobs at `/jobs`;
- safe task request form at `/drafts/new`;
- no apply/accept/commit.

Draft pages do not validate, revise, promote, run Codex, run pipeline, apply, or commit.
Task pages expose an explicit enable/disable gate plus doctor dry-run diagnostics, pipeline dry-run planning, doctor real-run readiness, and the confirmed real pipeline action. They do not apply, accept, or commit.
Run and pipeline pages do not classify, run review checks, prepare review, record findings, approve/reject, apply, accept, commit, run Codex, or run pipeline.
The generic `/jobs` form exposes only safe allowlisted actions and never arbitrary shell commands. Job metadata and logs are stored under `.web/jobs/`.
New Task Request at `/drafts/new` is a write-capable but safe scaffold flow: it creates a local raw request and task draft scaffold only. It does not run Codex, run pipeline, validate, promote, apply, accept, or commit.
Validate draft is available from `/drafts/<draft_id>` when the deterministic next action is `validate_task_draft`. It writes only draft-local validator reports and manifest validation metadata; it does not run Codex, run pipeline, promote, apply, accept, or commit.
Promote disabled is available from `/drafts/<draft_id>` when the deterministic next action is `promote_task_draft`. It writes to `tasks.yaml` with `enabled=false`; it does not enable the task, run doctor, run pipeline, run Codex, apply, accept, or commit.
Enable task and Disable task are available from `/tasks/<task_id>`. They only change the local `tasks.yaml` enabled flag; they do not run doctor, run pipeline, run Codex, apply, accept, or commit.
Doctor dry-run is available from `/tasks/<task_id>`. It checks readiness for `run-pipeline --dry-run`; it does not run Codex, execute tasks, apply, accept, or commit.
Pipeline dry-run is available from `/tasks/<task_id>` as a planning-only action and always uses `--dry-run`. It previews `run-pipeline --dry-run`; it does not run Codex, execute tasks, create real run artifacts, apply, accept, or commit.
Doctor real-run readiness is available from `/tasks/<task_id>` when `CODEX_CMD` or `AI_ORCHESTRATOR_CODEX_CMD` is configured before starting the web app. It checks readiness for real execution; it does not run Codex execution, run pipeline, apply, accept, or commit.
Run real pipeline is available only from `/tasks/<task_id>` when the task is enabled, `CODEX_CMD` or `AI_ORCHESTRATOR_CODEX_CMD` is configured, and the operator explicitly confirms. It launches the orchestrator with Codex in an isolated workspace and creates `.runs` artifacts; it does not apply changes, accept, or commit.
Main web pages include Home navigation plus Drafts, Tasks, Runs, Pipelines, and Jobs links.

## Current capabilities

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
- поддерживает human review gate через `review-run`;
- поддерживает manual apply workflow через `apply-run`;
- поддерживает delegated apply+commit через explicit `accept-run`;
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
Ran N tests ... OK
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

### Structured review findings

Deterministic validator approval is not always enough for safe self-improvement. Independent reviewers can record structured findings in machine-readable form, and open blocking findings prevent `review-run --decision approved`.

Findings severity levels:

- `critical`
- `major`
- `minor`
- `nit`

Blocking policy:

- open `critical` findings are blocking;
- open `major` findings are blocking;
- `minor` and `nit` findings are non-blocking;
- `accepted_risk` and `resolved` findings are non-blocking for this MVP, but they are still tracked explicitly.

Accepted-risk policy:

- `accepted_risk` means a human or another explicitly governed process has decided to carry the risk;
- `accepted_risk` does not count as `blocking_open`;
- `accepted_risk` findings do not enter generated rework feedback;
- `accepted_risk` must not be treated as a way to bypass human governance or silently auto-approve risky work;
- `critical` and `major` accepted-risk findings still require special caution in future stages.

Findings are stored in run artifacts as:

- `REVIEW_FINDINGS.json`
- `REVIEW_FINDINGS.md`

Example findings JSON:

```json
{
  "schema_version": "1.0",
  "run_id": "run_20260521_120000_abcd12",
  "summary": "QA review found missing regression coverage.",
  "overall_decision": "needs_rework",
  "findings": [
    {
      "id": "F001",
      "reviewer": "qa",
      "category": "qa",
      "severity": "major",
      "title": "Missing regression test",
      "evidence": "The diff changes apply logic but no negative test covers dirty target repo.",
      "required_action": "Add regression test for dirty target repo protection.",
      "file": "src/ai_orchestrator/apply.py",
      "line": null,
      "status": "open"
    }
  ]
}
```

Record findings:

```bash
python -m ai_orchestrator.cli record-findings run_20260521_120000_abcd12 \
  --runs-dir .runs \
  --findings-file review_findings.json
```

Profile-aware ingestion for external reviewer output:

```bash
python -m ai_orchestrator.cli record-findings run_20260521_120000_abcd12 \
  --runs-dir .runs \
  --findings-file reviewer_findings.json \
  --profile qa
```

Then inspect the run:

```bash
python -m ai_orchestrator.cli show-run run_20260521_120000_abcd12 --runs-dir .runs
```

If blocking findings exist:

- `review-run --decision approved` fails;
- the normal flow is `review-run --decision rejected --feedback ...` and then `rework-run`.

Ingestion hardening:

- duplicate finding ids are rejected;
- `finding.file` must be a safe relative workspace path;
- with `--profile <id>`, every `finding.reviewer` must match that profile exactly;
- with `--profile <id>`, every `finding.category` must be allowed by that reviewer profile contract;
- `record-findings` does not silently rewrite reviewer ids or categories.

Findings provenance:

- `source_kind=manual` is recorded for plain manual ingestion by default;
- `source_kind=reviewer_profile` and `source_profile=<id>` are recorded when `record-findings --profile <id>` is used;
- `source_kind=deterministic` and `source_profile=deterministic` are recorded for `run-review-checks`.

Future step:

- `rework-run` may eventually consume `REVIEW_FINDINGS` directly as structured rework feedback, but this stage keeps findings recording separate from the existing human feedback flow.

### Deterministic review checks

Deterministic review checks are the first independent review layer on top of validator approval. They generate structured findings without using an LLM, without launching Codex, and without modifying the target repo.

Run them explicitly:

```bash
python -m ai_orchestrator.cli run-review-checks run_20260521_120000_abcd12 --runs-dir .runs
```

Then inspect findings status:

```bash
python -m ai_orchestrator.cli show-run run_20260521_120000_abcd12 --runs-dir .runs
```

If findings block approval, the normal flow is:

```bash
python -m ai_orchestrator.cli review-run run_20260521_120000_abcd12 \
  --runs-dir .runs \
  --decision rejected \
  --feedback review_feedback.md
```

Profiles:

```bash
python -m ai_orchestrator.cli run-review-checks run_20260521_120000_abcd12 \
  --runs-dir .runs \
  --profile code-safety
```

- `default`: general deterministic checks across changed files, report integrity, tests, and change breadth.
- `docs-only`: lightweight policy set for documentation-focused work.
- `code-safety`: emphasizes high-risk orchestration files and source/test discipline.

Behavior:

- deterministic checks generate `REVIEW_FINDINGS.json` and `REVIEW_FINDINGS.md`;
- they do not use LLM reviewers;
- they do not run Codex or `codex exec`;
- they do not re-run the main validator;
- they do not call `review-run`, `rework-run`, `apply-run`, or `accept-run`;
- they do not modify the target repo;
- open `critical` and `major` findings block `review-run --decision approved`.

### Reviewer profiles

Reviewer profiles are contracts, not active reviewer agents. They define expected focus areas, severity guidance, evidence requirements, and output discipline for future independent reviewers that will produce `REVIEW_FINDINGS.json` / `REVIEW_FINDINGS.md`.

Profile contracts live in Python code plus markdown prompt templates under:

```text
src/ai_orchestrator/prompts/reviewers/
```

Changing those markdown templates changes future reviewer prompt packets, but does not execute any reviewer by itself.

Built-in profiles:

- `deterministic`
- `qa`
- `architecture`
- `maintainability`
- `ops`
- `security`
- `business`
- `data`

Important rules:

- profiles do not approve or reject runs;
- profiles do not apply or commit changes;
- profiles describe reviewer responsibilities only;
- reviewer prompt templates must preserve these constraints:
  - findings only
  - no approve/reject
  - no apply/commit
  - evidence-based findings
- future LLM reviewer agents must emit `ReviewFinding`-compatible JSON, not free-form prose;
- external reviewer outputs should be ingested with `record-findings --profile <id>` whenever the reviewer is operating under a built-in profile contract;
- the `deterministic` profile is already active today through `run-review-checks`;
- the other built-in profiles are future reviewer contracts and are not executed automatically in this stage.

Examples:

```bash
python -m ai_orchestrator.cli list-review-profiles
python -m ai_orchestrator.cli show-review-profile qa
python -m ai_orchestrator.cli show-review-profile security --format json
```

### Preparing reviewer prompt packets

`prepare-review` creates reviewer prompt packets for future external, human, or LLM reviewers, but it does not run those reviewers. It only assembles role contract information plus run artifacts into a markdown prompt packet.

Examples:

```bash
python -m ai_orchestrator.cli prepare-review run_20260522_120000_abcd12 \
  --runs-dir .runs \
  --profile qa
```

```bash
python -m ai_orchestrator.cli prepare-review run_20260522_120000_abcd12 \
  --runs-dir .runs \
  --profile qa \
  --profile architecture \
  --profile security
```

```bash
python -m ai_orchestrator.cli prepare-review run_20260522_120000_abcd12 \
  --runs-dir .runs \
  --all-profiles
```

Behavior:

- `prepare-review` writes reviewer prompt artifacts under `.runs/<run_id>/reviewer_prompts/`;
- it creates prompt packets only;
- it does not run any reviewer agent;
- it does not create `REVIEW_FINDINGS.json`;
- it does not approve or reject the run;
- it does not modify the target repo.

Workflow:

1. `prepare-review` creates the prompt packet.
2. An external reviewer or future reviewer agent reads that prompt.
3. The reviewer produces findings JSON compatible with `ReviewFindingsReport`.
4. The user records the findings:

```bash
python -m ai_orchestrator.cli record-findings run_20260522_120000_abcd12 \
  --runs-dir .runs \
  --findings-file reviewer_findings.json
```

Reviewers produce findings only. Human review and apply/commit gates remain separate.

### Risk classification and required reviewer profiles

`classify-run` is a deterministic pre-review classification step. It reads `EXECUTION_REPORT.json.changed_files`, applies policy rules, writes `RISK_CLASSIFICATION.json` / `RISK_CLASSIFICATION.md`, and chooses required reviewer profiles for the run.

Examples:

```bash
python -m ai_orchestrator.cli classify-run run_20260522_120000_abcd12 --runs-dir .runs
```

```bash
python -m ai_orchestrator.cli show-run run_20260522_120000_abcd12 --runs-dir .runs
```

```bash
python -m ai_orchestrator.cli prepare-review run_20260522_120000_abcd12 \
  --runs-dir .runs \
  --required-profiles
```

Behavior:

- `classify-run` is deterministic and read-only with respect to the target repo;
- it does not run reviewers;
- it does not create `REVIEW_FINDINGS.json`;
- it does not approve or reject the run;
- it does not apply or commit changes.

Risk policy examples:

- low-risk docs-only changes may require no mandatory reviewer profiles;
- tests-only changes require `qa`;
- source code changes typically require `qa` and `architecture`, with `maintainability` often optional;
- safety-critical orchestration files require `security`, `architecture`, `qa`, `ops`, and `maintainability`;
- broad or maintainability-sensitive changes may require `maintainability` review even when behavior is otherwise correct;
- data logic changes require `data` and `qa`.

`prepare-review --required-profiles` reads `RISK_CLASSIFICATION.json` and prepares prompt packets only for `required_review_profiles`. This supports a stricter lifecycle:

```text
approved run
-> classify-run
-> prepare-review --required-profiles
-> external reviewer(s)
-> record-findings --profile ...
-> human review
```

This risk layer supports near-autonomous self-improvement under human governance by making reviewer requirements deterministic before any future reviewer agent is involved.

### Maintainability as a quality gate

Maintainability is a first-class quality concern in `ai_orchestrator`.

Practical meaning:

- AI-generated code must remain readable and reviewable by humans;
- the `maintainability` reviewer profile exists to flag over-engineering, hidden side effects, oversized modules, and avoidable complexity;
- deterministic review checks can already produce maintainability findings for obvious risks such as broad change surfaces, oversized Python modules, or CLI/schema coupling changes;
- broad and safety-critical changes may require explicit maintainability review in addition to security, architecture, and QA review;
- maintainability review should prefer simpler code and should not demand abstractions only for aesthetics.

Reference:

```text
docs/maintainability_policy.md
```

### Schema module organization

Core execution, task, and run-state schemas remain in `src/ai_orchestrator/schemas.py`.

Domain-specific schemas live in focused modules:

- review findings: `src/ai_orchestrator/review_findings_schemas.py`
- review arbitration: `src/ai_orchestrator/review_arbitration_schemas.py`
- risk classification: `src/ai_orchestrator/risk_schemas.py`

Compatibility imports remain available from `ai_orchestrator.schemas`, so older code such as `from ai_orchestrator.schemas import ReviewFinding` still works. New production code should prefer the domain-specific schema modules so `schemas.py` stays focused and human-maintainable.

### Findings to rework feedback

`run-review-checks` and `record-findings` create structured findings, but they do not reject a run by themselves. `findings-feedback` turns open findings into concrete markdown feedback that can be reused for a rejected human review and then by `rework-run`.

Artifacts:

- `REVIEW_FINDINGS.json`
- `REVIEW_FINDINGS.md`
- `REVIEW_FEEDBACK_FROM_FINDINGS.md`

Default behavior:

- `findings-feedback` includes open blocking findings only;
- `--include-non-blocking` also includes open `minor` / `nit` findings as secondary suggestions;
- resolved findings are excluded;
- `accepted_risk` findings are excluded for this MVP, even with `--include-non-blocking`.

Example flow:

```bash
python -m ai_orchestrator.cli run-review-checks run_20260521_120000_abcd12 --runs-dir .runs

python -m ai_orchestrator.cli findings-feedback run_20260521_120000_abcd12 --runs-dir .runs

python -m ai_orchestrator.cli review-run run_20260521_120000_abcd12 \
  --runs-dir .runs \
  --decision rejected \
  --from-findings

python -m ai_orchestrator.cli rework-run run_20260521_120000_abcd12 \
  --runs-dir .runs \
  --backend codex_cli \
  --codex-cmd "$CODEX_CMD" \
  --verbose \
  --stream-codex-output
```

Important notes:

- `findings-feedback` does not approve or reject a run by itself;
- `review-run` still records the explicit human decision;
- `review-run --decision rejected --from-findings` generates or reuses `REVIEW_FEEDBACK_FROM_FINDINGS.md` and stores the rejected review using that feedback;
- generated findings feedback is deterministic and is not silently regenerated unless explicitly requested;
- `review-run --force` overwrites an existing human review decision only;
- `review-run --force-feedback` regenerates `REVIEW_FEEDBACK_FROM_FINDINGS.md` only when `--from-findings` is used;
- `review-run --force --force-feedback` overwrites both the human review decision and the generated findings feedback;
- `rework-run` creates a new run and can consume the stored rejected-review feedback automatically;
- no LLM is involved in this findings-to-feedback path;
- no target repo changes or commits occur in this flow;
- critical and major findings remain blocking until they are resolved in a later run.

### Review arbitration

Reviewer profiles can conflict. For example, `security` may prefer stricter protection, `maintainability` may flag the same solution as over-engineered, `architecture` may suggest a different compromise, and `business` may argue that a finding is outside the task objective. Review arbitration is the structured layer for resolving those conflicts without weakening hard gates.

`record-arbitration` does not run an LLM arbiter by itself. It records an external or manual arbitration report into:

- `REVIEW_ARBITRATION.json`
- `REVIEW_ARBITRATION.md`

Arbitration may:

- uphold a finding
- downgrade or upgrade severity
- dismiss a finding
- mark `needs_evidence`
- record `conflict`
- record `accepted_risk`

Important policy:

- deterministic hard gates cannot be dismissed;
- deterministic critical/major findings cannot be downgraded below their original severity;
- arbitration may require explicit human escalation;
- arbitration is tied to the exact `REVIEW_FINDINGS.json` bytes via `sha256`;
- if findings are replaced after arbitration, the arbitration becomes stale;
- stale arbitration cannot be used to approve a run;
- `show-run` and `show-pipeline` expose `arbitration_stale=true|false`;
- `review-run --decision approved` uses arbitration results when they exist;
- without arbitration, raw blocking findings still block approval.

Example:

```bash
python -m ai_orchestrator.cli record-arbitration run_20260523_120000_abcd12 \
  --runs-dir .runs \
  --arbitration-file arbitration.json
```

Then inspect the run:

```bash
python -m ai_orchestrator.cli show-run run_20260523_120000_abcd12 --runs-dir .runs
```

If arbitration resolves the findings with `overall_decision=pass` and no final blocking items remain, approval may proceed:

```bash
python -m ai_orchestrator.cli review-run run_20260523_120000_abcd12 --runs-dir .runs --decision approved
```

If arbitration still ends in rework, keep the human review gate explicit:

```bash
python -m ai_orchestrator.cli review-run run_20260523_120000_abcd12 \
  --runs-dir .runs \
  --decision rejected \
  --feedback review_feedback.md
```

If findings are replaced after arbitration, record a fresh arbitration against the current findings:

```bash
python -m ai_orchestrator.cli record-findings run_20260523_120000_abcd12 \
  --runs-dir .runs \
  --findings-file reviewer_findings_v2.json \
  --force

python -m ai_orchestrator.cli show-run run_20260523_120000_abcd12 --runs-dir .runs
```

If `show-run` reports:

```text
arbitration_stale=true
```

then re-record arbitration for the current `REVIEW_FINDINGS.json`:

```bash
python -m ai_orchestrator.cli record-arbitration run_20260523_120000_abcd12 \
  --runs-dir .runs \
  --arbitration-file arbitration.json \
  --force
```

### Self-improvement autonomy goal

The long-term direction of `ai_orchestrator` is near-autonomous self-development under hard validation and human governance.

Practical meaning:

- the system may eventually implement improvements to itself;
- independent validators/reviewers can record structured findings that demand rework;
- reviewer profiles define how future independent reviewers must reason, what evidence they must provide, and which categories/severity guidance they should use;
- reviewer agents, when added in the future, must produce findings only and must not approve, apply, or commit changes;
- critical and major open findings block approval;
- final apply/commit remains human-governed unless a task class is explicitly safe enough to automate;
- critical lifecycle files such as validation, review, apply, and acceptance gates should always receive stricter scrutiny.

### Inspecting run lifecycle status

`show-run` is a read-only inspection command. It aggregates status from `state.json` plus existing artifacts such as `final_report.md`, `REVIEW_PACKET.md`, `REVIEW_FINDINGS.json`, `REVIEW_DECISION.json`, `REWORK_FEEDBACK.md`, `APPLY_REPORT.md`, and `ACCEPTANCE.md`.

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

- `classify_run`: classify run risk before review routing;
- `prepare_required_reviews`: prepare prompt packets for required reviewer profiles;
- `run_review_checks`: run deterministic review checks before human review;
- `run_external_reviewer_or_record_findings`: run required external review or record its findings;
- `review_run`: review checks/findings are clear enough for explicit human review;
- `arbitrate_findings`: blocking findings need arbitration before approval;
- `human_escalation`: arbitration requires explicit human escalation;
- `review_rejected`: final blocking findings require a rejected review and rework;
- `rework_run`: human review rejected the run and feedback/rework is the next step;
- `apply_run`: validator approved the run and human review approved it, but files have not been applied back to the target repo yet;
- `manual_commit`: files were already applied with `apply-run`, so inspect `git diff`, run tests, and commit manually;
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

- `classify_runs`: at least one run needs risk classification;
- `prepare_required_reviews`: at least one run needs required reviewer prompt packets;
- `run_review_checks`: at least one run needs deterministic review checks;
- `run_external_reviewer_or_record_findings`: at least one run needs external review findings recorded;
- `review_runs`: at least one reviewed run still needs an explicit human review decision;
- `arbitrate_findings`: at least one run has blocking findings that need arbitration;
- `human_escalation`: at least one run requires human escalation;
- `review_rejected`: at least one run is blocked and waiting for an explicit rejected human review;
- `rework_run`: at least one run has a rejected human review decision;
- `apply_runs`: at least one run is human-approved and waiting for explicit `apply-run`;
- `manual_commit`: at least one run was already applied and now needs manual git review/commit;
- `done`: all executed runs already have `ACCEPTANCE.md`;
- `rework_or_inspect_failure`: at least one run did not pass validator approval;
- `inspect_pipeline`: inspect `pipeline_state.json` / run references first, for example when a referenced run is missing.

`show-pipeline` works after `run-pipeline`; it is an inspection/triage command, not a replacement for pipeline execution.

### Applying approved run without commit

`apply-run` closes the gap between validator/human approval and a final manual git commit. It applies approved workspace files back into the target repo, but intentionally does **not** run `git add` and does **not** create a commit.

Normal manual-commit workflow:

```bash
python -m ai_orchestrator.cli review-run run_20260519_120000_abcd12 \
  --runs-dir .runs \
  --decision approved

python -m ai_orchestrator.cli apply-run run_20260519_120000_abcd12 \
  --runs-dir .runs

git diff --stat
git diff
python -m unittest discover -s tests
git add ...
git commit -m "fix: apply approved run manually"
```

Behavior:

- `apply-run` requires validator approval;
- it requires human review approval by default;
- it can use `--allow-unreviewed` only for missing review decisions, never for rejected reviews;
- it applies only allowed changed files from the isolated workspace;
- it skips runtime/generated artifacts such as `EXECUTION_REPORT.json`;
- it writes `.runs/<run_id>/APPLY_REPORT.md` and `.runs/<run_id>/APPLY_REPORT.json`;
- it leaves the target repo dirty and unstaged on purpose for manual inspection;
- it does not call `accept-run`;
- it does not create a commit.

Compare the two commands:

- `apply-run`: apply files only, no staging, no commit;
- `accept-run`: apply files and create a git commit.

### apply-run vs accept-run

| Command | Applies files | git add | git commit | Report |
|---|---:|---:|---:|---|
| `apply-run` | yes | no | no | `APPLY_REPORT.md` / `APPLY_REPORT.json` |
| `accept-run` | yes | yes | yes | `ACCEPTANCE.md` |

### Recommended manual commit workflow

This is the recommended end-to-end flow when you want the orchestrator to prepare changes, but you want to keep the final git commit as a manual human decision.

1. List tasks:

```bash
python -m ai_orchestrator.cli list-tasks --tasks-file tasks.yaml
```

2. Run a single pipeline task:

```bash
python -m ai_orchestrator.cli run-pipeline \
  --tasks-file tasks.yaml \
  --only <task_id> \
  --codex-cmd "$CODEX_CMD" \
  --verbose \
  --stream-codex-output
```

3. Inspect the pipeline:

```bash
python -m ai_orchestrator.cli show-pipeline <pipeline_id> --runs-dir .runs
```

4. Inspect the specific run:

```bash
python -m ai_orchestrator.cli show-run <run_id> --runs-dir .runs --show-paths
```

5. Review `REVIEW_PACKET.md` manually.

6. Record the human review decision.

Approve:

```bash
python -m ai_orchestrator.cli review-run <run_id> \
  --runs-dir .runs \
  --decision approved
```

Reject:

```bash
python -m ai_orchestrator.cli review-run <run_id> \
  --runs-dir .runs \
  --decision rejected \
  --feedback review_feedback.md
```

7. If rejected, create a rework run:

```bash
python -m ai_orchestrator.cli rework-run <run_id> \
  --runs-dir .runs \
  --backend codex_cli \
  --codex-cmd "$CODEX_CMD" \
  --verbose \
  --stream-codex-output
```

8. If approved, apply changes without commit:

```bash
python -m ai_orchestrator.cli apply-run <run_id> --runs-dir .runs
```

9. Inspect the target repo manually:

```bash
git diff --stat
git diff
python -m unittest discover -s tests
```

10. Commit manually:

```bash
git add <files>
git commit -m "..."
```

Practical guidance:

- prefer `apply-run` when you want an explicit manual diff/test/commit gate;
- use `accept-run` only when you want the orchestrator to perform the apply + git add + git commit flow for you;
- `--allow-unreviewed` is an emergency/backward-compatibility flag only;
- a rejected human review blocks both `apply-run` and `accept-run`.

### Preflight doctor

`doctor` is a read-only preflight command for checking whether the repository and local environment are ready for a real `run-pipeline` / dogfooding execution.

Examples:

```bash
python -m ai_orchestrator.cli doctor
```

```bash
python -m ai_orchestrator.cli doctor \
  --tasks-file tasks.yaml \
  --task-id 0.1.21-dogfood-manual-workflow-doc \
  --codex-cmd "$CODEX_CMD"
```

```bash
python -m ai_orchestrator.cli doctor \
  --tasks-file tasks.yaml \
  --task-id 0.1.21-dogfood-manual-workflow-doc \
  --skip-tests
```

```bash
python -m ai_orchestrator.cli doctor \
  --tasks-file tasks.yaml \
  --task-id my-task \
  --intent dry-run
```

```bash
python -m ai_orchestrator.cli doctor \
  --tasks-file tasks.yaml \
  --task-id my-task \
  --intent real-run \
  --codex-cmd "$CODEX_CMD"
```

Behavior:

- `doctor` is read-only;
- it checks git repository detection and tracked working tree cleanliness;
- it can run `python -m unittest discover -s tests`, or skip that check with `--skip-tests`;
- it can validate `tasks.yaml`, a specific `task_id`, and that the resolved `seed_workspace` exists;
- it can verify the Codex CLI command only via `<codex-cmd> --version`;
- it does not run agents or `codex exec`;
- it does not run `run-pipeline`, `run-task`, `apply-run`, `accept-run`, `review-run`, or `rework-run`;
- it does not create files, modify artifacts, or create commits.

Doctor intent modes:

- `--intent preflight` is the default backward-compatible general readiness check;
- `--intent dry-run` checks readiness for `run-pipeline --dry-run`; Codex command is not required, so missing `codex_cmd` is reported as info rather than a warning;
- `--intent real-run` checks readiness for actual `run-pipeline`; `codex_cli` tasks should provide `--codex-cmd`, task `codex_cmd`, defaults `codex_cmd`, or `CODEX_CMD`/`AI_ORCHESTRATOR_CODEX_CMD`.

Use `doctor` before a real `run-pipeline` when you want a quick deterministic answer about whether the repo is clean, tests are green, the selected task is valid, and the Codex command is available.

For task-intake dogfood and other planning-only checks, use `--intent dry-run` before `run-pipeline --dry-run`. Before actual pipeline execution, use `--intent real-run`.

Important notes:

- launch `codex_cli` pipelines from a normal terminal, not from inside a Codex VS Code agent session;
- `doctor` cannot reliably detect every nested Codex session, so it prints only a cautionary warning;
- `doctor` runs `codex --version` only; it never runs `codex exec`.

### Dry-run behavior

`apply-run --dry-run` and `accept-run --dry-run` are validation/planning-only commands.

Dry-run guarantees:

- the target repo stays unchanged;
- no `git add` is performed;
- no git commit is created;
- no `.runs/<run_id>/APPLY_REPORT.md` or `.runs/<run_id>/APPLY_REPORT.json` is written;
- no `.runs/<run_id>/ACCEPTANCE.md` is written;
- `state.json` is not updated with applied/accepted state.

Dry-run still enforces the normal safety gates:

- the run must exist;
- validator approval and `EXECUTION_REPORT.json` must be valid;
- the human review gate must pass;
- the target workspace must exist and be a clean git repo;
- unsafe/generated/runtime files are still rejected from the apply plan.

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
- record explicit human approval with `review-run --decision approved`
- use `apply-run` to copy approved files back without staging or committing
- inspect `git diff`, run tests, then commit manually with your normal git workflow
- use `accept-run` only as an advanced explicit delegated apply + commit path

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
recommended manual apply workflow
advanced delegated commit option
```

Сначала смотри пакет:

```bash
RUN_ID="<run_id>"
cat ".runs/$RUN_ID/REVIEW_PACKET.md"
```

Если результат принят, сначала запиши human approval, затем применяй изменения без commit:

```bash
./.venv/Scripts/python.exe -m ai_orchestrator.cli review-run "$RUN_ID" \
  --runs-dir .runs \
  --decision approved

./.venv/Scripts/python.exe -m ai_orchestrator.cli apply-run "$RUN_ID" \
  --runs-dir .runs
```

Ожидаемо после `apply-run`:

```text
apply_status=applied
```

Проверка target repo:

```bash
git diff --stat
git diff
python -m unittest discover -s tests
```

Финальный commit остается ручным:

```bash
git add <files>
git commit -m "fix: correct toy subtract implementation"
```

Что делает `apply-run`:

```text
1. требует final_status=approved;
2. требует валидный EXECUTION_REPORT.json;
3. берет target repo из seed_workspace_path или из --target-workspace;
4. отказывается работать с dirty git repo;
5. применяет только безопасные changed_files;
6. не переносит EXECUTION_REPORT.json в target repo;
7. не делает git add;
8. не делает git commit;
9. пишет .runs/<run_id>/APPLY_REPORT.md и APPLY_REPORT.json.
```

Advanced delegated commit path:

`accept-run` применяет файлы, делает `git add` и создает commit. Используй его только если явно нужен delegated apply + commit.

Dry-run без изменений:

```bash
./.venv/Scripts/python.exe -m ai_orchestrator.cli apply-run "$RUN_ID" \
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

## Current near-term direction

The current dogfood direction is controlled real execution from the task-intake flow, without moving final repository ownership away from a human operator.

Recommended preparation path:

```text
raw_request.md
↓
draft-task-scaffold
↓
validate-task-draft
↓
revise-task-draft if needed
↓
validate-task-draft
↓
promote-task-draft with enabled=false
↓
inspect tasks.yaml / list-tasks
↓
explicit --replace --enable or manual enabled:true
↓
doctor --intent dry-run
↓
run-pipeline --dry-run
↓
user-run real pipeline from a normal Git Bash session
```

After a successful real run, the target repo transition remains human-governed:

```text
show-run / show-pipeline
↓
classify-run / run-review-checks / reviewer findings as needed
↓
review-run --decision approved
↓
apply-run
↓
git diff --stat / git diff / tests
↓
manual git add / git commit
```

`accept-run` remains available only as an advanced explicit delegated apply + commit path.


## accept-run idempotent disposable note

For disposable toy workspaces using `--init-target-git`, `accept-run` can return `accept_status=accepted_noop` when the target already matches the accepted workspace contents. Normal existing git repositories still reject empty accepts with `accept-run found no target changes to commit`.

## Task Draft Intake

Task intake now starts with a deterministic scaffold instead of writing directly to `tasks.yaml`.

Flow:

```text
raw_request.md
-> draft-task-scaffold
-> .task_drafts/<draft_id>/
-> task_draft.yaml
-> codex_prompt.md
-> task_review.md
-> later validation / human review / promotion
```

`draft-task-scaffold` is intentionally safe:

- it does not run Codex
- it does not modify `tasks.yaml`
- it does not create pipeline artifacts
- it does not apply or commit changes
- generated `target_task.enabled` stays `false`

Local task draft workspaces live under:

```text
.task_drafts/
```

This directory is ignored by git and is meant for local draft authoring only.

Create a deterministic scaffold from a raw request:

```bash
python -m ai_orchestrator.cli draft-task-scaffold \
  --request raw_request.md
```

Optional controls:

```bash
python -m ai_orchestrator.cli draft-task-scaffold \
  --request raw_request.md \
  --title "Document operator quickstart task" \
  --task-id operator-quickstart-draft \
  --risk-level medium \
  --prompt-language ru \
  --format json
```

Generated artifacts:

```text
.task_drafts/<draft_id>/
├── raw_request.md
├── task_draft.yaml
├── codex_prompt.md
├── task_review.md
└── MANIFEST.json
```

Notes:

- `raw_request.md` is copied as-is into the draft directory.
- `task_draft.yaml` contains a safe placeholder contract with non-empty guardrails.
- `codex_prompt.md` is a future draft-improvement prompt, not an execution prompt.
- `task_review.md` is a human checklist before any later validation or promotion step.
- `MANIFEST.json` records the generated artifact paths.

The scaffold preserves these constraints by default:

- findings only come later; this stage does not create review findings
- no promotion to `tasks.yaml`
- no weakening of validation, review, apply, or safety gates
- no automatic apply/accept/commit behavior

### Validating task drafts

After scaffold generation, the next deterministic step is validating the draft in place.

Flow:

```text
raw_request.md
-> draft-task-scaffold
-> task_draft.yaml
-> validate-task-draft
-> task_draft_validator_report.json
-> task_draft_validator_report.md
-> later human review / later promotion
```

`validate-task-draft` is deterministic and safe:

- it does not call Codex
- it does not modify `tasks.yaml`
- it does not run the pipeline
- it does not create `.runs`
- it only writes validator reports inside the draft directory

Example:

```bash
python -m ai_orchestrator.cli draft-task-scaffold \
  --request raw_request.md \
  --title "Add show-failed-runs command" \
  --task-id "show-failed-runs"

python -m ai_orchestrator.cli validate-task-draft <draft_id>
```

Then inspect:

```bash
cat .task_drafts/<draft_id>/task_draft_validator_report.md
```

Validator artifacts:

```text
.task_drafts/<draft_id>/
├── task_draft_validator_report.json
└── task_draft_validator_report.md
```

The validator also updates `MANIFEST.json` with:

- `validator_report`
- `validator_report_md`
- `validation_status`
- `valid_for_promotion`
- `validated_at`

Validation status meanings:

- `valid`: no errors or warnings; a later `promote-task-draft` stage may be allowed
- `needs_revision`: no errors, but warnings remain; promotion is blocked for now
- `invalid`: one or more blocking errors exist

Why warnings block promotion for now:

- open questions mean the task scope is still unresolved
- empty or broad `files_allowed` means the scope is too loose
- missing reviewer/risk clarity means later task execution would not be governed tightly enough

Important rules for promotion readiness:

- `target_task.enabled` must stay `false`
- `files_allowed` should be narrowed before promotion
- dangerous commands are rejected
- `task_draft_validator_report.json` must show `valid_for_promotion=true` before any future promotion step

### Inspecting task drafts

`show-task-draft` is a read-only inspection command for deterministic task-intake artifacts.

It summarizes:

- draft artifact paths
- target task id/title/enabled state
- risk level and reviewer profiles
- validation status and promotion readiness
- the next deterministic operator action

Example:

```bash
python -m ai_orchestrator.cli show-task-draft <draft_id>
```

Optional output modes:

```bash
python -m ai_orchestrator.cli show-task-draft <draft_id> --show-paths
python -m ai_orchestrator.cli show-task-draft <draft_id> --format json
```

Safety rules:

- it does not validate the draft
- it does not revise the draft
- it does not promote anything to `tasks.yaml`
- it does not run Codex or the pipeline
- it does not create `.runs`
- it does not apply or commit changes

### Listing task drafts

`list-task-drafts` is a read-only inspection command for local task draft triage.

It summarizes local drafts and their deterministic next action without opening each draft directory manually.

Examples:

```bash
python -m ai_orchestrator.cli list-task-drafts

python -m ai_orchestrator.cli list-task-drafts --status valid --format json
```

Safety rules:

- it does not validate drafts
- it does not revise drafts
- it does not promote anything to `tasks.yaml`
- it does not run Codex or the pipeline
- it does not create `.runs`
- it does not apply or commit changes

### Preparing task draft improvement prompts

`prepare-task-draft-improvement` creates a markdown prompt packet for a future task-authoring agent that may improve a draft later.

It is intentionally artifact-only:

- it does not run Codex or ChatGPT
- it does not modify `task_draft.yaml`
- it does not modify `codex_prompt.md` or `task_review.md`
- it does not promote anything to `tasks.yaml`
- it does not create `.runs`

Typical flow:

```bash
python -m ai_orchestrator.cli draft-task-scaffold --request raw_request.md

python -m ai_orchestrator.cli validate-task-draft <draft_id>

python -m ai_orchestrator.cli prepare-task-draft-improvement <draft_id>
```

The generated artifact is:

```text
.task_drafts/<draft_id>/TASK_DRAFT_IMPROVEMENT_PROMPT.md
```

Give that prompt to Codex/ChatGPT manually in a later human-governed step. The prompt includes the raw request, current `task_draft.yaml`, current `codex_prompt.md`, current `task_review.md`, and validator findings if `task_draft_validator_report.json` exists.

After an external agent proposes an improved draft, the user should save or apply the revised draft deliberately, then run:

```bash
python -m ai_orchestrator.cli validate-task-draft <draft_id>
```

Warnings from `validate-task-draft` still block promotion. This prompt-preparation command does not mark validation stale because it does not edit the draft.

### Importing improved task drafts

After `prepare-task-draft-improvement`, an external task-authoring agent may return a complete improved `task_draft.yaml` plus optional notes. `import-task-draft-improvement` validates that full draft and imports it back into the draft workspace.

Safety rules:

- it does not run Codex or ChatGPT
- it does not run the pipeline
- it does not promote to `tasks.yaml`
- it does not create `.runs`
- it validates the improved draft before replacing the current draft
- it backs up the previous `task_draft.yaml`
- it marks validation stale, so `validate-task-draft` must run again

Example:

```bash
python -m ai_orchestrator.cli prepare-task-draft-improvement <draft_id>

# external agent returns improved_task_draft.yaml and optional notes.md

python -m ai_orchestrator.cli import-task-draft-improvement <draft_id> \
  --improved-draft improved_task_draft.yaml \
  --notes TASK_DRAFT_IMPROVEMENT_NOTES.md

python -m ai_orchestrator.cli validate-task-draft <draft_id>
```

Import writes or updates only draft-local artifacts:

- `task_draft.yaml`
- `codex_prompt.md`
- `task_review.md`
- `MANIFEST.json`
- `task_draft.before_improvement*.yaml`
- optional `TASK_DRAFT_IMPROVEMENT_NOTES.md`

It preserves `raw_request.md` and `TASK_DRAFT_IMPROVEMENT_PROMPT.md`. Invalid improved drafts are rejected before the current draft is replaced.

### Revising task drafts

`revise-task-draft` is the deterministic edit step between scaffold generation and later validation/promotion.

What it does:

- updates `task_draft.yaml` using only explicit CLI changes
- regenerates derived artifacts:
  - `codex_prompt.md`
  - `task_review.md`
- updates `MANIFEST.json` with revision metadata
- marks any previous validation as stale

What it does not do:

- does not run Codex
- does not validate automatically
- does not promote the draft to `tasks.yaml`
- does not create `.runs`
- does not apply or commit changes

Example:

```bash
python -m ai_orchestrator.cli revise-task-draft <draft_id> \
  --risk-level medium \
  --clear-files-allowed \
  --allow-file src/ai_orchestrator/cli.py \
  --allow-file tests/test_example.py \
  --resolve-open-question "Confirm exact files_allowed before promotion." \
  --require-profile qa \
  --require-profile architecture
```

Then rerun deterministic validation:

```bash
python -m ai_orchestrator.cli validate-task-draft <draft_id>
```

Revision notes:

- `revise-task-draft` changes only the draft workspace under `.task_drafts/<draft_id>/`
- `raw_request.md` is preserved as-is
- if the draft was validated earlier, the manifest is updated to:
  - `validation_status=stale`
  - `valid_for_promotion=false`
- warnings and errors from `validate-task-draft` remain authoritative

Use revision to close scope gaps such as:

- narrowing `files_allowed`
- replacing `risk_level=unknown`
- resolving `open_questions`
- setting `required_review_profiles`
- refining commands, acceptance criteria, and rollback notes

### Promoting validated task drafts

`promote-task-draft` converts a validated draft into a local `tasks.yaml` entry.

Safety rules:

- promotion requires `validate-task-draft` to produce:
  - `validation_status=valid`
  - `valid_for_promotion=true`
- stale, invalid, or `needs_revision` drafts cannot be promoted
- `promote-task-draft` does not run Codex
- `promote-task-draft` does not run the pipeline
- `promote-task-draft` does not create `.runs`
- default promoted tasks stay `enabled=false`

Typical flow:

```bash
python -m ai_orchestrator.cli validate-task-draft <draft_id>

python -m ai_orchestrator.cli promote-task-draft <draft_id> \
  --tasks-file tasks.yaml
```

Optional controls:

```bash
python -m ai_orchestrator.cli promote-task-draft <draft_id> \
  --tasks-file tasks.yaml \
  --enable \
  --replace
```

Meaning of the flags:

- `--enable` promotes the task with `enabled=true`
- without `--enable`, promotion keeps `enabled=false`
- `--replace` is required if `tasks.yaml` already contains the same task id

Safe default workflow:

1. promote with `enabled=false`
2. inspect the generated `tasks.yaml`

```bash
python -m ai_orchestrator.cli list-tasks --tasks-file tasks.yaml
```

3. explicitly switch to execution intent only after inspection:

```bash
python -m ai_orchestrator.cli promote-task-draft <draft_id> \
  --tasks-file tasks.yaml \
  --replace \
  --enable
```

You can also edit `enabled: true` manually after inspection. `doctor --intent dry-run` still treats a disabled selected task as not ready, because `run-pipeline --dry-run --only <task_id>` will skip disabled tasks.

4. run deterministic dry-run checks:

```bash
python -m ai_orchestrator.cli doctor \
  --tasks-file tasks.yaml \
  --task-id <task_id> \
  --intent dry-run

python -m ai_orchestrator.cli run-pipeline \
  --tasks-file tasks.yaml \
  --only <task_id> \
  --dry-run
```

Only after that should a real `run-pipeline` happen in a later step.

Notes:

- promoted task prompts are generated from the validated draft, not copied directly from `raw_request.md`
- `target_task.enabled` in the draft is still expected to remain `false`; `--enable` is an explicit promotion-time override only
- if `tasks.yaml` is rewritten, YAML comments may not be preserved in this MVP
- inspect `tasks.yaml` after promotion before enabling or running anything
