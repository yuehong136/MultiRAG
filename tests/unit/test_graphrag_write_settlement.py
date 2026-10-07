"""Actual executor writes must settle before cancellation/failure returns."""

import asyncio
import threading
from typing import Any

import pytest

from core.graphrag import utils


@pytest.mark.parametrize("cancel", [False, True])
async def test_bounded_writes_drain_running_threads_and_stop_dispatch(monkeypatch: pytest.MonkeyPatch, cancel: bool) -> None:
    started, release, finished, failed = (threading.Event() for _ in range(4))
    calls: list[str] = []
    attempts = 0

    def write(chunks: list[dict[str, Any]], *args: Any) -> None:
        nonlocal attempts
        name = chunks[0]["id"]
        calls.append(name)
        if name == "slow":
            started.set()
            assert release.wait(10)
            finished.set()
        elif name == "bad":
            attempts += 1
            if attempts == 3:
                failed.set()
            raise RuntimeError("injected write failure")
        else:
            raise AssertionError("late batch dispatched after stop")

    monkeypatch.setattr(utils, "checked_insert", write)
    monkeypatch.setattr(utils, "_INSERT_BULK_SIZE", 1)
    monkeypatch.setattr(utils, "_INSERT_CONCURRENCY", 1 if cancel else 2)
    chunks = [{"id": name} for name in ["slow", "bad", "late"]]
    task = asyncio.create_task(utils.insert_chunks_bounded(chunks, "tenant", "kb", index_name="scratch"))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        if cancel:
            task.cancel()
        else:
            assert await asyncio.to_thread(failed.wait, 5)
        await asyncio.sleep(0.03)
        assert not task.done() and not finished.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
            await asyncio.wait_for(task, 5)
        assert finished.is_set() and "late" not in calls
        assert not any(not child.done() and child is not asyncio.current_task() for child in asyncio.all_tasks())
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
