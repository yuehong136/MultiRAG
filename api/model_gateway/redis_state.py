"""Limit counters and cooldowns shared by every data plane process, in Redis.

``RedisLimiter`` enforces the same four limits as ``MemoryLimiter`` with the
same fixed one-minute windows, so two gateway processes count together and a
restart does not empty the windows. One Lua script checks every subject and
only then counts, as LiteLLM's v3 rate limiter does
(``litellm/proxy/hooks/parallel_request_limiter_v3.py``): a refusal changes
nothing, however many processes race.

Redis being unreachable must not stop the gateway. Like LiteLLM, a failing
call falls back to counters in this process until Redis answers again.
"""

import logging
import math
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from redis.asyncio import Redis
from redis.exceptions import RedisError

from api.model_gateway import errors
from api.model_gateway.limits import MemoryLimiter, Quota, Subject, UsageSince
from common.app_config import RedisConfig

logger = logging.getLogger(__name__)

KEY_PREFIX = "model_gateway"
_WINDOW_SECONDS = 60
# A window's counter outlives the window, so a late settlement still lands on it.
_WINDOW_TTL_SECONDS = 2 * _WINDOW_SECONDS
# A quota period is at most a month; the counter is dropped a day after any period ends.
_QUOTA_TTL_SECONDS = 32 * 86400
_UNSET = -1
# Admission waits for Redis on every limited request; a slow Redis must not become a slow gateway.
_CONNECT_TIMEOUT_SECONDS = 1.0
_OPERATION_TIMEOUT_SECONDS = 1.0


def shared_redis(config: RedisConfig) -> Redis:
    """A client for the instance of the ``redis`` section; it connects on first use."""
    host, separator, port = config.host.rpartition(":")
    return Redis(
        host=(host if separator else config.host).removeprefix("[").removesuffix("]"),
        port=int(port) if separator and port.isdigit() else 6379,
        db=config.db,
        username=config.username or None,
        password=config.password or None,
        decode_responses=True,
        socket_connect_timeout=_CONNECT_TIMEOUT_SECONDS,
        socket_timeout=_OPERATION_TIMEOUT_SECONDS,
    )


# KEYS: four per subject — requests, tokens, running, quota.
# ARGV: request id, now, slot expiry, window ttl, slot ttl; then four per subject — rpm, tpm, parallel, quota (-1: none).
_ADMIT = """
local subjects = #KEYS / 4
local now = tonumber(ARGV[2])
for i = 0, subjects - 1 do
    local rpm, tpm = tonumber(ARGV[6 + i * 4]), tonumber(ARGV[7 + i * 4])
    local parallel, quota = tonumber(ARGV[8 + i * 4]), tonumber(ARGV[9 + i * 4])
    if quota >= 0 and tonumber(redis.call('GET', KEYS[i * 4 + 4]) or '0') >= quota then
        return {i, 'quota'}
    end
    if tpm >= 0 and tonumber(redis.call('GET', KEYS[i * 4 + 2]) or '0') >= tpm then
        return {i, 'tokens'}
    end
    if rpm >= 0 and tonumber(redis.call('GET', KEYS[i * 4 + 1]) or '0') >= rpm then
        return {i, 'requests'}
    end
    if parallel >= 0 then
        -- Slots of requests whose process died are given back once they expire.
        redis.call('ZREMRANGEBYSCORE', KEYS[i * 4 + 3], '-inf', now)
        if redis.call('ZCARD', KEYS[i * 4 + 3]) >= parallel then
            return {i, 'parallel'}
        end
    end
end
for i = 0, subjects - 1 do
    if tonumber(ARGV[6 + i * 4]) >= 0 then
        redis.call('INCR', KEYS[i * 4 + 1])
        redis.call('EXPIRE', KEYS[i * 4 + 1], tonumber(ARGV[4]))
    end
    if tonumber(ARGV[8 + i * 4]) >= 0 then
        redis.call('ZADD', KEYS[i * 4 + 3], tonumber(ARGV[3]), ARGV[1])
        redis.call('EXPIRE', KEYS[i * 4 + 3], tonumber(ARGV[5]))
    end
end
return {-1, ''}
"""

