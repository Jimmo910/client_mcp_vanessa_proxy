# client_mcp_vanessa_proxy

Stdio-прокси между Claude Code и MCP-сервером `client_mcp.cfe` ([1c-neurofish/onec-client-mcp-devkit](https://github.com/1c-neurofish/onec-client-mcp-devkit)) с **автореконнектом**: переживает рестарты 1С без ручного `/mcp` в Claude Code.

Предназначен для разработки расширений 1С с MCP-управляемой Vanessa Automation. Цикл итерации становится полностью автоматическим: правка кода → `restart-with-new-ext` → готов к следующему вызову MCP без вмешательства пользователя.

## Зачем

При рестарте 1С (например, после `DESIGNER /LoadCfg /UpdateDBCfg` новой версии расширения) `client_mcp.cfe` поднимает HTTP-сервер с новой `Mcp-Session-Id`. Старая сессия мертва — кэшированный в Claude Code session-id получает `HTTP 404 Session not found`. **Claude Code сам по 404 не реконнектится** (см. issue [claude-code#30224](https://github.com/anthropics/claude-code/issues/30224)) — пользователь должен вручную выполнить `/mcp`.

За одну сессию разработки рестарт 1С случается 10+ раз. Каждый ручной `/mcp` прерывает поток работы.

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

## Переменные окружения

Размещаются в секции `"env":{}` записи `vanessa` в `.mcp.json`:

```json
"vanessa": {
  "type": "stdio",
  "command": "/Users/you/.local/bin/uv",
  "args": ["run", "--script", "/путь/к/mcp-proxy-reconnect.py"],
  "env": {
    "MCP_BACKEND_URL": "http://localhost:9874/mcp",
    "MCP_PROXY_LOG": "/tmp/mcp-proxy-reconnect.log",
    "MCP_PROXY_WAIT_TOOLS_MIN": "5",
    "MCP_PROXY_WAIT_TOOLS_TIMEOUT": "120"
  },
  "timeout": 300
}
```

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `MCP_BACKEND_URL` | `http://localhost:9874/mcp` | Адрес HTTP-сервера client_mcp в 1С. Должен совпадать с `mcpPort` в параметре `/C "runMcp"`. |
| `MCP_PROXY_LOG` | `/tmp/mcp-proxy-reconnect.log` | Файл лога (события реконнекта, ошибки). |
| `MCP_PROXY_WAIT_TOOLS_MIN` | `5` | Минимальное число инструментов в `tools/list` после реконнекта — прокси дожидается этого порога прежде чем повторить исходный запрос. У VA регистрируется 27 инструментов, поэтому 5 — достаточный порог «backend готов». |
| `MCP_PROXY_WAIT_TOOLS_TIMEOUT` | `120` | Сколько секунд максимум ждать регистрации инструментов после реконнекта. На свежей БД ERP с `Обновление ИБ` может потребоваться 60+ секунд. |

Если задавать только через shell (без `.mcp.json`) — переменные нужно экспортировать в окружении, **из которого запущен сам Claude Code**, потому что Claude Code наследует env при старте stdio-серверов:
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

- **Server-push notifications не проксируются.** `GET /mcp` SSE-стрим от backend для `notifications/tools/list_changed` и т.п. — **не пробрасывается** в Claude Code. Для нашего use case (Vanessa регистрирует все инструменты при старте, без рантайм-добавлений) это не критично. PR welcome.
- **Состояние backend сбрасывается при рестарте 1С.** Открытый feature, подключённый TestClient, выбранный сценарий — теряются. Прокси прозрачен для Claude Code, поэтому LLM может потребоваться повторно выполнить шаги установки (`open_feature_file`, `connect_test_client`) если что-то выглядит странно.
- Протестировано на **macOS + 1С:Enterprise 8.3.27 + Vanessa Automation 1.2**. Сам прокси платформонезависим, но порог `MCP_PROXY_WAIT_TOOLS_MIN` стоит подтюнить под свой backend.

## Почему не `sparfenyuk/mcp-proxy`?

[`sparfenyuk/mcp-proxy`](https://github.com/sparfenyuk/mcp-proxy) — отличный мост общего назначения, но **не обрабатывает `HTTP 404 Session not found`** через re-initialize — он просто пробрасывает ошибку клиенту. После рестарта backend возвращает `Session terminated` пока сам не будет перезапущен. Этот прокси специально решает этот разрыв.

## Лицензия

MIT.

## История

Сделан в ходе разработки расширения 1С:ERP. Зафиксирован как issue в апстриме `client_mcp`: [#11](https://github.com/1c-neurofish/onec-client-mcp-devkit/issues/11). В `master` `client_mcp` уже исправлена связанная проблема [PR #10 dynamic tool registration](https://github.com/1c-neurofish/onec-client-mcp-devkit/pull/10) (мерж 2026-05-01) — при выходе релиза с этим фиксом отпадёт необходимость в `wait-for-tools` при первом старте, но прокси по-прежнему полезен для переживания рестартов 1С.
