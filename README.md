# AI Orchestrator MVP 0.1.6

Минимальный workflow-first инструмент для управляемого цикла:

```text
user task -> planner -> executor -> validator -> retry/rework -> final_report
```

Ключевая идея: **Codex/LLM выполняет работу, но не принимает решение о приемке**. Приемка остается в детерминированном Python-коде.

## Что умеет 0.1.6

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
- при seed workspace сохраняет baseline manifest до Codex и проверяет `changed_files` как diff относительно baseline.

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
Ran 25 tests ... OK
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
  --max-retries 2
```

Ожидаемо:

```text
status=approved
backend=codex_cli
```

Проверка:

```bash
RUN=".runs/<run_id>"
cat "$RUN/final_report.md"
cat "$RUN/artifacts/workspace/EXECUTION_REPORT.json"
cat "$RUN/artifacts/workspace/src/toy_calc.py"
cat "$RUN/artifacts/step_1_attempt_1_codex_log.md"
```

В `final_report.md` должна быть строка:

```text
Workspace file manifest matches structured report changed_files.
```

А `EXECUTION_REPORT.json.changed_files` должен содержать как минимум:

```json
["src/toy_calc.py", "EXECUTION_REPORT.json"]
```

Он не должен перечислять неизмененные seed-файлы.

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

## Что дальше

Следующий архитектурный шаг — безопасное применение результата после approve:

```text
0.1.7 — review packet + controlled apply/commit
```

Идея:

```text
run on seed/disposable workspace
↓
validator approved
↓
generate REVIEW_PACKET.md with diff/stat/report
↓
manual assistant/human approval
↓
apply patch or commit only allowed changed files
```