# KEYS: three per subject — tokens, running, quota.
# ARGV: request id, tokens, window ttl; then two per subject — counts tokens per minute (1/0), has a quota (1/0).
_SETTLE = """
local tokens = tonumber(ARGV[2])
for i = 0, #KEYS / 3 - 1 do
    redis.call('ZREM', KEYS[i * 3 + 2], ARGV[1])
    if tokens > 0 then
        if ARGV[4 + i * 2] == '1' then
            redis.call('INCRBY', KEYS[i * 3 + 1], tokens)
            redis.call('EXPIRE', KEYS[i * 3 + 1], tonumber(ARGV[3]))
        end
        -- A period nobody seeded picks this call up from the daily sums later.
        if ARGV[5 + i * 2] == '1' and redis.call('EXISTS', KEYS[i * 3 + 3]) == 1 then
            redis.call('INCRBY', KEYS[i * 3 + 3], tokens)
        end
    end
end
return 1
"""


@runtime_checkable
class RedisClient(Protocol):
    """The part of ``redis.asyncio.Redis`` the gateway uses."""

    def register_script(self, script: str) -> Any: ...

    def get(self, name: str) -> Awaitable[Any]: ...

    def set(self, name: str, value: Any, *, ex: int | None = None, nx: bool = False) -> Awaitable[Any]: ...

    def mget(self, keys: Sequence[str]) -> Awaitable[list[Any]]: ...


# Anything a broken connection raises; a script error is a RedisError too.
_UNAVAILABLE = (RedisError, OSError, TimeoutError)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _limit(value: int | None) -> int:
    return value if value is not None else _UNSET


