"""Shared limit counters and cooldowns against the local Redis.

Two limiters over one Redis stand in for two data plane processes. Every test
works under its own key prefix and removes it afterwards.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from redis.asyncio import Redis

from api.model_gateway.errors import GatewayError
from api.model_gateway.limits import NO_LIMITS, Limits, Quota, Subject
from api.model_gateway.redis_state import RedisCooldowns, RedisLimiter, shared_redis
from common.app_config import get_app_config


class Clock:
    def __init__(self) -> None:
        # Five seconds into a minute, in the middle of a month.
        self.now = datetime(2026, 11, 15, 10, 0, 5, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: int) -> None:
        self.now += timedelta(**delta)


class Usage:
    def __init__(self, used: int = 0) -> None:
        self.used = used
        self.calls: list[tuple[str, datetime]] = []

    async def __call__(self, key_id: str, since: datetime) -> int:
        self.calls.append((key_id, since))
        return self.used


class Processes:
    """Limiters that share one Redis and one clock, as gateway processes share wall time."""

    def __init__(self, redis: Redis, prefix: str) -> None:
        self.redis, self.prefix, self.clock = redis, prefix, Clock()

    def limiter(self, usage: Usage | None = None, *, slot_ttl_seconds: float = 3660.0) -> RedisLimiter:
        return RedisLimiter(self.redis, usage, slot_ttl_seconds=slot_ttl_seconds, clock=self.clock, prefix=self.prefix)


@pytest.fixture
async def processes() -> AsyncIterator[Processes]:
    redis = shared_redis(get_app_config().redis)
    prefix = "model_gateway_test_" + uuid.uuid4().hex[:12]
    try:
        await redis.ping()
    except Exception as error:
        await redis.aclose()
        pytest.skip(f"Redis is not reachable: {error}")
    try:
        yield Processes(redis, prefix)
    finally:
        keys = [key async for key in redis.scan_iter(match=f"{prefix}:*")]
        if keys:
            await redis.delete(*keys)
        await redis.aclose()


def key(limits: Limits = NO_LIMITS, quota: Quota | None = None, key_id: str = "key-1") -> Subject:
    return Subject("key", key_id, "this API key", limits, quota)


def model(limits: Limits, model_id: str = "model-1") -> Subject:
    return Subject("model", model_id, "the model 'chat-default'", limits)


async def refused(limiter: RedisLimiter, request_id: str, subjects: list[Subject]) -> GatewayError:
    with pytest.raises(GatewayError) as raised:
        await limiter.admit(request_id, subjects)
    assert raised.value.status_code == 429
    return raised.value


async def test_two_processes_share_one_request_window(processes: Processes) -> None:
    first, second = processes.limiter(), processes.limiter()
    subjects = [key(Limits(rpm=2))]
    await first.admit("r1", subjects)
    await second.admit("r2", subjects)

    error = await refused(first, "r3", subjects)

    assert (error.code, error.error_type) == ("rate_limit_exceeded", "requests")
    assert error.headers == {"retry-after": "55", "x-ratelimit-limit-requests": "2", "x-ratelimit-remaining-requests": "0"}
    await refused(second, "r4", subjects)
    processes.clock.advance(seconds=55)
    await second.admit("r5", subjects)


async def test_a_restarted_process_finds_the_window_as_it_left_it(processes: Processes) -> None:
    subjects = [key(Limits(rpm=1))]
    await processes.limiter().admit("r1", subjects)

    # A new limiter is what a restart amounts to.
    await refused(processes.limiter(), "r2", subjects)


async def test_tokens_are_settled_into_the_shared_window_after_the_call(processes: Processes) -> None:
    first, second = processes.limiter(), processes.limiter()
    subjects = [key(Limits(tpm=100))]
    await first.admit("r1", subjects)
    await first.settle("r1", subjects, 60)
    # 60 < 100: still admitted; this call takes the total past the limit and completes.
    await second.admit("r2", subjects)
    await second.settle("r2", subjects, 60)

    error = await refused(first, "r3", subjects)

    assert (error.code, error.error_type) == ("rate_limit_exceeded", "tokens")
    assert error.headers["x-ratelimit-limit-tokens"] == "100" and error.headers["retry-after"] == "55"
    processes.clock.advance(minutes=1)
    await first.admit("r4", subjects)


@pytest.mark.parametrize("tokens", [None, 0])
async def test_calls_with_unknown_usage_do_not_count(processes: Processes, tokens: int | None) -> None:
    limiter = processes.limiter()
    subjects = [key(Limits(tpm=1), Quota(1, "day"))]
    for request_id in ("r1", "r2", "r3"):
        await limiter.admit(request_id, subjects)
        await limiter.settle(request_id, subjects, tokens)


async def test_a_concurrency_slot_taken_in_one_process_is_given_back_in_another(processes: Processes) -> None:
    first, second = processes.limiter(), processes.limiter()
    subjects = [key(Limits(max_parallel=1))]
    await first.admit("r1", subjects)

    error = await refused(second, "r2", subjects)

    assert (error.code, error.headers) == ("concurrency_limit_exceeded", {"retry-after": "1"})
    await second.settle("r1", subjects, 10)
    await second.admit("r2", subjects)
    # Settling twice, or settling a request that was never admitted, frees nothing extra.
    await first.settle("r1", subjects, None)
    await first.settle("never-admitted", subjects, None)
    await refused(first, "r3", subjects)


async def test_a_slot_whose_process_died_is_free_again_after_its_time(processes: Processes) -> None:
    limiter = processes.limiter(slot_ttl_seconds=120.0)
    subjects = [key(Limits(max_parallel=1))]
    await limiter.admit("abandoned", subjects)
    processes.clock.advance(seconds=119)
    await refused(limiter, "r2", subjects)

    processes.clock.advance(seconds=2)

    await limiter.admit("r3", subjects)


async def test_a_quota_is_seeded_once_and_counted_by_every_process(processes: Processes) -> None:
    usage = Usage(used=90)
    first, second = processes.limiter(usage), processes.limiter(usage)
    subjects = [key(quota=Quota(100, "day"))]
    await first.admit("r1", subjects)
    await first.settle("r1", subjects, 20)

    error = await refused(second, "r2", subjects)

    assert (error.code, error.error_type, error.headers) == ("insufficient_quota", "insufficient_quota", {})
    # The second process found the count in Redis and did not read the logs again.
    assert usage.calls == [("key-1", datetime(2026, 11, 15, tzinfo=UTC))]


async def test_a_quota_starts_again_in_the_next_period(processes: Processes) -> None:
    usage = Usage(used=100)
    limiter = processes.limiter(usage)
    subjects = [key(quota=Quota(100, "day"))]
    await refused(limiter, "r1", subjects)

    processes.clock.advance(days=1)
    usage.used = 0

    await limiter.admit("r2", subjects)
    assert usage.calls[-1] == ("key-1", datetime(2026, 11, 16, tzinfo=UTC))


async def test_a_refusal_consumes_nothing_from_the_other_subjects(processes: Processes) -> None:
    limiter = processes.limiter()
    limited_key, busy_model = key(Limits(rpm=2, max_parallel=5)), model(Limits(max_parallel=1))
    await limiter.admit("holder", [busy_model])
    for attempt in range(5):
        error = await refused(limiter, f"blocked-{attempt}", [limited_key, busy_model])
        assert "the model 'chat-default'" in error.message

    await limiter.settle("holder", [busy_model], None)

    # The key's two requests per minute are all still available.
    await limiter.admit("r1", [limited_key, busy_model])
    await limiter.settle("r1", [limited_key, busy_model], None)
    await limiter.admit("r2", [limited_key, busy_model])
    await limiter.settle("r2", [limited_key, busy_model], None)
    assert (await refused(limiter, "r3", [limited_key, busy_model])).error_type == "requests"


async def test_a_model_limit_is_shared_by_keys_while_key_limits_are_separate(processes: Processes) -> None:
    limiter = processes.limiter()
    shared = model(Limits(rpm=2))
    await limiter.admit("r1", [key(Limits(rpm=1), key_id="key-a"), shared])
    await limiter.admit("r2", [key(Limits(rpm=1), key_id="key-b"), shared])

    assert "this API key" in (await refused(limiter, "r3", [key(Limits(rpm=1), key_id="key-a"), shared])).message
    assert "the model" in (await refused(limiter, "r4", [key(Limits(rpm=1), key_id="key-c"), shared])).message


async def test_a_cooldown_marked_by_one_process_is_seen_by_the_others(processes: Processes) -> None:
    first = RedisCooldowns(processes.redis, prefix=processes.prefix)
    second = RedisCooldowns(processes.redis, prefix=processes.prefix)

    await first.mark("model-a", 5.0)
    await first.mark("model-off", 0.0)

    assert await second.resting(["model-a", "model-b", "model-off"]) == {"model-a"}
    assert await second.resting([]) == set()
    assert 0 < await processes.redis.ttl(f"{processes.prefix}:cooldown:model-a") <= 5
