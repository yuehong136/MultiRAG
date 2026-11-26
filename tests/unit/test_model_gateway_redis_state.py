"""What the gateway does when Redis does not answer; the counting itself is tested against Redis."""

from datetime import UTC, datetime
from typing import Any

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from api.model_gateway.errors import GatewayError
from api.model_gateway.limits import Limits, Subject
from api.model_gateway.redis_state import RedisCooldowns, RedisLimiter, shared_redis
from common.app_config import RedisConfig


class DownRedis:
    """A client whose every call fails, like a Redis that cannot be reached."""

    def __init__(self) -> None:
        self.down = True
        self.calls = 0

    def register_script(self, script: str) -> Any:
        async def run(keys: Any, args: Any) -> Any:
            return await self._answer([-1, ""])

        return run

    async def get(self, name: str) -> Any:
        return await self._answer(None)

    async def set(self, name: str, value: Any, *, ex: int | None = None, nx: bool = False) -> Any:
        return await self._answer(True)

    async def mget(self, keys: Any) -> Any:
        return await self._answer([None for _ in keys])

    async def _answer(self, value: Any) -> Any:
        self.calls += 1
        if self.down:
            raise RedisConnectionError("connection refused")
        return value


def _limiter(redis: DownRedis) -> RedisLimiter:
    return RedisLimiter(redis, slot_ttl_seconds=3660.0, clock=lambda: datetime(2026, 11, 15, 10, 0, 5, tzinfo=UTC))


def _key(limits: Limits) -> Subject:
    return Subject("key", "key-1", "this API key", limits)


async def test_limits_are_counted_in_this_process_while_redis_is_down():
    limiter = _limiter(DownRedis())
    subjects = [_key(Limits(rpm=1, max_parallel=1))]

    await limiter.admit("r1", subjects)
    with pytest.raises(GatewayError) as busy:
        await limiter.admit("r2", subjects)
    assert busy.value.code == "rate_limit_exceeded"


async def test_a_call_admitted_here_is_settled_here_even_after_redis_returns():
    redis = DownRedis()
    limiter = _limiter(redis)
    subjects = [_key(Limits(max_parallel=1))]
    await limiter.admit("r1", subjects)
    redis.down = False
    before = redis.calls

    await limiter.settle("r1", subjects, 10)

    # The slot was held in this process, so Redis has nothing to release.
    assert redis.calls == before
    redis.down = True
    await limiter.admit("r2", subjects)


async def test_settling_does_not_fail_when_redis_went_away_mid_call():
    redis = DownRedis()
    redis.down = False
    limiter = _limiter(redis)
    subjects = [_key(Limits(tpm=10))]
    await limiter.admit("r1", subjects)
    redis.down = True

    await limiter.settle("r1", subjects, 5)


async def test_cooldowns_are_simply_not_shared_while_redis_is_down():
    cooldowns = RedisCooldowns(DownRedis())

    await cooldowns.mark("model-a", 5.0)

    assert await cooldowns.resting(["model-a"]) == set()


@pytest.mark.parametrize(("host", "expected"), [("127.0.0.1:6380", ("127.0.0.1", 6380)), ("redis", ("redis", 6379)), ("[::1]:6379", ("::1", 6379))])
def test_the_redis_section_address_is_read_as_host_and_port(host, expected):
    client = shared_redis(RedisConfig(host=host, db=3))

    kwargs = client.connection_pool.connection_kwargs
    assert (kwargs["host"], kwargs["port"], kwargs["db"]) == (*expected, 3)
