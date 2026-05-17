# AI Orchestrator MVP 0.1.9

Минимальный workflow-first инструмент для управляемого цикла:

```text
user task -> planner -> executor -> validator -> retry/rework -> final_report
```

Ключевая идея: **Codex/LLM выполняет работу, но не принимает решение о приемке**. Приемка остается в детерминированном Python-коде.

## Что умеет 0.1.9

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