class RedisLimiter:
    """``Limiter`` whose counters live in Redis, with in-process counters as a fallback."""

    def __init__(
        self,
        client: RedisClient,
        usage_since: UsageSince | None = None,
        *,
        slot_ttl_seconds: float,
        clock: Callable[[], datetime] = _utc_now,
        prefix: str = KEY_PREFIX,
    ) -> None:
        self._client = client
        self._usage_since = usage_since
        self._clock = clock
        self._prefix = f"{prefix}:limit"
        self._slot_ttl = max(math.ceil(slot_ttl_seconds), _WINDOW_SECONDS)
        self._admit: Any = client.register_script(_ADMIT)
        self._settle: Any = client.register_script(_SETTLE)
        self._fallback = MemoryLimiter(usage_since, clock=clock)
        # Requests admitted by the fallback are settled there.
        self._local: set[str] = set()
        self._degraded = False

    async def admit(self, request_id: str, subjects: Sequence[Subject]) -> None:
        now = self._clock()
        try:
            refusal = await self._admit_shared(request_id, subjects, now)
        except _UNAVAILABLE:
            self._note_unavailable()
            await self._fallback.admit(request_id, subjects)
            self._local.add(request_id)
            return
        self._note_available()
        if refusal is not None:
            raise refusal

    async def settle(self, request_id: str, subjects: Sequence[Subject], tokens: int | None) -> None:
        if request_id in self._local:
            self._local.discard(request_id)
            await self._fallback.settle(request_id, subjects, tokens)
            return
        now = self._clock()
        minute = int(now.timestamp()) // _WINDOW_SECONDS
        keys: list[str] = []
        flags: list[str] = []
        for subject in subjects:
            keys += [self._key(subject, "tokens", minute), self._key(subject, "running"), self._quota_key(subject, now)]
            flags += ["1" if subject.limits.tpm is not None else "0", "1" if subject.quota is not None else "0"]
        try:
            await self._settle(keys=keys, args=[request_id, tokens or 0, _WINDOW_TTL_SECONDS, *flags])
        except _UNAVAILABLE:
            # The slot expires on its own; the tokens of this call are lost to the minute window only.
            self._note_unavailable()

    async def _admit_shared(self, request_id: str, subjects: Sequence[Subject], now: datetime) -> errors.GatewayError | None:
        for subject in subjects:
            if subject.quota is not None:
                await self._seed_quota(subject, subject.quota, now)
        seconds = int(now.timestamp())
        minute, retry_after = seconds // _WINDOW_SECONDS, _WINDOW_SECONDS - seconds % _WINDOW_SECONDS
        keys: list[str] = []
        limits: list[int] = []
        for subject in subjects:
            keys += [self._key(subject, "requests", minute), self._key(subject, "tokens", minute), self._key(subject, "running"), self._quota_key(subject, now)]
            limits += [_limit(subject.limits.rpm), _limit(subject.limits.tpm), _limit(subject.limits.max_parallel), subject.quota.tokens if subject.quota is not None else _UNSET]
        index, reason = await self._admit(keys=keys, args=[request_id, seconds, seconds + self._slot_ttl, _WINDOW_TTL_SECONDS, self._slot_ttl, *limits])
        index, reason = int(index), reason.decode() if isinstance(reason, bytes) else str(reason)
        if index < 0:
            return None
        subject = subjects[index]
        if reason == "quota":
            assert subject.quota is not None
            return errors.insufficient_quota(subject.quota.tokens, subject.quota.period)
        if reason == "tokens":
            assert subject.limits.tpm is not None
            return errors.rate_limit_exceeded(subject.label, "tokens", subject.limits.tpm, retry_after)
        if reason == "requests":
            assert subject.limits.rpm is not None
            return errors.rate_limit_exceeded(subject.label, "requests", subject.limits.rpm, retry_after)
        assert subject.limits.max_parallel is not None
        return errors.concurrency_limit_exceeded(subject.label, subject.limits.max_parallel)

    async def _seed_quota(self, subject: Subject, quota: Quota, now: datetime) -> None:
        key = self._quota_key(subject, now)
        if await self._client.get(key) is not None:
            return
        used = await self._usage_since(subject.id, quota.period_start(now)) if self._usage_since is not None else 0
        # Another process may have seeded it meanwhile; its count already includes later calls.
        await self._client.set(key, used, ex=_QUOTA_TTL_SECONDS, nx=True)

    def _key(self, subject: Subject, counter: str, minute: int | None = None) -> str:
        base = f"{self._prefix}:{subject.kind}:{subject.id}:{counter}"
        return base if minute is None else f"{base}:{minute}"

    def _quota_key(self, subject: Subject, now: datetime) -> str:
        start = int(subject.quota.period_start(now).timestamp()) if subject.quota is not None else 0
        return f"{self._prefix}:{subject.kind}:{subject.id}:quota:{start}"

    def _note_unavailable(self) -> None:
        if not self._degraded:
            self._degraded = True
            logger.warning("model gateway: Redis is unavailable; limits are counted in this process until it answers again", exc_info=True)

    def _note_available(self) -> None:
        if self._degraded:
            self._degraded = False
            logger.warning("model gateway: Redis answers again; limits are shared between processes")


class RedisCooldowns:
    """Tells every data plane process which deployments are resting.

    Only the cooldown itself is shared. Each process still decides from its own
    recent calls when a deployment should rest, as each LiteLLM instance does.
    """

    def __init__(self, client: RedisClient, *, prefix: str = KEY_PREFIX) -> None:
        self._client = client
        self._prefix = f"{prefix}:cooldown"

    async def mark(self, model_id: str, seconds: float) -> None:
        """Rest a deployment everywhere for ``seconds``."""
        if seconds <= 0:
            return
        try:
            await self._client.set(f"{self._prefix}:{model_id}", "1", ex=max(math.ceil(seconds), 1))
        except _UNAVAILABLE:
            logger.debug("model gateway: could not share a cooldown", exc_info=True)

    async def resting(self, model_ids: Sequence[str]) -> set[str]:
        """Which of these deployments another process (or this one) put to rest."""
        if not model_ids:
            return set()
        try:
            values = await self._client.mget([f"{self._prefix}:{model_id}" for model_id in model_ids])
        except _UNAVAILABLE:
            return set()
        return {model_id for model_id, value in zip(model_ids, values, strict=True) if value is not None}
