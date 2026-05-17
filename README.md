# mcp-proxy-reconnect

Persistent stdio MCP proxy with **auto-reconnect on backend restart**. Designed for [client_mcp](https://github.com/1c-neurofish/onec-client-mcp-devkit) (1C:Enterprise + Vanessa Automation), but works with any Streamable-HTTP MCP backend that uses session IDs.

## Why

When 1C restarts (e.g. after `LoadCfg /UpdateDBCfg` of a new extension version), the `client_mcp.cfe` HTTP server gets a fresh `Mcp-Session-Id`. The old session is gone — Claude Code's cached session ID now gets `HTTP 404 Session not found`. **Claude Code does not auto-recover from 404** (see [issue claude-code#30224](https://github.com/anthropics/claude-code/issues/30224)) — the user has to manually run `/mcp` to re-initialize.

Over a single development session this happens 10+ times. Each `/mcp` interrupts the conversation flow.

## How it works

```
Claude Code  ←─ stdio JSON-RPC ─→  mcp-proxy-reconnect.py  ─── HTTP ──→  client_mcp.cfe
                                   (persistent)                          (restarts often)
```

The proxy is registered in `.mcp.json` as a `stdio` server — its stdio session with Claude Code is **persistent** and never dies. To the backend it's an ordinary HTTP MCP client; when the backend dies, the proxy reconnects.

When `HTTP 404` comes back from the backend (or `ConnectError`, etc.), the proxy:
1. Drops the stale session ID.
2. Re-sends the cached `initialize` request (cached from the first Claude Code init).
3. Sends `notifications/initialized`.
4. Waits for tools registration (in case the backend just started and tools haven't been registered yet).
5. Retries the original request with the new session ID.

Claude Code sees a normal response — no `/mcp` needed.

## Install

### As stdio MCP server in `.mcp.json`

```json
{
  "mcpServers": {
    "vanessa": {
      "type": "stdio",
      "command": "/Users/you/.local/bin/uv",
      "args": [
        "run", "--script",
        "/path/to/mcp-proxy-reconnect.py"
      ],
      "timeout": 300
    }
  }
}
```

The script uses [PEP 723 inline metadata](https://peps.python.org/pep-0723/) — `uv` auto-installs `httpx` on first run; no separate `pip install` needed.

### Backend URL

Default: `http://localhost:9874/mcp`. Override via env:

```json
"env": {
  "MCP_BACKEND_URL": "http://localhost:9876/mcp"
}
```

## Requirements

- Python 3.11+ (uses `asyncio.StreamReader`).
- `uv` (or any way to run `httpx>=0.27`).
- An MCP backend speaking Streamable HTTP with `Mcp-Session-Id` header (this is the standard).

## Configuration via env

| Variable | Default | Meaning |
|---|---|---|
| `MCP_BACKEND_URL` | `http://localhost:9874/mcp` | Backend Streamable HTTP endpoint. |
| `MCP_PROXY_LOG` | `/tmp/mcp-proxy-reconnect.log` | Log file (reconnect events, errors). |
| `MCP_PROXY_WAIT_TOOLS_MIN` | `5` | Minimum tools count expected after reconnect (waits up to `WAIT_TOOLS_TIMEOUT`). |
| `MCP_PROXY_WAIT_TOOLS_TIMEOUT` | `120` | Seconds to wait for tools to register after a reconnect. |

## Limitations

- **No server-push notifications proxying yet.** The backend `GET /mcp` SSE stream for server-initiated notifications (`notifications/tools/list_changed` etc.) is **not forwarded** to Claude Code. For our use case (Vanessa Automation registers all tools at startup, no runtime additions) this is fine. PRs welcome.
- **Stateful backend state is reset on backend restart.** Open feature, connected TestClient, selected scenario — all gone after a backend restart. The proxy is transparent to Claude Code, so the LLM may need to re-issue setup steps (`open_feature_file`, `connect_test_client`) when something looks off.
- Tested on macOS + 1C:Enterprise + Vanessa Automation. The proxy itself is platform-neutral, but `tools/list` wait threshold (`MCP_PROXY_WAIT_TOOLS_MIN`) should be tuned per backend.

## Why a separate proxy and not `sparfenyuk/mcp-proxy`?

`sparfenyuk/mcp-proxy` is a great general-purpose bridge but **does not handle `HTTP 404 Session not found`** by re-initializing — it just propagates the error to the client. After backend restart it returns `Session terminated` until restarted itself. This proxy specifically addresses that gap.

## License

MIT.

## Origin

Built during a 1C ERP extension development session ([task 19165](https://b24.office.partner-its.ru/company/personal/user/157/tasks/task/view/19165/), private). Filed as upstream issue in client_mcp: [#11](https://github.com/1c-neurofish/onec-client-mcp-devkit/issues/11). Note that **client_mcp `master` already fixes the tool-registration race via [PR #10 dynamic tool registration](https://github.com/1c-neurofish/onec-client-mcp-devkit/pull/10)** (merged 2026-05-01) — once that ships in a release, the proxy is still useful for survival across 1C restarts, but the wait-for-tools dance is no longer needed at first launch.
