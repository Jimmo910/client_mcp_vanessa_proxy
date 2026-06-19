# client_mcp_vanessa_proxy

Stdio-прокси между Claude Code и MCP-сервером `client_mcp.cfe` ([1c-neurofish/onec-client-mcp-devkit](https://github.com/1c-neurofish/onec-client-mcp-devkit)) с **автореконнектом**: переживает рестарты 1С без ручного `/mcp` в Claude Code.

Предназначен для разработки расширений 1С с MCP-управляемой Vanessa Automation. Цикл итерации становится полностью автоматическим: правка кода → `restart-with-new-ext` → готов к следующему вызову MCP без вмешательства пользователя.

Работает с любым MCP Streamable HTTP backend 1С на этом порту — в том числе с **объединённым расширением `client_mcp`** (`onec-client-mcp-devkit` + [`ROCTUP/1c-mcp-toolkit`](https://github.com/ROCTUP/1c-mcp-toolkit), **28 инструментов**: UI-автоматизация + интроспекция запросов/кода/метаданных). Тексты/имена не привязаны к Vanessa и настраиваются через env (`MCP_SERVER_LABEL`, `MCP_START_HINT`).

## Быстрый старт

Сценарий: 1С и ИИ-клиент (Claude Code) работают на одной машине; прокси крутится между ними.

1. Поставить [`uv`](https://github.com/astral-sh/uv) и положить рядом `mcp-proxy-reconnect.py`.
2. Прописать прокси в `.mcp.json` ИИ-клиента как stdio-сервер (см. «Установка» ниже):
   ```json
   "onec": { "type": "stdio", "command": "/путь/к/uv",
             "args": ["run", "--script", "/путь/к/mcp-proxy-reconnect.py"] }
   ```
3. Запустить 1С с MCP-сервером на 9874 (для объединённого расширения
   [mcp-1c-unified](https://github.com/Jimmo910/mcp-1c-unified)):
   `1cv8c "/F<база>" /C"runMcp;mcpPort=9874"` (на Linux — под Xvfb).
4. Готово. ИИ всегда видит MCP «подключённым»: пока 1С жива — вызовы идут в неё; при рестарте 1С
   прокси переподключается сам (без `/mcp`); когда 1С выключена — `tools/list` отдаётся из кэша, а
   на вызов инструмента приходит понятное «запусти 1С: …» вместо обрыва сессии.

Команду старта 1С, которую прокси подсказывает в этом сообщении, задаёт env `MCP_START_HINT`.

## Зачем

При рестарте 1С (например, после `DESIGNER /LoadCfg /UpdateDBCfg` новой версии расширения) `client_mcp.cfe` поднимает HTTP-сервер с новой `Mcp-Session-Id`. Старая сессия мертва — кэшированный в Claude Code session-id получает `HTTP 404 Session not found`. **Claude Code сам по 404 не реконнектится** (см. issue [claude-code#30224](https://github.com/anthropics/claude-code/issues/30224)) — пользователь должен вручную выполнить `/mcp`.

За одну сессию разработки рестарт 1С случается 10+ раз. Каждый ручной `/mcp` прерывает поток работы.

И ещё хуже — если **1С вообще не запущена** при старте Claude Code (типичная ситуация: открыл CC, потом думаю запускать 1С или нет), MCP-сервер `vanessa` отвалится по таймауту 30 сек на каждой попытке коннекта, и потом всё равно потребуется `/mcp` после запуска 1С. Прокси решает и эту проблему — отдаёт CC закэшированный `initialize`/`tools/list` сразу, без обращения к 1С, а реальный коннект делает лениво при первом вызове инструмента.

## Как работает

```
Claude Code  ←─ stdio JSON-RPC ─→  client_mcp_vanessa_proxy  ─── HTTP ──→  client_mcp.cfe (1С + VA)
                                   (живёт всегда)                          (рестартует часто)
```

Прокси регистрируется в `.mcp.json` Claude Code как `stdio` MCP-сервер — stdio-сессия с CC **persistent**, не умирает. Со стороны backend это обычный HTTP MCP-клиент; при смерти backend прокси прозрачно переподключается.

При `HTTP 404` от backend (или `ConnectError` и т.п.) прокси:
1. Сбрасывает протухший session-id.
2. Повторно отправляет закэшированный `initialize` (запомнен от первой инициализации с Claude Code).
3. Отправляет `notifications/initialized`.
4. Дожидается регистрации MCP-инструментов в backend (на случай если VA EPF ещё догружается после рестарта 1С).
5. Повторяет исходный запрос с новой `Mcp-Session-Id`.

Claude Code видит штатный ответ — никаких `/mcp` нажимать не надо.

### Offline-stub (1С не запущена)

Когда 1С при старте Claude Code не запущена и быстрый пробный `initialize` к backend падает с `ConnectError`, прокси:

1. **Сразу** (≈ 0.1 сек) отдаёт CC ответ на `initialize` — либо закэшированный с прошлой удачной сессии (`/tmp/mcp-1c-init-cache.json`), либо минимальный валидный stub.
2. На `tools/list` отдаёт список инструментов из кэша (`/tmp/mcp-1c-tools-cache.json`) — CC видит все инструменты backend (напр. 28 у объединённого devkit + toolkit), как если бы backend был жив.
3. На `tools/call` без живого backend пробует один раз ленивый реконнект; если 1С всё ещё не отвечает — возвращает чёткую JSON-RPC ошибку с инструкцией («запусти 1С: `<MCP_START_HINT>` и повтори вызов, переподключение не нужно»).
4. Как только 1С поднялась — следующий `tools/call` прозрачно создаёт новую сессию через тот же reconnect-путь, без `/mcp`.

Кэш-файлы создаются автоматически после первой успешной сессии. Без них stub отдаёт пустой `tools/list` — после первого запуска 1С с живым CC кэш заполнится.

## Совместимость с `client_mcp.cfe`

Прокси решает **только** проблему сохранения сессии Claude Code между рестартами 1С. **Сам процесс старта MCP внутри 1С — задача `client_mcp.cfe`.** Чтобы цикл «рестарт 1С → MCP готов» был полностью автоматическим, нужно:

1. **`client_mcp.cfe` собран из `master`** (или `v0.7.0+`, когда выйдет) — с [PR #10 dynamic tool registration](https://github.com/1c-neurofish/onec-client-mcp-devkit/pull/10). В релизах `v0.6.x` инструменты Vanessa Automation регистрируются только при ручном клике «Запустить» в форме «Управление MCP» — ни один прокси этого не обойдёт. Способ сборки .cfe из исходников см. в репозитории `onec-client-mcp-devkit` (`.github/workflows/release.yml`: EDT export → `ibcmd config import` → `ibcmd config save`).

2. **1С запускается с параметром `/C "runMcp;mcpPort=9874"`** — `client_mcp.cfe` сам поднимет MCP-сервер на этом порту при `ПриНачалеРаботыСистемы`.

Без этих двух условий прокси будет работать, но запуск VA по-прежнему требует ручных действий.

## Установка

### 1. Зависимости

- Python ≥ 3.11.
- [`uv`](https://github.com/astral-sh/uv) — `httpx` подтягивается автоматически через [PEP 723](https://peps.python.org/pep-0723/) inline metadata, отдельный `pip install` не нужен.

```bash
# macOS
brew install uv

# Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### 2. Скачать скрипт

```bash
git clone https://github.com/Jimmo910/client_mcp_vanessa_proxy.git
# или просто wget файла:
wget https://raw.githubusercontent.com/Jimmo910/client_mcp_vanessa_proxy/main/mcp-proxy-reconnect.py
```

### 3. Запись в `.mcp.json` Claude Code

```json
{
  "mcpServers": {
    "vanessa": {
      "type": "stdio",
      "command": "/Users/you/.local/bin/uv",
      "args": [
        "run", "--script",
        "/абсолютный/путь/к/mcp-proxy-reconnect.py"
      ],
      "timeout": 300
    }
  }
}
```

**Путь к `uv`** — узнать через `which uv`. Указывать абсолютным путём.

После правки `.mcp.json` — перезапустить Claude Code (или выполнить `/mcp` → переподключить `vanessa`).

## Переменные окружения (все опциональные)

**Все переменные имеют разумные дефолты — если у тебя стандартная установка (1С локально, порт 9874, демо-БД ERP с Vanessa Automation), секцию `"env":{}` в `.mcp.json` можно не указывать вообще, всё заработает.**

Переопределять имеет смысл только если ты меняешь стандартные значения (другой порт MCP, другой путь лога, нестандартный backend без 27 VA-инструментов и т.п.).

| Переменная | По умолчанию | Когда нужно переопределять |
|---|---|---|
| `MCP_BACKEND_URL` | `http://localhost:9874/mcp` | Если меняешь `mcpPort` в `/C "runMcp;mcpPort=N"` или backend не на localhost. |
| `MCP_PROXY_LOG` | `/tmp/mcp-proxy-reconnect.log` | Хочешь логи в другом месте. |
| `MCP_PROXY_WAIT_TOOLS_MIN` | `5` | Backend регистрирует меньше инструментов чем 5 (например, у VA их 27 — порог 5 ловит «backend готов»). Если у тебя backend без VA — может потребоваться `1`. |
| `MCP_PROXY_WAIT_TOOLS_TIMEOUT` | `120` | Регистрация tools после рестарта 1С занимает больше 120 сек (большая ERP с длинным «Обновлением ИБ»). |
| `MCP_PROXY_INIT_PROBE` | `3` | Сколько секунд ждать ответа backend при стартовом пробе перед тем как отдать CC offline-stub. Дефолт 3 сек — оптимально для локальной 1С. Поднимать если backend медленный на старте, опускать если хочется быстрее переходить в offline-stub. |
| `MCP_PROXY_INIT_CACHE` | `/tmp/mcp-1c-init-cache.json` | Путь к кэшу последнего успешного ответа `initialize`. Используется при offline-stub. |
| `MCP_PROXY_TOOLS_CACHE` | `/tmp/mcp-1c-tools-cache.json` | Путь к кэшу последнего успешного `tools/list`. Используется при offline-stub. |
| `MCP_SERVER_LABEL` | `1c-mcp` | `serverInfo.name` в offline-stub (когда нет кэша `initialize`). Косметика. |
| `MCP_START_HINT` | `DISPLAY=:99 1cv8c "/F~/dev/mcp-run/file-db" /C"runMcp;mcpPort=9874" …` | Команда старта 1С, которую прокси подсказывает LLM в graceful-ошибке при выключенной 1С. Поставь свою команду запуска backend. |

Если всё-таки нужно переопределить — размещаются в секции `"env":{}` записи `vanessa` в `.mcp.json`:

```json
"vanessa": {
  "type": "stdio",
  "command": "/Users/you/.local/bin/uv",
  "args": ["run", "--script", "/путь/к/mcp-proxy-reconnect.py"],
  "env": {
    "MCP_BACKEND_URL": "http://localhost:9876/mcp",
    "MCP_PROXY_WAIT_TOOLS_TIMEOUT": "300"
  },
  "timeout": 300
}
```

Альтернатива — экспорт в shell, **из которого запущен сам Claude Code** (CC наследует env при старте stdio-серверов):
```bash
export MCP_BACKEND_URL=http://localhost:9876/mcp
claude  # запускаем Claude Code из этого же шелла
```

## Пример запуска 1С для полного цикла

В скрипте старта 1С (например, в моём оркестраторе — `vanessa-bdd-deploy/scripts/run.sh::cmd_start`):

```bash
ONEC=/opt/1cv8/8.3.27.2170/1cv8       # на macOS: тот же путь
DB_PATH=/path/to/file-base
VA_EPF=/path/to/vanessa-automation-single.epf
MCP_PORT=9874

nohup "$ONEC" ENTERPRISE \
    /TestManager \
    "/F$DB_PATH" \
    /N Admin \
    "/CrunMcp;mcpPort=$MCP_PORT" \
    "/Execute$VA_EPF" \
    > /tmp/1c.stdout 2>&1 &
```

Ключевые параметры:
- **`/CrunMcp;mcpPort=9874`** — заставляет `client_mcp.cfe` (`master`) автоматически поднять MCP-сервер на 9874.
- **`/N Admin`** без `/P` — демо-БД ERP не имеет пароля. Если передать `/P ""` — профиль `«Этот клиент»` может «слететь» после `kill TestClient`, и следующий `mcp__vanessa__connect_test_client` вернёт «Профиль не найден».
- **`/TestManager`** — нужен для типа `ТестируемаяГруппаФормы` и шага «Я подключаю TestClient».
- **`/Execute<VA_EPF>`** — загружает Vanessa Automation как внешнюю обработку. С `client_mcp` master она зарегистрирует свои 27 инструментов в MCP динамически (PR #10).

Ожидание готовности (например, через `curl`):

```bash
# Фаза 1: HTTP отвечает
until curl -fs -o /dev/null http://localhost:$MCP_PORT/mcp; do sleep 2; done

# Фаза 2: tools/list вернул >= 5 инструментов
while true; do
    curl -s -X POST http://localhost:$MCP_PORT/mcp \
        -H "Content-Type: application/json" \
        -H "Accept: application/json, text/event-stream" \
        -D /tmp/h.txt -o /dev/null \
        -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"wait","version":"1"}}}'
    SID=$(grep -i "^mcp-session-id:" /tmp/h.txt | sed 's/.*: //' | tr -d '\r\n')

    curl -s -X POST http://localhost:$MCP_PORT/mcp \
        -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
        -H "Mcp-Session-Id: $SID" \
        -d '{"jsonrpc":"2.0","method":"notifications/initialized"}' > /dev/null

    COUNT=$(curl -s -X POST http://localhost:$MCP_PORT/mcp \
        -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
        -H "Mcp-Session-Id: $SID" \
        -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
        | grep -oE '"name":"[^"]+"' | wc -l)

    [ "$COUNT" -ge 5 ] && break
    sleep 2
done
```

После этого можно вызывать `mcp__vanessa__*` из Claude Code — прокси сам подключится к backend, и при следующих рестартах 1С тоже сам реинициализируется.

## Ограничения

- **Server-push notifications проксируются.** Фоновая задача `notification_pump` держит long-poll `GET /mcp` SSE-стрим и пробрасывает server-initiated сообщения (`notifications/tools/list_changed` и т.п.) в Claude Code; на `tools/list_changed` дополнительно обновляется кэш `tools/list`. Пробрасываются только сообщения с `method` (нотификации/серверные запросы) — ответы на запросы приходят по SSE их собственного POST-ответа. Если backend не поддерживает `GET /mcp` (HTTP 405/406) — pump тихо отключается, остальное работает штатно.
- **Состояние backend сбрасывается при рестарте 1С.** Открытый feature, подключённый TestClient, выбранный сценарий — теряются. Прокси прозрачен для Claude Code, поэтому LLM может потребоваться повторно выполнить шаги установки (`open_feature_file`, `connect_test_client`) если что-то выглядит странно.
- Протестировано на **macOS + 1С:Enterprise 8.3.27 + Vanessa Automation 1.2**. Сам прокси платформонезависим, но порог `MCP_PROXY_WAIT_TOOLS_MIN` стоит подтюнить под свой backend.

## Почему не `sparfenyuk/mcp-proxy`?

[`sparfenyuk/mcp-proxy`](https://github.com/sparfenyuk/mcp-proxy) — отличный мост общего назначения, но **не обрабатывает `HTTP 404 Session not found`** через re-initialize — он просто пробрасывает ошибку клиенту. После рестарта backend возвращает `Session terminated` пока сам не будет перезапущен. Этот прокси специально решает этот разрыв.

## Лицензия

MIT.

## История

Сделан в ходе разработки расширения 1С:ERP. Зафиксирован как issue в апстриме `client_mcp`: [#11](https://github.com/1c-neurofish/onec-client-mcp-devkit/issues/11). В `master` `client_mcp` уже исправлена связанная проблема [PR #10 dynamic tool registration](https://github.com/1c-neurofish/onec-client-mcp-devkit/pull/10) (мерж 2026-05-01) — при выходе релиза с этим фиксом отпадёт необходимость в `wait-for-tools` при первом старте, но прокси по-прежнему полезен для переживания рестартов 1С.
