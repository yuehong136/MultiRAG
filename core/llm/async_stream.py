"""Synchronous iteration of an async stream without taking over a caller loop."""

import asyncio
import concurrent.futures
import threading
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterator
from typing import TypeVar

T = TypeVar("T")
R = TypeVar("R")


def iterate_owned_stream(factory: Callable[[], AsyncGenerator[T, None]]) -> Iterator[T]:
    # A running loop cannot host run_until_complete, even for a different loop.
    # Move only that case to an owned thread; ordinary worker threads stay local.
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        threaded = False
    else:
        threaded = True
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, name="local-llm-stream") if threaded else None
    if thread is not None:
        thread.start()

    def run(work: Awaitable[R]) -> R:
        async def await_work() -> R:
            return await work

        if thread is not None:
            try:
                return asyncio.run_coroutine_threadsafe(await_work(), loop).result()
            except concurrent.futures.CancelledError:
                raise asyncio.CancelledError from None
        return loop.run_until_complete(work)

    async def open_stream() -> AsyncGenerator[T, None]:
        return factory()

    async def drain_tasks() -> None:
        tasks = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    stream = None
    try:
        stream = run(open_stream())
        while True:
            try:
                yield run(anext(stream))
            except StopAsyncIteration:
                return
    finally:
        try:
            if stream is not None:
                run(stream.aclose())
        finally:
            try:
                run(drain_tasks())
                run(loop.shutdown_asyncgens())
                run(loop.shutdown_default_executor())
            finally:
                if thread is not None:
                    loop.call_soon_threadsafe(loop.stop)
                    thread.join()
                loop.close()
