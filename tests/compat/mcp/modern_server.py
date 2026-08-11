# /// script
# requires-python = ">=3.12,<3.15"
# dependencies = [
#   "mcp==2.0.0",
# ]
# ///
"""Isolated MCP SDK 2 fixture and probe for EIM-F2.

The PEP 723 environment prevents MCP SDK 2 from entering MultiRAG's production
dependency graph while FastMCP 3 still requires ``mcp<2``.
"""

import argparse
import asyncio
import json
import socket
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from typing import Any

import httpx2
import uvicorn

from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.server import MCPServer

_VALID_TOKEN = "eim-f2-valid"
_INSUFFICIENT_TOKEN = "eim-f2-insufficient"
_CALL_STATUS: dict[str, str] = {}
_REQUEST_HEADERS: dict[str, str] = {}

server = MCPServer(
    "eim-f2-modern",
    version="2.0.0-fixture",
    instructions="MCP 2026-07-28 compatibility fixture.",
)


@server.tool(structured_output=True)
async def compat_echo(value: str) -> dict[str, Any]:
    """Return structured data and the MCP routing headers seen by the server."""
    return {
        "echo": value,
        "server_era": "modern",
        "request_headers": {key: _REQUEST_HEADERS.get(key) for key in ("mcp-protocol-version", "mcp-method", "mcp-name", "mcp-session-id") if _REQUEST_HEADERS.get(key) is not None},
    }


@server.tool(structured_output=True)
async def compat_fail() -> None:
    """Return a deterministic MCP tool error."""
    raise RuntimeError("eim-f2 fixture tool error")


@server.tool(structured_output=True)
async def compat_wait(call_id: str, delay_ms: int = 250) -> dict[str, str]:
    """Expose whether client cancellation reaches the server handler."""
    _CALL_STATUS[call_id] = "running"
    try:
        await asyncio.sleep(delay_ms / 1000)
    except asyncio.CancelledError:
        _CALL_STATUS[call_id] = "cancelled"
        raise
    _CALL_STATUS[call_id] = "completed"
    return {"call_id": call_id, "status": "completed"}


@server.tool(structured_output=True)
async def compat_status(call_id: str) -> dict[str, str]:
    """Read the server-side status of a wait probe."""
    return {"call_id": call_id, "status": _CALL_STATUS.get(call_id, "unknown")}


class CompatibilityMiddleware:
    """Add loopback-only auth fixtures and capture protocol routing headers."""

    def __init__(self, app: Callable[..., Awaitable[None]]) -> None:
        self._app = app

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        headers = {key.decode("latin-1").lower(): value.decode("latin-1") for key, value in scope.get("headers", [])}
        _REQUEST_HEADERS.clear()
        _REQUEST_HEADERS.update(headers)

        downstream_scope = scope
        path = scope.get("path", "")
        if path.startswith("/protected"):
            token = headers.get("authorization", "").removeprefix("Bearer ")
            if token != _VALID_TOKEN:
                status = 403 if token == _INSUFFICIENT_TOKEN else 401
                body = b'{"error":"insufficient_scope"}' if status == 403 else b'{"error":"unauthorized"}'
                await send(
                    {
                        "type": "http.response.start",
                        "status": status,
                        "headers": [(b"content-type", b"application/json")],
                    }
                )
                await send({"type": "http.response.body", "body": body})
                return
            downstream_scope = dict(scope)
            downstream_scope["path"] = path.removeprefix("/protected") or "/"
            downstream_scope["raw_path"] = downstream_scope["path"].encode()

        await self._app(downstream_scope, receive, send)


def _bound_loopback_socket() -> tuple[socket.socket, int]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(2048)
    return listener, listener.getsockname()[1]


async def _probe(
    url: str,
    mode: str,
    cancel: bool,
    list_only: bool,
    token: str | None,
) -> dict[str, Any]:
    async with AsyncExitStack() as stack:
        client_target: Any = url
        if token is not None:
            http_client = await stack.enter_async_context(httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}))
            client_target = streamable_http_client(url, http_client=http_client)
        client = await stack.enter_async_context(Client(client_target, mode=mode))
        tools = await client.list_tools()
        if list_only:
            return {
                "event": "probe",
                "protocol_version": client.protocol_version,
                "tools": sorted(tool.name for tool in tools.tools),
            }
        echo = await client.call_tool("compat_echo", {"value": "modern-probe"})
        result: dict[str, Any] = {
            "event": "probe",
            "protocol_version": client.protocol_version,
            "tools": sorted(tool.name for tool in tools.tools),
            "echo": echo.structured_content,
            "is_error": echo.is_error,
        }
        if not cancel:
            return result

        call_id = "modern-cancel"
        pending = asyncio.create_task(client.call_tool("compat_wait", {"call_id": call_id, "delay_ms": 1000}))
        await asyncio.sleep(0.05)
        pending.cancel()
        try:
            await pending
        except asyncio.CancelledError:
            result["caller_cancelled"] = True

    await asyncio.sleep(0.1)
    async with Client(url, mode=mode) as status_client:
        status = await status_client.call_tool("compat_status", {"call_id": "modern-cancel"})
        result["server_status_after_cancel"] = status.structured_content
        await asyncio.sleep(1.0)
        final_status = await status_client.call_tool("compat_status", {"call_id": "modern-cancel"})
        result["server_status_final"] = final_status.structured_content
    return result


def _serve() -> None:
    listener, port = _bound_loopback_socket()
    app = CompatibilityMiddleware(
        server.streamable_http_app(
            streamable_http_path="/mcp",
            json_response=True,
            stateless_http=True,
            host="127.0.0.1",
        )
    )
    print(
        json.dumps(
            {
                "event": "ready",
                "server": "modern",
                "transport": "streamable-http",
                "url": f"http://127.0.0.1:{port}/mcp",
                "protected_url": f"http://127.0.0.1:{port}/protected/mcp",
            },
            sort_keys=True,
        ),
        flush=True,
    )
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on")
    asyncio.run(uvicorn.Server(config).serve(sockets=[listener]))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--probe")
    parser.add_argument("--mode", default="auto")
    parser.add_argument("--cancel", action="store_true")
    parser.add_argument("--list-only", action="store_true")
    parser.add_argument("--token")
    args = parser.parse_args()
    if args.serve:
        _serve()
        return
    if not args.probe:
        parser.error("choose --serve or --probe URL")
    print(
        json.dumps(
            asyncio.run(
                _probe(
                    args.probe,
                    args.mode,
                    args.cancel,
                    args.list_only,
                    args.token,
                )
            ),
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
