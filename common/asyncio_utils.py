"""Loop-local concurrency limits for module-level async consumers."""

import asyncio
import threading
from types import TracebackType
from weakref import WeakKeyDictionary


class LoopLocalSemaphore:
    """Keep native waiting/cancellation semantics independently in each loop.

    A semaphore binds to a loop only on contention. Closed loops are evicted
    because a bound semaphore can itself hold a reference to its loop.
    """

    def __init__(self, value: int) -> None:
        if type(value) is not int or value < 0:
            raise ValueError("Semaphore value must be a nonnegative integer")
        self._value = value
        self._semaphores: WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = WeakKeyDictionary()
        self._guard = threading.Lock()

    def _get(self) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        with self._guard:
            for cached_loop in list(self._semaphores):
                if cached_loop.is_closed():
                    del self._semaphores[cached_loop]
            sem = self._semaphores.get(loop)
            if sem is None:
                sem = asyncio.Semaphore(self._value)
                self._semaphores[loop] = sem
            return sem

    async def acquire(self) -> bool:
        return await self._get().acquire()

    def release(self) -> None:
        self._get().release()

    def locked(self) -> bool:
        return self._get().locked()

    async def __aenter__(self) -> "LoopLocalSemaphore":
        await self.acquire()
        return self

    async def __aexit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None) -> None:
        self.release()
