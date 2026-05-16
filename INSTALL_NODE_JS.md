`npm` обычно не ставят отдельно: ставят **Node.js**, а вместе с ним получают `node`, `npm` и обычно `npx`. Официальная npm-документация прямо говорит: чтобы использовать npm CLI, нужно установить Node.js и npm через Node installer или version manager. ([Документация npm][1])

## Где может быть реальная проблема

1. **Корпоративная политика.**
   Установка Node.js может быть запрещена без согласования с ИБ/IT. Это главный риск, не технический.

2. **Права администратора.**
   MSI-инсталлятор Node.js может требовать admin rights, особенно если ставит в `Program Files` и меняет системный `PATH`.

3. **PATH.**
   Тебе уже нельзя менять `PATH`. Это решаемо: можно использовать portable/локальную установку и вызывать `node.exe`, `npm.cmd`, `npx.cmd` по полному пути.

4. **Прокси/сертификаты.**
   `npm install @openai/codex` скачивает пакет из npm registry. В корпоративной сети это может упереться в proxy, SSL inspection или запрет внешних registry. Сам npm используется для установки зависимостей проекта. ([Node.js][2])

5. **Security review.**
   Установка Codex CLI через npm — официальный путь OpenAI: `npm i -g @openai/codex`. ([OpenAI Разработчики][3]) Но для корпоративной среды лучше не ставить глобально, а поставить локально в папку проекта.

## Мой совет

Ставить можно, но **не глобально** и **без изменения PATH**.

Самый безопасный вариант для твоего ограничения:

```text
portable Node.js в папку пользователя
↓
локальный npm install внутри ai_orchestrator_mvp
↓
запуск Codex через ./node_modules/.bin/codex
```

## Вариант A — если IT разрешает обычную установку

Ставишь Node.js LTS с официального сайта Node.js. На странице загрузки сейчас есть LTS-версия и Windows installer / standalone binary варианты. ([Node.js][4])

После установки проверяешь:

```bash
node --version
npm --version
npx --version
```

Если команды доступны — дальше:

```bash
cd ~/Documents/ai_orchestrator_mvp
npm install --save-dev @openai/codex
./node_modules/.bin/codex --version
```

## Вариант B — без admin rights и без PATH

Это лучше для твоей ситуации.

1. Скачай **Windows Binary / ZIP** Node.js LTS с официальной страницы Node.js, не MSI-installer. ([Node.js][4])

2. Распакуй, например, сюда:

```text
C:\Users\Slivin.Aleksandr\Tools\node
```

3. В Git Bash проверь через полный путь:

```bash
/c/Users/Slivin.Aleksandr/Tools/node/node.exe --version
/c/Users/Slivin.Aleksandr/Tools/node/npm.cmd --version
/c/Users/Slivin.Aleksandr/Tools/node/npx.cmd --version
```

Путь поправь под реальное имя распакованной папки.

4. В проекте установи Codex CLI локально:

```bash
cd ~/Documents/ai_orchestrator_mvp

/c/Users/Slivin.Aleksandr/Tools/node/npm.cmd install --save-dev @openai/codex
```

5. Проверь локальный Codex:

```bash
./node_modules/.bin/codex.cmd --version
```

Если `.cmd` из Git Bash не запустится, проверь так:

```bash
cmd //c node_modules\\.bin\\codex.cmd --version
```

## Затем запуск orchestrator

```bash
python -m ai_orchestrator.cli \
  "In the isolated Codex executor workspace, create RESULT.md with the title '# Orchestrator Codex smoke test'. The file must contain the exact token ORCHESTRATOR_SMOKE_TEST_OK. In your final response, mention RESULT.md and include the exact token ORCHESTRATOR_SMOKE_TEST_OK." \
  --criteria "ORCHESTRATOR_SMOKE_TEST_OK" \
  --backend codex_cli \
  --codex-cmd "./node_modules/.bin/codex.cmd" \
  --max-retries 1
```

Если не сработает из-за `.cmd`, пробуем так:

```bash
python -m ai_orchestrator.cli \
  "In the isolated Codex executor workspace, create RESULT.md with the title '# Orchestrator Codex smoke test'. The file must contain the exact token ORCHESTRATOR_SMOKE_TEST_OK. In your final response, mention RESULT.md and include the exact token ORCHESTRATOR_SMOKE_TEST_OK." \
  --criteria "ORCHESTRATOR_SMOKE_TEST_OK" \
  --backend codex_cli \
  --codex-cmd "cmd /c node_modules\\.bin\\codex.cmd" \
  --max-retries 1
```