#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "httpx>=0.27",
# ]
# ///
"""Persistent MCP proxy with auto-reconnect for client_mcp.cfe (Vanessa Automation).

Sits between Claude Code (stdio JSON-RPC) and client_mcp.cfe (HTTP Streamable on
http://localhost:9874/mcp). When 1С restarts, the HTTP backend gets a fresh
session-id; this proxy detects HTTP 404 / ConnectionError and silently
re-initializes the backend session, retrying the original request. Claude Code's
stdio session stays alive across all 1С restarts — no /mcp needed.

Limitations / notes:
- Server-push notifications (GET /mcp SSE stream) NOT proxied yet. VA-side
  notifications are rare; if needed, add a background task that opens a
  long-poll GET and forwards SSE events to stdout.
- After re-init the backend state is reset (open feature, connected TestClient
  forgotten). LLM should re-issue setup steps if it sees unexpected state.

Usage in .mcp.json:
    "vanessa": {
        "type": "stdio",
        "command": "python3",
        "args": [
            "/Users/Shared/MyWork1C_AI/.claude/skills/vanessa-bdd-deploy/scripts/mcp-proxy-reconnect.py"
        ]
    }

Backend URL configurable via env var MCP_BACKEND_URL (default http://localhost:9874/mcp).
Log file via MCP_PROXY_LOG (default /tmp/mcp-proxy-reconnect.log).
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

RECONNECT_RETRIES = 8
RECONNECT_DELAYS = [0.5, 1, 2, 4, 8, 16, 30, 30]  # seconds

# After initialize backend MCP server has only base tools (e.g. infobase_info).
# Vanessa Automation EPF registers ~25 more tools asynchronously when it
# finishes loading via /Execute. Wait until tools/list returns at least this
# many entries before returning the initialize response to Claude Code.
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

    def reset(self) -> None:
        self.session_id = None


async def write_stdout(obj: dict) -> None:
    line = json.dumps(obj, ensure_ascii=False)
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


async def handle_request(
    client: httpx.AsyncClient, state: ProxyState, request: dict
) -> None:
    method = request.get("method", "<no-method>")
    req_id = request.get("id")

    # Cache initialize for reconnect path.
    if method == "initialize":
        state.cached_initialize = dict(request)
        LOG.info("Cached initialize request from Claude Code")

    is_notification = "id" not in request
    try:
        response = await call_backend_with_reconnect(client, request, state)

        # Note: tools/list is NOT blocked here. Claude Code has a tight
        # default timeout on tools/list; waiting longer than that makes CC
        # abandon the connection. Instead we ensure tools are ready BEFORE
        # CC ever connects, via vanessa-bdd-deploy wait-mcp script which
        # polls tools/list count itself.

    except Exception as exc:  # noqa: BLE001
        LOG.exception("Failed to call backend: %s", exc)
        if not is_notification:
            await write_stdout(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32603, "message": f"Proxy error: {exc}"},
                }
            )
        return

    if is_notification:
        return  # don't echo notifications back to CC
    if response is None:
        return
    await write_stdout(response)


async def main() -> None:
    setup_logging()
    LOG.info("MCP proxy starting; backend=%s", BACKEND_URL)
    state = ProxyState()
    timeout_cfg = httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=10.0)
    async with httpx.AsyncClient(timeout=timeout_cfg) as client:
        async for request in read_stdin_lines():
            # Run each request as a separate task so that a slow one doesn't
            # block the next read. But preserve order via shared state.
            await handle_request(client, state, request)
    LOG.info("stdin closed; exiting")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
