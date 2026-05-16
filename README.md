# AI Orchestrator MVP 0.1.4

Минимальный workflow-first инструмент для управляемого цикла:

```text
user task -> planner -> executor -> validator -> retry/rework -> final_report
```

Ключевая идея: **LLM/Codex выполняет работу, но не принимает решение о приемке результата**. Приемка остается в детерминированном Python-коде.

## Что умеет текущая версия

- хранит состояние каждого запуска в `.runs/<run_id>/state.json`;
- сохраняет логи и артефакты в `.runs/<run_id>/artifacts/`;
- поддерживает offline `mock` backend;
- поддерживает `codex_cli` backend через локальный Codex CLI;
- передает prompt в `codex exec` через `stdin`, чтобы не ломаться на Windows `.cmd` и multiline prompt;
- собирает текстовые файлы из isolated workspace в `ExecutionResult.content`;
- поддерживает structured validation через `EXECUTION_REPORT.json` и Pydantic-схему;
- оставляет старую текстовую проверку acceptance criteria как fallback;
- опционально повторно запускает test-команды из `EXECUTION_REPORT.json`, чтобы не доверять только отчету executor-а.

## Структура проекта

```text
ai_orchestrator_mvp/
├── pyproject.toml
├── package.json
├── package-lock.json
├── README.md
├── examples/
├── src/ai_orchestrator/
│   ├── cli.py
│   ├── engine.py
│   ├── schemas.py
│   ├── validation.py
│   └── backends/
│       ├── base.py
│       ├── mock.py
│       └── codex_cli.py
└── tests/
```

## Важные ограничения

Не коммить и не передавай в архив:

```text
.venv/
.runs/
.codex_home/
.codex_temp/
.tmp_tests/
node_modules/
codex_smoke_workspace/
```

`node_modules` может весить много. `.runs` и `.codex_home` могут содержать локальные runtime-данные и логи.

## Установка с нуля: Windows + Git Bash + portable Node

Ниже инструкция для корпоративного ПК, где нельзя менять системный `PATH`.

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

### 2. Создать Python venv

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
Ran 15 tests ... OK
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

Ожидаемо `where node` должен показать примерно:

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

### 5. Важно: не использовать пустой локальный CODEX_HOME

Если раньше задавал `CODEX_HOME`, сбрось его:

```bash
unset CODEX_HOME
```

Иначе Codex может стартовать без авторизации и вернуть:

```text
401 Unauthorized: Missing bearer or basic authentication in header
```

Рабочий сценарий использует уже авторизованный профиль Codex:

```text
C:\Users\Slivin.Aleksandr\.codex
```

## Быстрая проверка: mock backend

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

## Быстрая проверка: прямой Codex CLI smoke-test

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

Ожидаемо оба файла содержат:

```text
ORCHESTRATOR_SMOKE_TEST_OK
```

## End-to-end smoke-test через orchestrator + Codex CLI

```bash
unset CODEX_HOME
NODE_HOME="/c/Users/Slivin.Aleksandr/Tools/node"
export PATH="$NODE_HOME:$PATH"
hash -r

CODEX_CMD="$(pwd -W)/node_modules/.bin/codex.cmd"

./.venv/Scripts/python.exe -m ai_orchestrator.cli \
  "In the isolated Codex executor workspace, create RESULT.md with the title '# Orchestrator Codex smoke test'. The file must contain the exact token ORCHESTRATOR_SMOKE_TEST_OK. In your final response, mention RESULT.md and include the exact token ORCHESTRATOR_SMOKE_TEST_OK." \
  --criteria "ORCHESTRATOR_SMOKE_TEST_OK" \
  --backend codex_cli \
  --codex-cmd "$CODEX_CMD" \
  --max-retries 1
```

Ожидаемо:

```text
status=approved
backend=codex_cli
```

Проверка артефактов:

```bash
RUN=".runs/<run_id>"
cat "$RUN/final_report.md"
cat "$RUN/artifacts/workspace/RESULT.md"
cat "$RUN/artifacts/step_1_attempt_1_codex_log.md"
```

## Structured execution report contract

Для реальных coding-задач лучше не ограничиваться поиском строк в логах. Используй флаг:

```bash
--require-structured-report
```

Тогда executor обязан создать файл:

```text
EXECUTION_REPORT.json
```

в корне isolated workspace.

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

- файл `EXECUTION_REPORT.json` существует, если включен `--require-structured-report`;
- JSON валиден;
- JSON соответствует Pydantic-схеме;
- `report.status == completed`;
- если есть test reports, все `tests[*].status == passed`;
- если задача выглядит как test-задача, `tests` не должен быть пустым;
- `changed_files` и `commands_run` не должны быть пустыми при `--require-structured-report`;
- explicit criteria тоже проверяются;
- если включен `--rerun-report-test-commands`, validator повторно запускает allowlisted test-команды из `tests[*].command` в workspace.

## Structured criteria DSL

Если есть `EXECUTION_REPORT.json`, часть criteria можно проверять по полям JSON, а не по строкам.

Поддерживаются:

