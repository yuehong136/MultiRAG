"""Own detached Agent work until completion and stop it before app resources close."""

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

_tasks: set[asyncio.Task[None]] = set()


def start_agent_task(work: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
    task = asyncio.create_task(work, name="agent-webhook")
    _tasks.add(task)
    task.add_done_callback(_completed)
    return task


def _completed(task: asyncio.Task[None]) -> None:
    _tasks.discard(task)
    if task.cancelled():
        logging.info("Agent background task cancelled")
        return
    error = task.exception()
    if error is not None:
        logging.error("Agent background task failed (%s)", type(error).__name__)


async def stop_agent_tasks() -> None:
    loop = asyncio.get_running_loop()
    tasks = [task for task in _tasks if task.get_loop() is loop]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _tasks.difference_update(tasks)
