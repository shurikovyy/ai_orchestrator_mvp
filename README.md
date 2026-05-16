# AI Orchestrator MVP 0.1.2

Минимальный deterministic workflow-инструмент для цикла:

```text
user task → planner → executor → validator → retry/rework → final_report
```

Цель MVP — проверить не “агентов ради агентов”, а надежный контур исполнения задачи:

```text
AI Orchestrator
→ backend
→ isolated workspace
→ artifacts/logs/state
→ validation
→ approved/failed report
```

Сейчас поддерживаются два backend-а:

- `mock` — полностью offline, без внешних API;
- `codex_cli` — реальное исполнение через OpenAI Codex CLI.

> Важно: CrewAI на этом этапе сознательно не подключается. Сначала должен быть надежный deterministic core: state, retry policy, artifacts, validation. CrewAI можно добавлять позже как внешний orchestration/facade layer.

---

## Проверенная рабочая среда

Инструкция ниже ориентирована на конфигурацию, на которой уже был получен успешный результат:

```text
OS: Windows
Shell: Git Bash / MINGW64
Python: 3.13 через venv
Node.js: portable install в C:\Users\Slivin.Aleksandr\Tools\node
npm: из portable Node.js
Codex CLI: локально в node_modules через package.json
Codex auth: default-профиль пользователя, не локальный пустой CODEX_HOME
```

Финальный критерий успеха:

```text
status=approved
backend=codex_cli
```

---

## Что не коммитить в репозиторий

В репозитории не должны лежать runtime-директории:

```text
.venv/
node_modules/
.runs/
.codex_home/
.codex_temp/
codex_smoke_workspace/
__pycache__/
*.pyc
```

`node_modules` может быть очень тяжелым. Его нужно восстанавливать командой:

```bash
npm install
```

---

## 1. Клонирование репозитория

Замените `<REPO_URL>` на реальный URL репозитория.

```bash
cd ~/Documents
git clone <REPO_URL> ai_orchestrator_mvp_0_1_2
cd ai_orchestrator_mvp_0_1_2
```

Если проект получен архивом, просто перейдите в папку, где лежит `pyproject.toml`:

```bash
cd ~/Documents/ai_orchestrator_mvp_0_1_2
```

Проверка, что вы в корне проекта:

```bash
ls pyproject.toml
ls src/ai_orchestrator/cli.py
ls package.json
```

Ожидаемо: все три файла существуют.

---

## 2. Создание Python venv и установка проекта

Создать venv:

```bash
python -m venv .venv
```

Дальше лучше использовать явный Python из venv, а не просто `python`. Это убирает риск, что запустится системный/AppStore Python.

```bash
./.venv/Scripts/python.exe -m pip install --upgrade pip
./.venv/Scripts/python.exe -m pip install -e .
```

Проверить, что пакет доступен:

```bash
./.venv/Scripts/python.exe -c "import ai_orchestrator; print(ai_orchestrator.__version__)"
```

Ожидаемо:

```text
0.1.2
```

---

## 3. Настройка portable Node.js без изменения системного PATH

Если на корпоративном ПК нельзя менять системный `PATH`, используйте временный `PATH` только для текущей Git Bash-сессии.

Для текущей машины рабочий путь был такой:

```bash
NODE_HOME="/c/Users/Slivin.Aleksandr/Tools/node"
export PATH="$NODE_HOME:$PATH"
hash -r
```

Если у вас другой пользователь или другая папка, замените путь:

```bash
NODE_HOME="/c/Users/<YOUR_USER>/Tools/node"
export PATH="$NODE_HOME:$PATH"
hash -r
```

Проверка:

```bash
ls "$NODE_HOME/node.exe" "$NODE_HOME/npm.cmd"
node --version
npm --version
cmd //c "where node"
cmd //c "node --version"
```

Ожидаемо:

```text
.../Tools/node/node.exe
.../Tools/node/npm.cmd
v...
...
C:\Users\...\Tools\node\node.exe
v...
```

Если `node` не найден, значит временный `PATH` не применился в текущей shell-сессии.

---

## 4. Установка локального Codex CLI через npm

В проекте есть `package.json` с зависимостью `@openai/codex`. Установите npm-зависимости локально в проект:

```bash
npm install
```

Или через полный путь к portable npm:

```bash
/c/Users/Slivin.Aleksandr/Tools/node/npm.cmd install
```

После установки должен появиться `node_modules/`.

Проверка локального Codex CLI:

