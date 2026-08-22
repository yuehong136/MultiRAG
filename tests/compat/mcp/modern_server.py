# /// script
# requires-python = ">=3.12,<3.15"
# dependencies = [
#   "mcp==2.0.0",
# ]
# ///
"""Isolated MCP SDK 2 fixture and probe for EIM-F2.

The exact PEP 723 lock keeps the compatibility oracle independent from
MultiRAG's production dependency resolution.
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
from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS

import mcp.types as types
from mcp.client import Client
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
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


class HandshakeCounterofferApp:
    """Minimal typed wire oracle for one released initialize-era revision."""

    def __init__(self, protocol_version: str) -> None:
        if protocol_version not in HANDSHAKE_PROTOCOL_VERSIONS:
            raise ValueError(f"not a supported handshake protocol version: {protocol_version}")
        self.protocol_version = protocol_version
        self.request_headers: dict[str, str] = {}
        self.methods_seen: list[str] = []

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        if scope["type"] == "lifespan":
            await self._lifespan(receive, send)
            return
        if scope["type"] != "http":
            return
        if scope.get("path") == "/__compat/headers":
            await self._send_json(
                send,
                200,
                self.request_headers | {"methods_seen": self.methods_seen},
            )
            return
        if scope.get("path") != "/mcp":
            await self._send_json(send, 404, {"error": "not_found"})
            return
        if scope.get("method") == "DELETE":
            await self._send_json(send, 200, {})
            return

        headers = {key.decode("latin-1").lower(): value.decode("latin-1") for key, value in scope.get("headers", [])}
        body = await self._read_body(receive)
        try:
            request = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            await self._send_json(send, 400, {"error": "invalid_json"})
            return
        if not isinstance(request, dict):
            await self._send_json(send, 400, {"error": "invalid_request"})
            return

        method = request.get("method")
        request_id = request.get("id")
        if isinstance(method, str):
            self.methods_seen.append(method)
            self.request_headers = {
                "method": method,
                **{
                    key: value
                    for key, value in headers.items()
                    if key
                    in {
                        "mcp-protocol-version",
                        "mcp-method",
                        "mcp-name",
                        "mcp-session-id",
                    }
                },
            }

        if method == "server/discover":
            await self._send_json(
                send,
                200,
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32601, "message": "Method not found"},
                },
            )
            return
        if method == "notifications/initialized":
            await self._send_empty(send, 202)
            return

        result: dict[str, Any]
        if method == "initialize":
            result = types.InitializeResult(
                protocol_version=self.protocol_version,
                capabilities=types.ServerCapabilities(
                    tools=types.ToolsCapability(list_changed=False),
                ),
                server_info=types.Implementation(
                    name="eim-f9-handshake-counteroffer",
                    version="2.0.0-fixture",
                ),
            ).model_dump(
                mode="json",
                by_alias=True,
                exclude_none=True,
                exclude_defaults=True,
            )
        elif method == "tools/list":
            result = types.ListToolsResult(
                tools=[
                    types.Tool(
                        name="compat_echo",
                        description="Return the configured handshake protocol version.",
                        input_schema={
                            "type": "object",
                            "properties": {"value": {"type": "string"}},
                            "required": ["value"],
                        },
                    )
                ]
            ).model_dump(
                mode="json",
                by_alias=True,
                exclude_none=True,
                exclude_defaults=True,
            )
        elif method == "tools/call":
            params = request.get("params")
            arguments = params.get("arguments", {}) if isinstance(params, dict) else {}
            value = arguments.get("value") if isinstance(arguments, dict) else None
            content = types.TextContent(
                text=json.dumps(
                    {
                        "echo": value,
                        "server_protocol_version": self.protocol_version,
                    },
                    sort_keys=True,
                )
            )
            result = {
                "content": [
                    content.model_dump(
                        mode="json",
                        by_alias=True,
                        exclude_none=True,
                    )
                ]
            }
        else:
            await self._send_json(
                send,
                200,
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32601, "message": "Method not found"},
                },
            )
            return

        await self._send_json(
            send,
            200,
            {"jsonrpc": "2.0", "id": request_id, "result": result},
        )

    @staticmethod
    async def _read_body(
        receive: Callable[[], Awaitable[dict[str, Any]]],
    ) -> bytes:
        chunks: list[bytes] = []
        while True:
            message = await receive()
            chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                return b"".join(chunks)

    @staticmethod
    async def _send_empty(
        send: Callable[[dict[str, Any]], Awaitable[None]],
        status: int,
    ) -> None:
        await send({"type": "http.response.start", "status": status, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    @staticmethod
    async def _send_json(
        send: Callable[[dict[str, Any]], Awaitable[None]],
        status: int,
        payload: dict[str, Any],
    ) -> None:
        body = json.dumps(payload, sort_keys=True).encode()
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    @staticmethod
    async def _lifespan(
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return


def _bound_loopback_socket() -> tuple[socket.socket, int]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(2048)
    return listener, listener.getsockname()[1]


def _protocol_headers(protocol_version: str, token: str | None) -> dict[str, str]:
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "Mcp-Protocol-Version": protocol_version,
    }
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return headers


async def _post_jsonrpc(
    client: httpx2.AsyncClient,
    url: str,
    *,
    payload: dict[str, Any],
    headers: dict[str, str],
) -> tuple[int, dict[str, Any] | None]:
    response = await client.post(url, json=payload, headers=headers)
    if not response.content:
        return response.status_code, None
    return response.status_code, response.json()


async def _probe_handshake_version(
    url: str,
    protocol_version: str,
    token: str | None,
    tool_name: str,
) -> dict[str, Any]:
    if protocol_version not in HANDSHAKE_PROTOCOL_VERSIONS:
        raise ValueError(f"not a supported handshake protocol version: {protocol_version}")
    headers = _protocol_headers(protocol_version, token)
    async with httpx2.AsyncClient() as client:
        initialize_status, initialize = await _post_jsonrpc(
            client,
            url,
            payload={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": protocol_version,
                    "capabilities": {},
                    "clientInfo": {"name": "eim-f9-exact-handshake", "version": "1"},
                },
            },
            headers=headers,
        )
        initialized_status, _ = await _post_jsonrpc(
            client,
            url,
            payload={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers=headers,
        )
        list_status, listed = await _post_jsonrpc(
            client,
            url,
            payload={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            headers=headers,
        )
        call_status, called = await _post_jsonrpc(
            client,
            url,
            payload={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": {}},
            },
            headers=headers,
        )
    initialize_result = initialize.get("result", {}) if isinstance(initialize, dict) else {}
    list_result = listed.get("result", {}) if isinstance(listed, dict) else {}
    call_result = called.get("result", {}) if isinstance(called, dict) else {}
    return {
        "event": "handshake-probe",
        "requested_protocol_version": protocol_version,
        "negotiated_protocol_version": initialize_result.get("protocolVersion"),
        "initialize_status": initialize_status,
        "initialized_status": initialized_status,
        "list_status": list_status,
        "tools": sorted(tool.get("name") for tool in list_result.get("tools", []) if isinstance(tool, dict) and isinstance(tool.get("name"), str)),
        "call_status": call_status,
        "tool_result": call_result.get("structuredContent"),
        "is_error": call_result.get("isError", False),
    }


async def _probe_unknown_version(
    url: str,
    protocol_version: str,
    token: str | None,
) -> dict[str, Any]:
    headers = _protocol_headers(protocol_version, token) | {
        "Mcp-Method": "tools/list",
    }
    async with httpx2.AsyncClient() as client:
        status, response = await _post_jsonrpc(
            client,
            url,
            payload={
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/list",
                "params": {
                    "_meta": {
                        "io.modelcontextprotocol/protocolVersion": protocol_version,
                        "io.modelcontextprotocol/clientCapabilities": {},
                    }
                },
            },
            headers=headers,
        )
    error = response.get("error", {}) if isinstance(response, dict) else {}
    return {
        "event": "unknown-version-probe",
        "protocol_version": protocol_version,
        "status": status,
        "error_code": error.get("code"),
        "error_data": error.get("data"),
    }


async def _probe(
    url: str,
    mode: str,
    cancel: bool,
    list_only: bool,
    token: str | None,
    tool_name: str | None,
) -> dict[str, Any]:
    async with AsyncExitStack() as stack:
        client_target: Any = url
        if token is not None:
            http_client = await stack.enter_async_context(create_mcp_http_client(headers={"Authorization": f"Bearer {token}"}))
            client_target = streamable_http_client(url, http_client=http_client)
        client = await stack.enter_async_context(Client(client_target, mode=mode))
        tools = await client.list_tools()
        if list_only:
            return {
                "event": "probe",
                "protocol_version": client.protocol_version,
                "tools": sorted(tool.name for tool in tools.tools),
            }
        if tool_name is not None:
            tool_result = await client.call_tool(tool_name, {})
            return {
                "event": "probe",
                "protocol_version": client.protocol_version,
                "tools": sorted(tool.name for tool in tools.tools),
                "tool_name": tool_name,
                "tool_result": tool_result.structured_content,
                "is_error": tool_result.is_error,
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


def _serve_handshake(protocol_version: str) -> None:
    listener, port = _bound_loopback_socket()
    app = HandshakeCounterofferApp(protocol_version)
    print(
        json.dumps(
            {
                "event": "ready",
                "server": "handshake-counteroffer",
                "protocol_version": protocol_version,
                "transport": "streamable-http",
                "url": f"http://127.0.0.1:{port}/mcp",
                "headers_url": f"http://127.0.0.1:{port}/__compat/headers",
            },
            sort_keys=True,
        ),
        flush=True,
    )
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        lifespan="on",
    )
    asyncio.run(uvicorn.Server(config).serve(sockets=[listener]))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--serve-handshake")
    parser.add_argument("--probe")
    parser.add_argument("--mode", default="auto")
    parser.add_argument("--handshake-version")
    parser.add_argument("--unknown-version")
    parser.add_argument("--cancel", action="store_true")
    parser.add_argument("--list-only", action="store_true")
    parser.add_argument("--token")
    parser.add_argument("--tool")
    args = parser.parse_args()
    if args.serve:
        _serve()
        return
    if args.serve_handshake:
        _serve_handshake(args.serve_handshake)
        return
    if not args.probe:
        parser.error("choose --serve, --serve-handshake VERSION, or --probe URL")
    if args.handshake_version:
        if args.tool is None:
            parser.error("--handshake-version requires --tool")
        print(
            json.dumps(
                asyncio.run(
                    _probe_handshake_version(
                        args.probe,
                        args.handshake_version,
                        args.token,
                        args.tool,
                    )
                ),
                sort_keys=True,
            )
        )
        return
    if args.unknown_version:
        print(
            json.dumps(
                asyncio.run(
                    _probe_unknown_version(
                        args.probe,
                        args.unknown_version,
                        args.token,
                    )
                ),
                sort_keys=True,
            )
        )
        return
    print(
        json.dumps(
            asyncio.run(
                _probe(
                    args.probe,
                    args.mode,
                    args.cancel,
                    args.list_only,
                    args.token,
                    args.tool,
                )
            ),
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