```text
report.status=completed
changed_files includes src/toy_calc.py
changed_files:src/toy_calc.py
commands_run includes python -m unittest discover -s tests
commands_run:python -m unittest discover -s tests
tests.status=passed
tests passed
```

Неизвестные criteria проверяются старым способом: как substring в `ExecutionResult.content`.

## Независимый rerun тестов validator-ом

Structured report лучше текстового отчета, но сам по себе он всё еще остается утверждением executor-а. Включи флаг:

```bash
--rerun-report-test-commands
```

Тогда validator после парсинга `EXECUTION_REPORT.json` повторно запускает команды из:

```text
tests[*].command
```

в директории workspace, где лежит `EXECUTION_REPORT.json`.

В MVP intentionally не запускаются все `commands_run`, потому что там могут быть write/setup/delete-команды. Повторяются только test-команды.

Allowlist текущей версии:

```text
python -m unittest ...
python -m pytest ...
pytest ...
```

Команды с shell-control операторами блокируются до запуска:

```text
&&  ||  ;  |  >  <  `  $(...)
```

Python-команды запускаются через тот же interpreter, которым запущен orchestrator: `sys.executable`. Это снижает риск, что validator внезапно использует другой Python из `PATH`.

Можно задать timeout на каждую команду:

```bash
--validation-command-timeout 60
```

## Structured coding-task smoke-test

```bash
unset CODEX_HOME
NODE_HOME="/c/Users/Slivin.Aleksandr/Tools/node"
export PATH="$NODE_HOME:$PATH"
hash -r

CODEX_CMD="$(pwd -W)/node_modules/.bin/codex.cmd"

TASK=$(cat <<'EOF'
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
EOF
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
cat "$RUN/artifacts/step_1_attempt_1_codex_log.md"
```

## Как читать результат

Успешный structured run должен иметь:

```text
final_report.md: Status: approved
final_report.md: Structured report and explicit acceptance criteria passed.
final_report.md: Validator re-ran test command successfully.
step_1_attempt_1_codex_log.md: exit_code: 0
artifacts/workspace/EXECUTION_REPORT.json
artifacts/workspace/src/toy_calc.py
artifacts/workspace/tests/test_toy_calc.py
```

## Troubleshooting

### `ModuleNotFoundError: No module named 'ai_orchestrator'`

Причина: проект не установлен в venv или запускается не тот Python.

Исправление:

```bash
./.venv/Scripts/python.exe -m pip install -e .
./.venv/Scripts/python.exe -m ai_orchestrator.cli "test"
```

### `codex: command not found`

Причина: Codex CLI не установлен или не доступен в текущей shell-сессии.

Исправление:

```bash
NODE_HOME="/c/Users/Slivin.Aleksandr/Tools/node"
export PATH="$NODE_HOME:$PATH"
hash -r
/c/Users/Slivin.Aleksandr/Tools/node/npm.cmd install
CODEX_CMD="$(pwd -W)/node_modules/.bin/codex.cmd"
"$CODEX_CMD" --version
```

### `""node"" не является внутренней или внешней командой`

Причина: `.cmd`-shim Codex пытается вызвать `node`, но portable Node не в текущем `PATH`.

Исправление:

```bash
NODE_HOME="/c/Users/Slivin.Aleksandr/Tools/node"
export PATH="$NODE_HOME:$PATH"
hash -r
cmd //c "where node"
```

### `'.' is not recognized as an internal or external command`

Причина: Windows subprocess плохо переварил путь вида `./node_modules/.bin/codex.cmd`.

Исправление: используй абсолютный Windows path:

```bash
CODEX_CMD="$(pwd -W)/node_modules/.bin/codex.cmd"
```

и передавай:

```bash
--codex-cmd "$CODEX_CMD"
```

### `401 Unauthorized: Missing bearer or basic authentication in header`

Частая причина: задан пустой локальный `CODEX_HOME`.

Исправление:

```bash
unset CODEX_HOME
```

Если хочешь использовать отдельный `CODEX_HOME`, его надо отдельно авторизовать через Codex CLI.

### `sandbox: read-only`

Если в логе Codex виден `sandbox: read-only`, проверь, что backend передал:

```text
--sandbox workspace-write
```

В нормальном успешном логе должно быть:

```text
sandbox: workspace-write
```

### Structured report missing

Если запуск с `--require-structured-report` падает с:

```text
Structured execution report is required, but EXECUTION_REPORT.json was not found.
```

Значит Codex не создал `EXECUTION_REPORT.json`. Уточни task prompt: явно попроси создать `EXECUTION_REPORT.json`, не Markdown.

### Structured report invalid JSON

Если падает с:

```text
EXECUTION_REPORT.json is not valid JSON
```

Значит executor записал Markdown, комментарии, trailing comma или неэкранированные символы. Файл должен быть чистым JSON.

## Что дальше

Следующий архитектурный шаг — усилить проверку еще дальше:

1. сравнить фактические workspace-файлы с `changed_files` из `EXECUTION_REPORT.json`;
2. сохранять stdout/stderr rerun-команд отдельными validator-артефактами;
3. добавить policies для разрешенных путей и типов файлов;
4. добавить режим применения результата в реальный git workspace только после approve;
5. добавить отдельные validators для Python/Airflow/SQL задач.
