"""Launch the real MultiRAG MCP server on an ephemeral loopback port."""

import asyncio
import importlib.util
import json
import socket
from pathlib import Path
from typing import Any

import uvicorn
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

_SERVER_PATH = Path(__file__).resolve().parents[3] / "mcp" / "server" / "server.py"
_SPEC = importlib.util.spec_from_file_location("multirag_mcp_eim_f2_server", _SERVER_PATH)
assert _SPEC is not None and _SPEC.loader is not None
server = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(server)

_REQUEST_HEADERS: dict[str, str] = {}


class _CompatibilityCaptureMiddleware:
    """Expose only protocol routing headers from this loopback test process."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope.get("path") == "/__compat/headers":
            response = JSONResponse(_REQUEST_HEADERS)
            await response(scope, receive, send)
            return

        if scope["type"] == "http" and scope.get("path") == "/mcp":
            headers = {key.decode("latin-1").lower(): value.decode("latin-1") for key, value in scope.get("headers", [])}
            _REQUEST_HEADERS.clear()
            _REQUEST_HEADERS.update({key: value for key, value in headers.items() if key in {"mcp-protocol-version", "mcp-method", "mcp-name", "mcp-session-id"}})

        await self._app(scope, receive, send)


async def _empty_datasets(_self: Any, api_key: str) -> list[dict[str, Any]]:
    """Keep the compatibility probe independent from a running MultiRAG API."""
    assert api_key == "eim-f2-multirag"
    return []


def _bound_loopback_socket() -> tuple[socket.socket, int]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(2048)
    return listener, listener.getsockname()[1]


def main() -> None:
    server.MODE = server.LaunchMode.SELF_HOST
    server.HOST_API_KEY = "eim-f2-multirag"
    server.TRANSPORT_SSE_ENABLED = False
    server.TRANSPORT_STREAMABLE_HTTP_ENABLED = True
    server.JSON_RESPONSE = True
    server.ALLOWED_HOSTS = []
    server.ALLOWED_ORIGINS = []
    server.MultiRAGConnector.list_datasets_structured = _empty_datasets
    server.create_mcp_server()
    app = _CompatibilityCaptureMiddleware(server.create_starlette_app())
    listener, port = _bound_loopback_socket()
    print(
        json.dumps(
            {
                "event": "ready",
                "server": "multirag-current",
                "transport": "streamable-http",
                "url": f"http://127.0.0.1:{port}/mcp",
                "headers_url": f"http://127.0.0.1:{port}/__compat/headers",
            },
            sort_keys=True,
        ),
        flush=True,
    )
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on")
    asyncio.run(uvicorn.Server(config).serve(sockets=[listener]))


if __name__ == "__main__":
    main()
