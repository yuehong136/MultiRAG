"""Bounded async runtime primitives for provider calls."""

from __future__ import annotations

import asyncio
import math
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Hashable
from dataclasses import dataclass, field
from typing import Generic, TypeVar

_K = TypeVar("_K", bound=Hashable)
_V = TypeVar("_V")


class ProviderRuntimeCapacityError(RuntimeError):
    """The bounded provider runtime cannot accept another distinct request."""


@dataclass(frozen=True, slots=True)
class ProducedValue(Generic[_V]):
    value: _V = field(repr=False)
    ttl_seconds: float


@dataclass(frozen=True, slots=True)
class _CachedValue(Generic[_V]):
    value: _V = field(repr=False)
    expires_at: float


class AsyncExpiringSingleFlightCache(Generic[_K, _V]):
    """Bounded monotonic cache with cancellation-safe shared producers."""

    def __init__(
        self,
        *,
        max_entries: int,
        max_in_flight: int,
        clock: Callable[[], float],
        max_ttl_seconds: float = 86_400.0,
    ) -> None:
        if max_entries < 1 or max_in_flight < 1 or not math.isfinite(max_ttl_seconds) or max_ttl_seconds <= 0:
            raise ValueError("provider cache bounds must be positive")
        self._max_entries = max_entries
        self._max_in_flight = max_in_flight
        self._clock = clock
        self._max_ttl_seconds = max_ttl_seconds
        self._cache: OrderedDict[_K, _CachedValue[_V]] = OrderedDict()
        self._in_flight: dict[_K, asyncio.Task[_V]] = {}
        self._lock = asyncio.Lock()

    async def get_or_create(
        self,
        key: _K,
        producer: Callable[[], Awaitable[ProducedValue[_V]]],
    ) -> tuple[_V, bool]:
        async with self._lock:
            self._prune_expired()
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
                return cached.value, True
            task = self._in_flight.get(key)
            if task is None:
                if len(self._in_flight) >= self._max_in_flight:
                    raise ProviderRuntimeCapacityError("provider runtime capacity exhausted")
                task = asyncio.create_task(self._produce(key, producer))
                task.add_done_callback(_consume_task_exception)
                self._in_flight[key] = task
        return await asyncio.shield(task), False

    async def invalidate(self, predicate: Callable[[_K], bool]) -> None:
        async with self._lock:
            for key in tuple(self._cache):
                if predicate(key):
                    del self._cache[key]

    async def _produce(
        self,
        key: _K,
        producer: Callable[[], Awaitable[ProducedValue[_V]]],
    ) -> _V:
        try:
            produced = await producer()
            if not math.isfinite(produced.ttl_seconds) or not 0 <= produced.ttl_seconds <= self._max_ttl_seconds:
                raise ProviderRuntimeCapacityError("provider cache ttl is invalid")
            if produced.ttl_seconds > 0:
                async with self._lock:
                    now = self._safe_clock()
                    self._cache[key] = _CachedValue(
                        value=produced.value,
                        expires_at=now + produced.ttl_seconds,
                    )
                    self._cache.move_to_end(key)
                    while len(self._cache) > self._max_entries:
                        self._cache.popitem(last=False)
            return produced.value
        finally:
            current = asyncio.current_task()
            async with self._lock:
                if self._in_flight.get(key) is current:
                    del self._in_flight[key]

    def _prune_expired(self) -> None:
        now = self._safe_clock()
        for key, cached in tuple(self._cache.items()):
            if cached.expires_at <= now:
                del self._cache[key]

    def _safe_clock(self) -> float:
        now = self._clock()
        if not math.isfinite(now):
            raise ProviderRuntimeCapacityError("provider clock is invalid")
        return now


class PerKeyRateLimiter(Generic[_K]):
    """Bounded no-burst rate limiter using a monotonic schedule per key."""

    def __init__(
        self,
        *,
        calls_per_second: float,
        max_keys: int,
        clock: Callable[[], float],
        sleep: Callable[[float], Awaitable[None]],
        max_queue_delay_seconds: float = 2.0,
    ) -> None:
        if not math.isfinite(calls_per_second) or not 0 < calls_per_second <= 1_000 / 60 or max_keys < 1 or not math.isfinite(max_queue_delay_seconds) or max_queue_delay_seconds <= 0:
            raise ValueError("provider rate limit must be positive")
        self._interval = 1 / calls_per_second
        self._max_keys = max_keys
        self._clock = clock
        self._sleep = sleep
        self._max_queue_delay_seconds = max_queue_delay_seconds
        self._next_allowed: OrderedDict[_K, float] = OrderedDict()
        self._lock = asyncio.Lock()

    async def wait(self, key: _K) -> None:
        async with self._lock:
            now = self._clock()
            if not math.isfinite(now):
                raise ProviderRuntimeCapacityError("provider clock is invalid")
            for stale_key, next_allowed in tuple(self._next_allowed.items()):
                if next_allowed <= now:
                    del self._next_allowed[stale_key]
            scheduled = max(now, self._next_allowed.get(key, now))
            if scheduled - now > self._max_queue_delay_seconds:
                raise ProviderRuntimeCapacityError("provider rate limit queue is full")
            if key not in self._next_allowed and len(self._next_allowed) >= self._max_keys:
                raise ProviderRuntimeCapacityError("provider rate limit key capacity exhausted")
            self._next_allowed[key] = scheduled + self._interval
            self._next_allowed.move_to_end(key)
        delay = scheduled - now
        if delay > 0:
            await self._sleep(delay)


def _consume_task_exception(task: asyncio.Task[object]) -> None:
    """Retrieve orphaned producer failures after every waiter is cancelled."""

    if not task.cancelled():
        task.exception()