```bash
CODEX_CMD="$(pwd -W)/node_modules/.bin/codex.cmd"
"$CODEX_CMD" --version
```

Ожидаемо:

```text
OpenAI Codex v...
```

Если видите ошибку вида:

```text
""node"" не является внутренней или внешней командой
```

значит `node.exe` не находится через текущий `PATH`. Повторите шаг 3:

```bash
NODE_HOME="/c/Users/Slivin.Aleksandr/Tools/node"
export PATH="$NODE_HOME:$PATH"
hash -r
```

---

## 5. Авторизация Codex CLI

Для успешного `codex_cli` backend Codex CLI должен быть авторизован.

Сначала не задавайте локальный `CODEX_HOME`:

```bash
unset CODEX_HOME
```

Проверьте статус авторизации:

```bash
CODEX_CMD="$(pwd -W)/node_modules/.bin/codex.cmd"
"$CODEX_CMD" login status
```

Если Codex уже авторизован, команда должна завершиться успешно и показать активный auth mode.

Если не авторизован, выполните интерактивный login:

```bash
"$CODEX_CMD" login
```

Если браузерный login неудобен, можно использовать device auth:

```bash
"$CODEX_CMD" login --device-auth
```

Если используется API key, официальный способ — передать ключ через stdin:

```bash
printenv OPENAI_API_KEY | "$CODEX_CMD" login --with-api-key
```

После login снова проверьте:

```bash
"$CODEX_CMD" login status
```

### Важное замечание про CODEX_HOME

Не используйте пустой локальный `CODEX_HOME`, если вы заранее не логинились именно в него.

Плохой вариант для первого запуска:

```bash
mkdir -p .codex_home
export CODEX_HOME="$(pwd -W)/.codex_home"
```

Такой профиль будет пустым. При запуске Codex может упасть с ошибкой:

```text
401 Unauthorized: Missing bearer or basic authentication in header
```

Рабочий вариант для первого успешного запуска:

```bash
unset CODEX_HOME
```

Так Codex использует default-профиль пользователя, например:

```text
C:\Users\<USER>\.codex
```

---

## 6. Запуск unit-тестов

```bash
./.venv/Scripts/python.exe -m unittest discover -s tests
```

Ожидаемо:

```text
........
----------------------------------------------------------------------
Ran 8 tests in ...s

OK
```

Если тесты не проходят, не переходите к Codex smoke-test. Сначала исправьте Python-часть.

---

## 7. Offline smoke-test через mock backend

Этот тест не требует Node.js, npm, Codex CLI и доступа к OpenAI API.

```bash
./.venv/Scripts/python.exe -m ai_orchestrator.cli \
  "Create a short architecture note for an AI task orchestrator" \
  --criteria "has title" \
  --criteria "mentions validation loop" \
  --max-retries 2
```

Ожидаемый вывод:

```text
run_id=run_...
status=approved
backend=mock
final_report=.runs\run_...\final_report.md
state=.runs\run_...\state.json
```

Проверить последний run:

```bash
RUN="$(ls -td .runs/run_* | head -n 1)"
cat "$RUN/final_report.md"
cat "$RUN/state.json"
find "$RUN/artifacts" -maxdepth 4 -type f -print
```

---

## 8. Прямой smoke-test Codex CLI без orchestrator

Этот шаг проверяет отдельно Codex CLI, без Python orchestrator.

```bash
unset CODEX_HOME

NODE_HOME="/c/Users/Slivin.Aleksandr/Tools/node"
export PATH="$NODE_HOME:$PATH"
hash -r

CODEX_CMD="$(pwd -W)/node_modules/.bin/codex.cmd"

rm -rf codex_smoke_workspace codex_final.md
mkdir -p codex_smoke_workspace

"$CODEX_CMD" exec \
  --cd codex_smoke_workspace \
  --sandbox workspace-write \
  --output-last-message codex_final.md \
  --skip-git-repo-check \
  "Create a file RESULT.md with the title '# Codex CLI smoke test'. The file must contain the exact token ORCHESTRATOR_SMOKE_TEST_OK. In your final response, include the exact token ORCHESTRATOR_SMOKE_TEST_OK."
```

Проверка результата:

```bash
cat codex_smoke_workspace/RESULT.md
cat codex_final.md
```

Ожидаемо в обоих результатах есть:

```text
ORCHESTRATOR_SMOKE_TEST_OK
```

