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


@pytest.mark.parametrize("kind", ["node", "edge"])
async def test_phase_timeout_drains_embedding_before_retry_and_releases_limit(monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    from common.asyncio_utils import LoopLocalSemaphore
    from core.graphrag.general import index

    started, release, finished = (threading.Event() for _ in range(3))
    calls, active, peak = 0, 0, 0

    class Embedding:
        llm_name = "settlement-test"

        def encode(self, texts: list[str]) -> tuple[list[list[float]], int]:
            nonlocal calls, active, peak
            calls += 1
            active += 1
            peak = max(peak, active)
            try:
                if calls == 1:
                    started.set()
                    assert release.wait(5)
                    finished.set()
                return [[0.25, 0.75]], 0
            finally:
                active -= 1

    limiter = LoopLocalSemaphore(1)
    monkeypatch.setattr(utils, "chat_limiter", limiter)
    monkeypatch.setattr(utils, "get_embed_cache", lambda *args: None)
    monkeypatch.setattr(utils, "set_embed_cache", lambda *args: None)
    output: list[dict[str, Any]] = []
    meta = {"description": "description", "source_id": ["doc"], "entity_type": "person", "weight": 1, "keywords": ["related"]}

    async def operation() -> None:
        if kind == "node":
            await utils.graph_node_to_chunk("kb", Embedding(), "A", meta, output)
        else:
            await utils.graph_edge_to_chunk("kb", Embedding(), "A", "B", meta, output)

    running = asyncio.create_task(index._run_with_retry("embedding", operation, attempts=2, timeout_seconds=0.05, backoff_seconds=0, backoff_max_seconds=0))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        await asyncio.sleep(0.08)
        assert calls == 1 and limiter.locked() and not running.done()
        release.set()
        await asyncio.wait_for(running, 2)
        assert finished.is_set() and calls == 2 and peak == 1 and not limiter.locked()
        assert len(output) == 1
    finally:
        release.set()
        if not running.done():
            running.cancel()
        await asyncio.gather(running, return_exceptions=True)
