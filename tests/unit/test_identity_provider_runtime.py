"""Closed concurrency tests for the EIM-I4 provider runtime primitives."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable

import pytest

from api.identity.providers.runtime import (
    AsyncExpiringSingleFlightCache,
    PerKeyRateLimiter,
    ProducedValue,
    ProviderRuntimeCapacityError,
)


class _Clock:
    def __init__(self, value: float = 100.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def _cache(clock: _Clock, *, max_entries: int = 4, max_in_flight: int = 4) -> AsyncExpiringSingleFlightCache[str, str]:
    return AsyncExpiringSingleFlightCache(
        max_entries=max_entries,
        max_in_flight=max_in_flight,
        clock=clock,
    )


async def test_same_key_cold_miss_is_single_flight_and_then_hits_cache() -> None:
    clock = _Clock()
    cache = _cache(clock)
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def produce() -> ProducedValue[str]:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return ProducedValue("token-a", ttl_seconds=30.0)

    first = asyncio.create_task(cache.get_or_create("account-a", produce))
    await entered.wait()
    second = asyncio.create_task(cache.get_or_create("account-a", produce))
    await asyncio.sleep(0)
    release.set()

    assert await first == ("token-a", False)
    assert await second == ("token-a", False)
    assert await cache.get_or_create("account-a", produce) == ("token-a", True)
    assert calls == 1


async def test_cache_expiry_uses_injected_monotonic_clock_and_refreshes_once() -> None:
    clock = _Clock()
    cache = _cache(clock)
    calls = 0

    async def produce() -> ProducedValue[str]:
        nonlocal calls
        calls += 1
        return ProducedValue(f"token-{calls}", ttl_seconds=10.0)

    assert await cache.get_or_create("account-a", produce) == ("token-1", False)
    clock.value = 109.999
    assert await cache.get_or_create("account-a", produce) == ("token-1", True)
    clock.value = 110.0
    assert await cache.get_or_create("account-a", produce) == ("token-2", False)
    assert calls == 2


async def test_different_account_keys_never_share_a_value_or_producer() -> None:
    clock = _Clock()
    cache = _cache(clock)
    calls: list[str] = []

    def producer_for(account: str) -> Awaitable[ProducedValue[str]]:
        async def produce() -> ProducedValue[str]:
            calls.append(account)
            return ProducedValue(f"token-{account}", ttl_seconds=30.0)

        return produce()

    async def account_a() -> ProducedValue[str]:
        return await producer_for("a")

    async def account_b() -> ProducedValue[str]:
        return await producer_for("b")

    assert await asyncio.gather(
        cache.get_or_create("account-a", account_a),
        cache.get_or_create("account-b", account_b),
    ) == [("token-a", False), ("token-b", False)]
    assert sorted(calls) == ["a", "b"]


async def test_cancelled_waiter_does_not_cancel_shared_producer_or_other_waiters() -> None:
    clock = _Clock()
    cache = _cache(clock)
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def produce() -> ProducedValue[str]:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return ProducedValue("token-a", ttl_seconds=30.0)

    cancelled_waiter = asyncio.create_task(cache.get_or_create("account-a", produce))
    await entered.wait()
    surviving_waiter = asyncio.create_task(cache.get_or_create("account-a", produce))
    cancelled_waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled_waiter

    release.set()
    assert await surviving_waiter == ("token-a", False)
    assert await cache.get_or_create("account-a", produce) == ("token-a", True)
    assert calls == 1


async def test_failed_producer_is_not_cached_and_releases_the_in_flight_slot() -> None:
    clock = _Clock()
    cache = _cache(clock, max_in_flight=1)
    calls = 0

    async def produce() -> ProducedValue[str]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("sanitized-upstream-failure")
        return ProducedValue("token-recovered", ttl_seconds=30.0)

    with pytest.raises(RuntimeError, match="sanitized-upstream-failure"):
        await cache.get_or_create("account-a", produce)

    assert await cache.get_or_create("account-a", produce) == ("token-recovered", False)
    assert calls == 2


async def test_distinct_in_flight_keys_are_bounded_without_cancelling_the_admitted_call() -> None:
    clock = _Clock()
    cache = _cache(clock, max_in_flight=1)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow() -> ProducedValue[str]:
        entered.set()
        await release.wait()
        return ProducedValue("token-a", ttl_seconds=30.0)

    async def never_called() -> ProducedValue[str]:
        raise AssertionError("capacity rejection must happen before producer start")

    admitted = asyncio.create_task(cache.get_or_create("account-a", slow))
    await entered.wait()
    with pytest.raises(ProviderRuntimeCapacityError, match="capacity exhausted"):
        await cache.get_or_create("account-b", never_called)

    release.set()
    assert await admitted == ("token-a", False)


async def test_lru_entry_bound_evicts_without_cross_key_value_reuse() -> None:
    clock = _Clock()
    cache = _cache(clock, max_entries=1)
    calls: list[str] = []

    def producer(value: str) -> Awaitable[ProducedValue[str]]:
        async def produce() -> ProducedValue[str]:
            calls.append(value)
            return ProducedValue(value, ttl_seconds=30.0)

        return produce()

    async def token_a() -> ProducedValue[str]:
        return await producer("token-a")

    async def token_b() -> ProducedValue[str]:
        return await producer("token-b")

    assert await cache.get_or_create("account-a", token_a) == ("token-a", False)
    assert await cache.get_or_create("account-b", token_b) == ("token-b", False)
    assert await cache.get_or_create("account-a", token_a) == ("token-a", False)
    assert calls == ["token-a", "token-b", "token-a"]


@pytest.mark.parametrize("ttl", [float("nan"), float("inf"), -1.0, 86_401.0])
async def test_non_finite_negative_or_excessive_ttl_is_never_cached(ttl: float) -> None:
    cache = _cache(_Clock())

    async def produce() -> ProducedValue[str]:
        return ProducedValue("tenant-token-sensitive", ttl_seconds=ttl)

    with pytest.raises(ProviderRuntimeCapacityError, match="cache ttl is invalid"):
        await cache.get_or_create("account-a", produce)
    assert "tenant-token-sensitive" not in repr(ProducedValue("tenant-token-sensitive", 10.0))


async def test_non_finite_clock_fails_closed_before_starting_producer() -> None:
    clock = _Clock(float("nan"))
    cache = _cache(clock)
    called = False

    async def produce() -> ProducedValue[str]:
        nonlocal called
        called = True
        return ProducedValue("token", ttl_seconds=30.0)

    with pytest.raises(ProviderRuntimeCapacityError, match="clock is invalid"):
        await cache.get_or_create("account-a", produce)
    assert called is False


async def test_rate_limiter_is_per_key_and_rejects_an_excessive_queue() -> None:
    clock = _Clock()
    sleeps: list[float] = []

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)

    limiter = PerKeyRateLimiter[str](
        calls_per_second=1.0,
        max_keys=2,
        clock=clock,
        sleep=record_sleep,
        max_queue_delay_seconds=1.5,
    )

    await limiter.wait("account-a")
    await limiter.wait("account-a")
    with pytest.raises(ProviderRuntimeCapacityError, match="queue is full"):
        await limiter.wait("account-a")
    await limiter.wait("account-b")

    assert sleeps == [1.0]


@pytest.mark.parametrize("rate", [float("nan"), float("inf"), 0.0, 17.0])
def test_rate_limiter_rejects_non_finite_or_over_provider_limit(rate: float) -> None:
    with pytest.raises(ValueError, match="rate limit must be positive"):
        PerKeyRateLimiter[str](
            calls_per_second=rate,
            max_keys=1,
            clock=_Clock(),
            sleep=asyncio.sleep,
        )