Если этот шаг не проходит, проблема не в orchestrator. Сначала исправьте Codex CLI: auth, network, PATH, sandbox, proxy/firewall.

---

## 9. End-to-end smoke-test через orchestrator + codex_cli

Это главный тест. Он должен завершиться `status=approved`.

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

Ожидаемый вывод:

```text
run_id=run_...
status=approved
backend=codex_cli
final_report=.runs\run_...\final_report.md
state=.runs\run_...\state.json
```

Проверка последнего run:

```bash
RUN="$(ls -td .runs/run_* | head -n 1)"

cat "$RUN/final_report.md"
cat "$RUN/artifacts/workspace/RESULT.md"
cat "$RUN/artifacts/step_1_attempt_1_codex_final.md"
cat "$RUN/artifacts/step_1_attempt_1_codex_log.md"
```

В `final_report.md` должно быть:

```text
Status: `approved`
Backend: `codex_cli`
attempt=1, approved=True, score=1.00
```

В `RESULT.md` должно быть:

```text
# Orchestrator Codex smoke test

ORCHESTRATOR_SMOKE_TEST_OK
```

В `step_1_attempt_1_codex_log.md` должно быть:

```text
exit_code: 0
sandbox: workspace-write
## workspace files
### RESULT.md
```

---

## 10. Короткая команда для повторного успешного запуска

Когда проект уже установлен, npm-зависимости есть, Codex авторизован, можно использовать короткий блок:

```bash
cd ~/Documents/ai_orchestrator_mvp_0_1_2

unset CODEX_HOME

NODE_HOME="/c/Users/Slivin.Aleksandr/Tools/node"
export PATH="$NODE_HOME:$PATH"
hash -r

CODEX_CMD="$(pwd -W)/node_modules/.bin/codex.cmd"

./.venv/Scripts/python.exe -m unittest discover -s tests && \
./.venv/Scripts/python.exe -m ai_orchestrator.cli \
  "In the isolated Codex executor workspace, create RESULT.md with the title '# Orchestrator Codex smoke test'. The file must contain the exact token ORCHESTRATOR_SMOKE_TEST_OK. In your final response, mention RESULT.md and include the exact token ORCHESTRATOR_SMOKE_TEST_OK." \
  --criteria "ORCHESTRATOR_SMOKE_TEST_OK" \
  --backend codex_cli \
  --codex-cmd "$CODEX_CMD" \
  --max-retries 1
```

---

## Troubleshooting

### 1. `ModuleNotFoundError: No module named 'ai_orchestrator'`

Причина: проект не установлен в venv или используется не тот Python.

Проверьте:

```bash
ls pyproject.toml
./.venv/Scripts/python.exe -m pip install -e .
./.venv/Scripts/python.exe -m ai_orchestrator.cli --help
```

Не полагайтесь на просто `python`, если venv не активирован.

---

### 2. `bash: codex: command not found`

Причина: глобальный `codex` не установлен или не доступен в `PATH`.

Решение: использовать локальный Codex CLI:

```bash
npm install
CODEX_CMD="$(pwd -W)/node_modules/.bin/codex.cmd"
"$CODEX_CMD" --version
```

---

### 3. `""node"" не является внутренней или внешней командой`

Причина: `codex.cmd` внутри вызывает `node`, но portable Node.js не находится через `PATH`.

Решение:

```bash
NODE_HOME="/c/Users/Slivin.Aleksandr/Tools/node"
export PATH="$NODE_HOME:$PATH"
hash -r

node --version
cmd //c "where node"
```

---

### 4. `'.' is not recognized as an internal or external command`

Причина: Windows `.cmd` через Python subprocess плохо работает с `./node_modules/.bin/codex.cmd`.

Плохой вариант для `--codex-cmd`:

```bash
--codex-cmd "./node_modules/.bin/codex.cmd"
```

Рабочий вариант:

```bash
CODEX_CMD="$(pwd -W)/node_modules/.bin/codex.cmd"
--codex-cmd "$CODEX_CMD"
```

---

### 5. `401 Unauthorized: Missing bearer or basic authentication in header`

Причина: Codex CLI не авторизован в текущем `CODEX_HOME`.

Частый сценарий: вы сделали так:

```bash
export CODEX_HOME="$(pwd -W)/.codex_home"
```

но в этой директории нет credentials.

Решение для первого успешного запуска:

```bash
unset CODEX_HOME
"$CODEX_CMD" login status
```

Если не залогинено:

