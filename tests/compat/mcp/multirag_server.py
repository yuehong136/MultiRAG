"""Launch the real MultiRAG MCP server on an ephemeral loopback port."""

import asyncio
import importlib.util
import json
import socket
from pathlib import Path

import uvicorn

_SERVER_PATH = Path(__file__).resolve().parents[3] / "mcp" / "server" / "server.py"
_SPEC = importlib.util.spec_from_file_location("multirag_mcp_eim_f2_server", _SERVER_PATH)
assert _SPEC is not None and _SPEC.loader is not None
server = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(server)


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
    server.create_mcp_server()
    app = server.create_starlette_app()
    listener, port = _bound_loopback_socket()
    print(
        json.dumps(
            {
                "event": "ready",
                "server": "multirag-current",
                "transport": "streamable-http",
                "url": f"http://127.0.0.1:{port}/mcp",
            },
            sort_keys=True,
        ),
        flush=True,
    )
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on")
    asyncio.run(uvicorn.Server(config).serve(sockets=[listener]))


if __name__ == "__main__":
    main()
