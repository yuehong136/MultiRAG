# /// script
# requires-python = ">=3.12,<3.15"
# dependencies = [
#   "fastmcp==3.4.7",
# ]
# ///
"""Hermetic MCP SDK 1 / FastMCP 3 compatibility fixture for EIM-F2."""

import argparse
import asyncio
import json
import socket
from typing import Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

_CALL_STATUS: dict[str, str] = {}

server = FastMCP(
    "eim-f2-legacy",
    instructions="MCP SDK 1 fixture; this server must remain legacy-only.",
)


@server.tool
async def compat_echo(value: str) -> dict[str, str]:
    """Return deterministic structured data for protocol compatibility tests."""
    return {"echo": value, "server_era": "legacy"}


@server.tool
async def compat_fail() -> None:
    """Return a deterministic MCP tool error."""
    raise ToolError("eim-f2 fixture tool error")


@server.tool
async def compat_wait(call_id: str, delay_ms: int = 250) -> dict[str, str]:
    """Expose whether a timed-out client actually cancelled server execution."""
    _CALL_STATUS[call_id] = "running"
    try:
        await asyncio.sleep(delay_ms / 1000)
    except asyncio.CancelledError:
        _CALL_STATUS[call_id] = "cancelled"
        raise
    _CALL_STATUS[call_id] = "completed"
    return {"call_id": call_id, "status": "completed"}


@server.tool
async def compat_status(call_id: str) -> dict[str, str]:
    """Read the server-side status of a wait probe."""
    return {"call_id": call_id, "status": _CALL_STATUS.get(call_id, "unknown")}


def _bound_loopback_socket() -> tuple[socket.socket, int]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(2048)
    return listener, listener.getsockname()[1]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--transport", choices=("http", "sse"), default="http")
    args = parser.parse_args()
    transport: Literal["http", "sse"] = args.transport
    path = "/mcp" if transport == "http" else "/sse"
    listener, port = _bound_loopback_socket()
    print(
        json.dumps(
            {
                "event": "ready",
                "server": "legacy",
                "transport": transport,
                "url": f"http://127.0.0.1:{port}{path}",
            },
            sort_keys=True,
        ),
        flush=True,
    )
    server.run(
        transport=transport,
        show_banner=False,
        host="127.0.0.1",
        port=port,
        path=path,
        stateless_http=transport == "http",
        sockets=[listener],
        uvicorn_config={"log_level": "warning"},
    )


if __name__ == "__main__":
    main()
