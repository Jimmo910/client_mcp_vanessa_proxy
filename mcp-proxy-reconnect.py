#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "httpx>=0.27",
# ]
# ///
"""Persistent MCP proxy with auto-reconnect for the unified 1С MCP (client_mcp.cfe).

Sits between Claude Code (stdio JSON-RPC) and the 1С MCP server (HTTP Streamable
on http://localhost:9874/mcp), which is started inside a 1С thin client with
/C"runMcp;mcpPort=9874". When 1С restarts, the HTTP backend gets a fresh
session-id; this proxy detects HTTP 404 / ConnectionError and silently
re-initializes the backend session, retrying the original request. Claude Code's
stdio session stays alive across all 1С restarts — no /mcp reconnect needed.

When 1С is fully down, the proxy never crashes: it replays the cached initialize
and tools/list (so the toolset stays visible) and answers tool calls with a
graceful "1С not running, start it" error instead of dropping the session.

Notes:
- Server-push notifications (GET /mcp SSE stream) are forwarded by a background
  task (notification_pump): server-initiated messages such as
  notifications/tools/list_changed reach Claude Code, and the tools cache is
  refreshed when the toolset changes.
- After re-init the backend state is reset (open form / connected client state
  forgotten). The LLM should re-issue setup steps if it sees unexpected state.

Usage in .mcp.json:
    "onec": {
        "type": "stdio",
        "command": "uv",
        "args": ["run", "--script",
                 "/home/artem/tools/client_mcp_vanessa_proxy/mcp-proxy-reconnect.py"]
    }

Env (all optional, sane defaults):
- MCP_BACKEND_URL   backend MCP endpoint (default http://localhost:9874/mcp)
- MCP_PROXY_LOG     log file (default /tmp/mcp-proxy-reconnect.log)
- MCP_SERVER_LABEL  serverInfo.name used in the offline stub (default 1c-mcp)
- MCP_START_HINT    shell command shown to the LLM when 1С is down, to start it
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from typing import Any

import httpx

BACKEND_URL = os.environ.get("MCP_BACKEND_URL", "http://localhost:9874/mcp")
LOG_FILE = os.environ.get("MCP_PROXY_LOG", "/tmp/mcp-proxy-reconnect.log")

# serverInfo.name reported to CC in the offline stub, and the shell command shown
# to the LLM when 1С is down (how to bring the unified MCP back up).
SERVER_LABEL = os.environ.get("MCP_SERVER_LABEL", "1c-mcp")
START_HINT = os.environ.get(
    "MCP_START_HINT",
    'DISPLAY=:99 1cv8c "/F~/dev/mcp-run/file-db" /C"runMcp;mcpPort=9874" '
    "/DisableStartupDialogs &",
)

# Disk caches of the last successful initialize / tools/list responses.
# Replayed (with id substituted) to Claude Code when 1C is not running, so
# the MCP connection always looks alive from CC's side — no /mcp reconnect needed.
INIT_CACHE_FILE = os.environ.get(
    "MCP_PROXY_INIT_CACHE", "/tmp/mcp-1c-init-cache.json"
)
TOOLS_CACHE_FILE = os.environ.get(
    "MCP_PROXY_TOOLS_CACHE", "/tmp/mcp-1c-tools-cache.json"
)

# Quick probe on initialize: if backend doesn't answer fast — respond with stub
# right away, defer real connect until user actually calls a tool.
INIT_PROBE_TIMEOUT_SEC = float(os.environ.get("MCP_PROXY_INIT_PROBE", "3"))

RECONNECT_RETRIES = 4
RECONNECT_DELAYS = [1, 2, 4, 8]  # seconds — total ~15s worst case

# Right after initialize the backend may expose only base tools (e.g.
# infobase_info); the extension/providers register the rest asynchronously while
# loading. Wait until tools/list returns at least this many entries before
# returning the initialize response to Claude Code.
WAIT_TOOLS_MIN = int(os.environ.get("MCP_PROXY_WAIT_TOOLS_MIN", "5"))
WAIT_TOOLS_TIMEOUT_SEC = float(os.environ.get("MCP_PROXY_WAIT_TOOLS_TIMEOUT", "120"))
WAIT_TOOLS_POLL_SEC = 1.0

LOG = logging.getLogger("mcp-proxy")


def setup_logging() -> None:
    handler = logging.FileHandler(LOG_FILE, mode="a", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOG.addHandler(handler)
    LOG.setLevel(logging.INFO)
    # Also mirror to stderr so Claude Code can show in MCP logs.
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(logging.Formatter("[mcp-proxy] %(levelname)s %(message)s"))
    LOG.addHandler(sh)


class ProxyState:
    """Holds session-id and the cached initialize request for re-init."""

    def __init__(self) -> None:
        self.session_id: str | None = None
        self.cached_initialize: dict | None = None  # raw request from CC
        # When False — backend is unreachable; we serve init/tools/list from
        # disk cache. Next tool call triggers a real connect attempt.
        self.backend_ready: bool = False
        # Sticky: set once we learn the backend has no server-push GET SSE
        # endpoint (HTTP 405/406 or non-stream body). Keeps notification_pump
        # dormant instead of hammering an unsupported endpoint.
        self.push_disabled: bool = False

    def reset(self) -> None:
        self.session_id = None
        self.backend_ready = False


def _save_response_cache(path: str, response: dict) -> None:
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(response, fh, ensure_ascii=False)
    except OSError as exc:
        LOG.warning("Could not save cache %s: %s", path, exc)


def _load_response_cache(path: str) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def _stub_initialize_response(request: dict) -> dict:
    """Replay last successful initialize, or fall back to a minimal stub."""
    cached = _load_response_cache(INIT_CACHE_FILE)
    if cached is not None:
        cached["id"] = request.get("id")
        return cached
    params = request.get("params") or {}
    return {
        "jsonrpc": "2.0",
        "id": request.get("id"),
        "result": {
            "protocolVersion": params.get("protocolVersion", "2025-03-26"),
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": f"{SERVER_LABEL} (offline stub)", "version": "0.0"},
        },
    }


def _stub_tools_list_response(request: dict) -> dict:
    """Replay last successful tools/list, or empty list when no cache."""
    cached = _load_response_cache(TOOLS_CACHE_FILE)
    if cached is not None:
        cached["id"] = request.get("id")
        return cached
    return {
        "jsonrpc": "2.0",
        "id": request.get("id"),
        "result": {"tools": []},
    }


def _backend_down_error(request: dict, detail: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": request.get("id"),
        "error": {
            "code": -32000,
            "message": (
                "1С MCP backend (объединённое расширение client_mcp на "
                "localhost:9874) недоступен — похоже, 1С не запущена. "
                f"Запусти 1С: `{START_HINT}` и повтори вызов — переподключение "
                "MCP не нужно, прокси держит сессию. "
                f"Детали: {detail}"
            ),
        },
    }


# Serializes stdout writes: both the request dispatcher and the background
# notification_pump write JSON-RPC lines to stdout, and the lines must not
# interleave. Instantiated at import (binds to the loop on first await).
_STDOUT_LOCK = asyncio.Lock()


async def write_stdout(obj: dict) -> None:
    line = json.dumps(obj, ensure_ascii=False)
    async with _STDOUT_LOCK:
        sys.stdout.buffer.write((line + "\n").encode("utf-8"))
        sys.stdout.buffer.flush()


def _build_headers(state: ProxyState) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if state.session_id:
        headers["Mcp-Session-Id"] = state.session_id
    return headers


def _parse_response(response: httpx.Response) -> dict | None:
    """Parse JSON or SSE response from backend."""
    content_type = response.headers.get("Content-Type", "")
    if "text/event-stream" in content_type:
        last_data: str | None = None
        for line in response.text.splitlines():
            if line.startswith("data:"):
                last_data = line[len("data:"):].lstrip()
        if last_data:
            return json.loads(last_data)
        return None
    body = response.text.strip()
    if not body:
        return None
    return json.loads(body)


async def _do_initialize(client: httpx.AsyncClient, state: ProxyState) -> None:
    """Send the cached initialize to backend, capture new session-id."""
    if state.cached_initialize is None:
        LOG.error("Cannot reinit — no cached initialize request from Claude Code")
        return
    state.reset()
    headers = _build_headers(state)
    LOG.info("Sending initialize to backend (reconnect path)")
    response = await client.post(
        BACKEND_URL,
        json=state.cached_initialize,
        headers=headers,
        timeout=30,
    )
    response.raise_for_status()
    new_session = response.headers.get("Mcp-Session-Id")
    if new_session:
        state.session_id = new_session
        LOG.info("Backend reinitialized; new session-id=%s", new_session)


async def _send_initialized_notification(
    client: httpx.AsyncClient, state: ProxyState
) -> None:
    """After initialize, MCP protocol requires a 'notifications/initialized' from client."""
    notif = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    try:
        await client.post(
            BACKEND_URL,
            json=notif,
            headers=_build_headers(state),
            timeout=10,
        )
        LOG.info("Sent notifications/initialized to backend")
    except Exception as exc:  # noqa: BLE001
        LOG.warning("Failed to send notifications/initialized: %s", exc)


async def _wait_for_tools(client: httpx.AsyncClient, state: ProxyState) -> int:
    """Poll tools/list until backend reports at least WAIT_TOOLS_MIN tools.

    VA EPF registers tools asynchronously after /Execute loads it; without this
    wait, the first call from Claude Code sees only base infobase_info and gets
    'tool not found' for all VA-specific tools.

    Returns the final tool count (may be less than WAIT_TOOLS_MIN on timeout).
    """
    deadline = asyncio.get_event_loop().time() + WAIT_TOOLS_TIMEOUT_SEC
    last_count = 0
    while asyncio.get_event_loop().time() < deadline:
        try:
            response = await client.post(
                BACKEND_URL,
                json={"jsonrpc": "2.0", "id": -999, "method": "tools/list"},
                headers=_build_headers(state),
                timeout=10,
            )
            response.raise_for_status()
            parsed = _parse_response(response)
            tools = (parsed or {}).get("result", {}).get("tools", [])
            last_count = len(tools)
            if last_count >= WAIT_TOOLS_MIN:
                LOG.info(
                    "Backend tools ready: %d tools registered (>= %d required)",
                    last_count,
                    WAIT_TOOLS_MIN,
                )
                return last_count
        except Exception as exc:  # noqa: BLE001
            LOG.debug("Polling tools/list raised %s", exc)
        await asyncio.sleep(WAIT_TOOLS_POLL_SEC)

    LOG.warning(
        "Timeout waiting for VA tools — got %d (< %d required) after %ss",
        last_count,
        WAIT_TOOLS_MIN,
        WAIT_TOOLS_TIMEOUT_SEC,
    )
    return last_count


async def call_backend_with_reconnect(
    client: httpx.AsyncClient, request: dict, state: ProxyState
) -> dict | None:
    """Send request to backend; auto-reinit on 404 / connection errors."""
    last_error: Exception | None = None
    for attempt in range(RECONNECT_RETRIES):
        try:
            headers = _build_headers(state)
            response = await client.post(
                BACKEND_URL, json=request, headers=headers, timeout=300
            )

            # 404 = stale session id (backend restarted with new id).
            if response.status_code == 404 and state.session_id is not None:
                LOG.warning(
                    "Backend 404 (session %s not found) — reinitializing",
                    state.session_id,
                )
                await _do_initialize(client, state)
                await _send_initialized_notification(client, state)
                # Wait for VA tools to register before retrying original request,
                # otherwise tools/call will get 'tool not found'.
                await _wait_for_tools(client, state)
                continue  # retry original request

            response.raise_for_status()

            # Capture session id from response (initialize returns it).
            new_session = response.headers.get("Mcp-Session-Id")
            if new_session:
                state.session_id = new_session

            state.backend_ready = True
            return _parse_response(response)

        except (
            httpx.ConnectError,
            httpx.ReadTimeout,
            httpx.RemoteProtocolError,
            httpx.HTTPStatusError,
        ) as exc:
            last_error = exc
            delay = RECONNECT_DELAYS[min(attempt, len(RECONNECT_DELAYS) - 1)]
            LOG.warning(
                "Backend error %s — sleeping %ss before retry (attempt %d/%d)",
                exc.__class__.__name__,
                delay,
                attempt + 1,
                RECONNECT_RETRIES,
            )
            state.reset()
            await asyncio.sleep(delay)
            # After a ConnectError, the next attempt would send the original
            # request without a session_id — useless for everything except
            # `initialize`. Try to spin up a fresh session first.
            if request.get("method") != "initialize" and state.cached_initialize:
                try:
                    await _do_initialize(client, state)
                    await _send_initialized_notification(client, state)
                    await _wait_for_tools(client, state)
                except Exception as init_exc:  # noqa: BLE001
                    LOG.debug(
                        "Inline reinit after ConnectError failed: %s",
                        init_exc.__class__.__name__,
                    )

    raise RuntimeError(f"Backend unavailable after {RECONNECT_RETRIES} attempts: {last_error}")


async def read_stdin_lines():
    """Async generator yielding parsed JSON-RPC messages from stdin."""
    loop = asyncio.get_event_loop()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: protocol, sys.stdin)
    while True:
        line_bytes = await reader.readline()
        if not line_bytes:
            return
        text = line_bytes.decode("utf-8").strip()
        if not text:
            continue
        try:
            yield json.loads(text)
        except json.JSONDecodeError as exc:
            LOG.error("Invalid JSON on stdin: %s | line=%r", exc, text)


async def _probe_backend_initialize(
    client: httpx.AsyncClient, state: ProxyState, request: dict
) -> dict | None:
    """Quick initialize probe: short timeout, no retries. Returns response or None."""
    state.reset()
    try:
        response = await client.post(
            BACKEND_URL,
            json=request,
            headers=_build_headers(state),
            timeout=INIT_PROBE_TIMEOUT_SEC,
        )
        response.raise_for_status()
        new_session = response.headers.get("Mcp-Session-Id")
        if new_session:
            state.session_id = new_session
        parsed = _parse_response(response)
        if parsed is not None:
            state.backend_ready = True
        return parsed
    except Exception as exc:  # noqa: BLE001
        LOG.warning(
            "Init probe to backend failed (%s) — serving stub init to CC",
            exc.__class__.__name__,
        )
        return None


async def handle_request(
    client: httpx.AsyncClient, state: ProxyState, request: dict
) -> None:
    method = request.get("method", "<no-method>")
    is_notification = "id" not in request

    # ---- initialize ---------------------------------------------------------
    # Always respond fast. Try backend with a short probe; on failure return a
    # stubbed (or cached) response so CC sees the MCP as connected.
    if method == "initialize":
        state.cached_initialize = dict(request)
        LOG.info("Cached initialize request from Claude Code")
        response = await _probe_backend_initialize(client, state, request)
        if response is not None:
            _save_response_cache(INIT_CACHE_FILE, response)
            await write_stdout(response)
            # Send the initialized notification + wait for VA tools to register.
            await _send_initialized_notification(client, state)
            await _wait_for_tools(client, state)
        else:
            stub = _stub_initialize_response(request)
            await write_stdout(stub)
        return

    # ---- notifications/initialized -----------------------------------------
    # Forward if backend is alive; swallow silently otherwise.
    if method == "notifications/initialized":
        if state.backend_ready:
            try:
                await client.post(
                    BACKEND_URL,
                    json=request,
                    headers=_build_headers(state),
                    timeout=5,
                )
            except Exception as exc:  # noqa: BLE001
                LOG.debug("forwarding notifications/initialized failed: %s", exc)
        return

    # ---- tools/list ---------------------------------------------------------
    # If backend down, replay cached list so CC keeps the toolset visible.
    if method == "tools/list":
        if not state.backend_ready:
            await _try_lazy_reconnect(client, state)
        if state.backend_ready:
            try:
                response = await call_backend_with_reconnect(client, request, state)
                if response is not None and "result" in response:
                    _save_response_cache(TOOLS_CACHE_FILE, response)
                if response is not None:
                    await write_stdout(response)
                    return
            except Exception as exc:  # noqa: BLE001
                LOG.warning("tools/list against live backend failed: %s", exc)
                state.reset()
        # fall through to cached / empty stub
        await write_stdout(_stub_tools_list_response(request))
        return

    # ---- all other methods (tools/call, ping, etc.) ------------------------
    if not state.backend_ready:
        reconnected = await _try_lazy_reconnect(client, state)
        if not reconnected and not is_notification:
            await write_stdout(_backend_down_error(request, "lazy reconnect failed"))
            return

    try:
        response = await call_backend_with_reconnect(client, request, state)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("Backend call failed: %s", exc)
        state.reset()
        if not is_notification:
            await write_stdout(_backend_down_error(request, str(exc)))
        return

    if is_notification:
        return
    if response is None:
        return
    await write_stdout(response)


async def _try_lazy_reconnect(
    client: httpx.AsyncClient, state: ProxyState
) -> bool:
    """Try to (re)initialize backend silently. True if backend is now alive."""
    if state.cached_initialize is None:
        LOG.debug("Lazy reconnect skipped — no cached initialize")
        return False
    try:
        await _do_initialize(client, state)
        await _send_initialized_notification(client, state)
        await _wait_for_tools(client, state)
        state.backend_ready = True
        LOG.info("Lazy reconnect to backend succeeded")
        return True
    except Exception as exc:  # noqa: BLE001
        LOG.debug("Lazy reconnect failed: %s", exc.__class__.__name__)
        state.reset()
        return False


async def _refresh_tools_cache(client: httpx.AsyncClient, state: ProxyState) -> None:
    """Re-fetch tools/list and update the disk cache (after tools/list_changed)."""
    if not state.backend_ready or not state.session_id:
        return
    try:
        response = await client.post(
            BACKEND_URL,
            json={"jsonrpc": "2.0", "id": -998, "method": "tools/list"},
            headers=_build_headers(state),
            timeout=15,
        )
        response.raise_for_status()
        parsed = _parse_response(response)
        if parsed is not None and "result" in parsed:
            _save_response_cache(TOOLS_CACHE_FILE, parsed)
            count = len(parsed.get("result", {}).get("tools", []))
            LOG.info("Tools cache refreshed after list_changed (%d tools)", count)
    except Exception as exc:  # noqa: BLE001
        LOG.debug("Tools cache refresh failed: %s", exc.__class__.__name__)


async def _handle_push_payload(
    payload: str, client: httpx.AsyncClient, state: ProxyState
) -> None:
    """Forward a server-initiated SSE payload (a 'data:' event) to Claude Code.

    Only messages carrying a 'method' (notifications + server→client requests)
    are forwarded; plain responses are skipped — tools/call results arrive on
    their own POST SSE response, not here, so forwarding responses would
    double-deliver / desync ids.
    """
    payload = payload.strip()
    if not payload or payload == "[DONE]":
        return
    try:
        msg = json.loads(payload)
    except json.JSONDecodeError:
        LOG.debug("Server-push payload not JSON: %r", payload[:200])
        return
    items = msg if isinstance(msg, list) else [msg]
    for item in items:
        if not isinstance(item, dict) or "method" not in item:
            continue
        await write_stdout(item)
        method = item.get("method")
        LOG.info("Forwarded server-push: %s", method)
        if method == "notifications/tools/list_changed":
            await _refresh_tools_cache(client, state)


async def notification_pump(client: httpx.AsyncClient, state: ProxyState) -> None:
    """Background task: long-poll the backend GET /mcp SSE stream and forward
    server-initiated messages to Claude Code.

    Resilient by design — never crashes the process: idles while 1С is down,
    reopens the stream after restarts (new session-id), and disables itself if
    the backend has no server-push endpoint (HTTP 405/406 or non-stream body).
    """
    idle_poll = 2.0   # backend not ready / no session yet
    backoff = 5.0     # after a stream error / non-200
    stream_timeout = httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0)
    while True:
        try:
            if state.push_disabled:
                await asyncio.sleep(60)
                continue
            if not state.backend_ready or not state.session_id:
                await asyncio.sleep(idle_poll)
                continue

            opened_session = state.session_id
            headers = _build_headers(state)
            headers["Accept"] = "text/event-stream"
            async with client.stream(
                "GET", BACKEND_URL, headers=headers, timeout=stream_timeout
            ) as response:
                if response.status_code in (404, 405, 406):
                    state.push_disabled = True
                    LOG.info(
                        "Backend has no server-push GET SSE (HTTP %d) — pump disabled",
                        response.status_code,
                    )
                    continue
                if response.status_code != 200:
                    LOG.debug("Push GET returned HTTP %d — backing off", response.status_code)
                    await asyncio.sleep(backoff)
                    continue
                ctype = response.headers.get("Content-Type", "")
                if "text/event-stream" not in ctype:
                    state.push_disabled = True
                    LOG.info(
                        "Backend GET /mcp is not an SSE stream (Content-Type=%r) — pump disabled",
                        ctype,
                    )
                    continue

                LOG.info("Server-push stream opened (session=%s)", opened_session)
                data_buf: list[str] = []
                async for line in response.aiter_lines():
                    # Stop if the session was rotated (restart) or backend went down.
                    if state.session_id != opened_session or not state.backend_ready:
                        break
                    if line.startswith(":"):
                        continue  # SSE comment / keep-alive
                    if line == "":
                        if data_buf:
                            await _handle_push_payload("\n".join(data_buf), client, state)
                            data_buf = []
                        continue
                    if line.startswith("data:"):
                        data_buf.append(line[len("data:"):].lstrip())
                if data_buf:
                    await _handle_push_payload("\n".join(data_buf), client, state)
            LOG.debug("Server-push stream closed; will reopen if backend stays ready")

        except asyncio.CancelledError:
            raise
        except (
            httpx.ConnectError,
            httpx.ReadError,
            httpx.ReadTimeout,
            httpx.RemoteProtocolError,
        ) as exc:
            LOG.debug("Server-push stream connection issue: %s", exc.__class__.__name__)
            await asyncio.sleep(backoff)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("notification_pump unexpected error: %s", exc)
            await asyncio.sleep(backoff)


async def main() -> None:
    setup_logging()
    LOG.info("MCP proxy starting; backend=%s", BACKEND_URL)
    state = ProxyState()
    timeout_cfg = httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=10.0)
    async with httpx.AsyncClient(timeout=timeout_cfg) as client:
        pump_task = asyncio.create_task(notification_pump(client, state))
        try:
            async for request in read_stdin_lines():
                # Run each request as a separate task so that a slow one doesn't
                # block the next read. But preserve order via shared state.
                await handle_request(client, state, request)
        finally:
            pump_task.cancel()
            try:
                await pump_task
            except asyncio.CancelledError:
                pass
    LOG.info("stdin closed; exiting")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
