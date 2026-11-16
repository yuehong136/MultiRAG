"""Event loops async psycopg accepts, for scratch servers and sync test drivers."""

import asyncio
import sys
from collections.abc import Coroutine
from typing import Any

# Same choice as api/multirag_server.py: on win32 uvicorn and asyncio.run()
# default to Proactor loops, which async psycopg rejects (InterfaceError).
UVICORN_LOOP = "asyncio:SelectorEventLoop" if sys.platform == "win32" else "auto"


def run[T](coroutine: Coroutine[Any, Any, T]) -> T:
    """Own and drain a selector loop without replacing an installed outer loop."""
    # Agent imports can patch asyncio.run() to reuse an existing loop. Runner
    # still closes this driver's loop, including its SDK logging children.
    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        return runner.run(coroutine)