```bash
"$CODEX_CMD" login
```

или:

```bash
"$CODEX_CMD" login --device-auth
```

---

### 6. `status=failed`, но `score=1.00`

Это означает: acceptance criterion найден, но executor завершился неуспешно.

Проверить:

```bash
RUN="$(ls -td .runs/run_* | head -n 1)"
cat "$RUN/final_report.md"
cat "$RUN/artifacts/step_1_attempt_1_codex_log.md"
```

Смотрите `exit_code` и `stderr`.

Если `exit_code: 1`, проблема обычно в Codex CLI/auth/network/sandbox, а не в validator.

---

### 7. `Missing criterion: ORCHESTRATOR_SMOKE_TEST_OK`

Причина: validator не видит нужный критерий в `ExecutionResult.content`.

В версии `0.1.2` backend должен добавлять workspace-файлы в лог под секцию:

```text
## workspace files
```

Проверьте:

```bash
RUN="$(ls -td .runs/run_* | head -n 1)"
cat "$RUN/artifacts/step_1_attempt_1_codex_log.md"
find "$RUN/artifacts/workspace" -maxdepth 4 -type f -print
```

Если `RESULT.md` создан, но не попал в `workspace files`, значит надо проверять `src/ai_orchestrator/backends/codex_cli.py` и список разрешенных расширений.

---

### 8. `sandbox: read-only` в Codex log

Для успешного smoke-test ожидается:

```text
sandbox: workspace-write
```

Если видите `read-only`, проверьте полный лог и то, какие аргументы реально получил Codex. Backend должен запускать Codex с аргументом:

```text
--sandbox workspace-write
```

---

## Как устроен MVP

```text
src/ai_orchestrator/
  schemas.py              # Pydantic contracts/state/result models
  engine.py               # deterministic workflow engine
  cli.py                  # command line entrypoint
  backends/
    base.py               # backend protocol
    mock.py               # offline demo backend
    codex_cli.py          # Codex CLI executor adapter
  prompts/
    codex_executor.md
    validator.md

tests/
  test_engine.py
  test_codex_cli_backend.py
```

Основной поток:

```text
TaskSpec
→ TaskExecutionEngine.run(...)
→ backend.plan(...)
→ backend.execute_step(...)
→ backend.validate(...)
→ RunState/state.json + final_report.md
```

`codex_cli` backend:

1. создает isolated workspace внутри `.runs/<run_id>/artifacts/workspace`;
2. запускает `codex exec`;
3. передает prompt через stdin, чтобы избежать Windows `.cmd`/newline quoting bugs;
4. сохраняет stdout/stderr/final message;
5. собирает текстовые файлы workspace (`.md`, `.py`, `.json`, `.sql`, ...);
6. добавляет их в `ExecutionResult.content`;
7. validator проверяет acceptance criteria по собранному content.

---

## Безопасные правила работы

1. Не запускайте первые реальные тесты в боевом Airflow repo.
2. Не используйте `danger-full-access`.
3. Не используйте `--dangerously-bypass-approvals-and-sandbox`.
4. Для smoke-test используйте только isolated workspace внутри `.runs`.
5. Не коммитьте `.runs`, `.codex_home`, `node_modules`, `.venv`.
6. Если хотите изолированный `CODEX_HOME`, сначала отдельно выполните login в него.
7. Любой `status=failed` диагностируйте через `final_report.md` и `step_*_codex_log.md`, а не по последней строке CLI.

---

## Официальные ссылки

- OpenAI Codex CLI reference: https://developers.openai.com/codex/cli/reference
- OpenAI Codex CLI overview: https://developers.openai.com/codex/cli
- npm: downloading and installing Node.js and npm: https://docs.npmjs.com/downloading-and-installing-node-js-and-npm/
- Node.js downloads: https://nodejs.org/en/download

---

## Следующий этап после успешного smoke-test

Текущий validator уже доказал end-to-end контур, но для реальных задач он слишком простой: он проверяет наличие текстовых criteria.

Следующий правильный шаг — добавить structured execution report:

```json
{
  "status": "completed",
  "changed_files": [],
  "commands_run": [],
  "tests": [],
  "risks": [],
  "summary": ""
}
```

После этого orchestrator должен валидировать не только наличие строк, а:

```text
exit_code
+ structured JSON report
+ changed files whitelist
+ test results
+ explicit risks
```

Только после этого есть смысл пробовать маленькую реальную задачу на копии репозитория.
