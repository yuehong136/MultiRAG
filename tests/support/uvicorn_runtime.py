"""Own a test server loop even when an application patches asyncio.run."""

import asyncio
import socket

import uvicorn


def run_server(server: uvicorn.Server, *, sockets: list[socket.socket]) -> None:
    # Agent components may apply nest_asyncio before Uvicorn is imported. Its
    # replacement asyncio.run has no loop_factory argument. A thread-owned
    # Runner preserves Uvicorn's configured loop without changing global state.
    with asyncio.Runner(loop_factory=server.config.get_loop_factory()) as runner:
        runner.run(server.serve(sockets=sockets))
