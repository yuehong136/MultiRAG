"""Module limiters must survive real contention, cancellation and loop reuse."""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from common.asyncio_utils import LoopLocalSemaphore
from core.graphrag.utils import chat_limiter
from core.svr import task_executor


async def contend(limiter: LoopLocalSemaphore) -> asyncio.Semaphore:
    native = limiter._get()
    for _ in range(limiter._value):
        await limiter.acquire()
    waiter = asyncio.create_task(limiter.acquire())
    await asyncio.sleep(0)
    assert not waiter.done() and limiter.locked()
    limiter.release()
    assert await waiter
    for _ in range(limiter._value):
        limiter.release()
    assert not limiter.locked()
    return native


@pytest.mark.parametrize("limiter", [chat_limiter, task_executor.task_limiter, task_executor.chunk_limiter, task_executor.embed_limiter, task_executor.minio_limiter, task_executor.kg_limiter])
def test_module_limiters_wait_in_two_sequential_loops(limiter: LoopLocalSemaphore) -> None:
    first = asyncio.run(contend(limiter))
    second = asyncio.run(contend(limiter))
    assert first is not second
    assert len(limiter._semaphores) == 1  # old bound loop has been evicted


def test_concurrent_thread_loops_have_independent_capacity() -> None:
    limiter = LoopLocalSemaphore(1)
    barrier = threading.Barrier(2)

    async def run() -> asyncio.Semaphore:
        async with limiter:
            assert await asyncio.to_thread(barrier.wait, 5) in (0, 1)
        return await contend(limiter)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(asyncio.run, run()) for _ in range(2)]
        natives = [future.result(timeout=10) for future in futures]
    assert natives[0] is not natives[1]


async def test_waiter_cancellation_and_context_error_restore_capacity() -> None:
    limiter = LoopLocalSemaphore(1)
    async with limiter:
        waiter = asyncio.create_task(limiter.acquire())
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert limiter.locked()
    with pytest.raises(RuntimeError, match="body"):
        async with limiter:
            raise RuntimeError("body")
    assert not limiter.locked()
    await contend(limiter)


async def test_cancel_after_wakeup_transfers_permit_to_next_waiter() -> None:
    limiter = LoopLocalSemaphore(1)
    await limiter.acquire()
    first, second = (asyncio.create_task(limiter.acquire()) for _ in range(2))
    await asyncio.sleep(0)
    limiter.release()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert await asyncio.wait_for(second, 1)
    limiter.release()
    assert not limiter.locked()


@pytest.mark.parametrize("value", [-1, True, 1.5, "2"])
def test_invalid_initial_capacity_fails(value: Any) -> None:
    with pytest.raises(ValueError):
        LoopLocalSemaphore(value)
