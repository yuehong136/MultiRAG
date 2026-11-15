"""Admission limits of the data plane.

Four limits exist: requests per minute, tokens per minute, concurrent
requests, and a token quota per UTC day or month. They can be set on a key
and, except for the quota, on a published model (shared by all its keys).

Token usage is only known when a call ends, so token limits are settled
afterwards: the call that crosses a limit completes, and the next one is
refused. Calls whose usage is unknown do not count towards token limits.

Counters live in this process. The quota survives restarts because it is
seeded from the request logs; the per-minute windows simply start empty.
Several gateway processes would each count on their own.
"""

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from api.model_gateway import errors

QUOTA_PERIODS = ("day", "month")


@dataclass(frozen=True, slots=True)
class Limits:
    """Per-minute rates and concurrency; ``None`` means unlimited."""

    rpm: int | None = None
    tpm: int | None = None
    max_parallel: int | None = None

    def __bool__(self) -> bool:
        return self.rpm is not None or self.tpm is not None or self.max_parallel is not None


NO_LIMITS = Limits()


@dataclass(frozen=True, slots=True)
class Quota:
    """A token budget that resets at the start of each UTC day or month."""

    tokens: int
    period: str

    def period_start(self, now: datetime) -> datetime:
        start = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        return start.replace(day=1) if self.period == "month" else start


@dataclass(frozen=True, slots=True)
class Subject:
    """One thing being limited: a key, or a published model across all keys."""

    kind: str
    id: str
    label: str
    limits: Limits = NO_LIMITS
    quota: Quota | None = None


@runtime_checkable
class Limiter(Protocol):
    async def admit(self, request_id: str, subjects: Sequence[Subject]) -> None:
        """Admit the request against every subject, or raise a 429 GatewayError."""
        ...

    async def settle(self, request_id: str, subjects: Sequence[Subject], tokens: int | None) -> None:
        """Release the request's concurrency slots and account for its tokens."""
        ...


# (key id, period start) -> tokens already used in that period.
UsageSince = Callable[[str, datetime], Awaitable[int]]
_SubjectKey = tuple[str, str]


def _utc_now() -> datetime:
    return datetime.now(UTC)


class MemoryLimiter:
    """``Limiter`` with counters held in this process, in fixed one-minute windows."""

    def __init__(self, usage_since: UsageSince | None = None, *, clock: Callable[[], datetime] = _utc_now) -> None:
        self._usage_since = usage_since
        self._clock = clock
        # Per subject: (minute, count). An entry for an older minute counts as zero.
        self._requests: dict[_SubjectKey, tuple[int, int]] = {}
        self._tokens: dict[_SubjectKey, tuple[int, int]] = {}
        self._running: dict[_SubjectKey, set[str]] = {}
        # Per key id: (period start, tokens used in that period).
        self._quota_used: dict[str, tuple[datetime, int]] = {}

    async def admit(self, request_id: str, subjects: Sequence[Subject]) -> None:
        now = self._clock()
        for subject in subjects:
            if subject.quota is not None:
                await self._seed_quota(subject.id, subject.quota, now)
        # No awaits from here on: on the event loop the checks and the
        # bookkeeping below are one atomic step, and a refusal changes nothing.
        seconds = int(now.timestamp())
        minute, retry_after = seconds // 60, 60 - seconds % 60
        for subject in subjects:
            key, limits = (subject.kind, subject.id), subject.limits
            if subject.quota is not None and self._quota_used[subject.id][1] >= subject.quota.tokens:
                raise errors.insufficient_quota(subject.quota.tokens, subject.quota.period)
            if limits.tpm is not None and self._window(self._tokens, key, minute) >= limits.tpm:
                raise errors.rate_limit_exceeded(subject.label, "tokens", limits.tpm, retry_after)
            if limits.rpm is not None and self._window(self._requests, key, minute) >= limits.rpm:
                raise errors.rate_limit_exceeded(subject.label, "requests", limits.rpm, retry_after)
            if limits.max_parallel is not None and len(self._running.get(key, ())) >= limits.max_parallel:
                raise errors.concurrency_limit_exceeded(subject.label, limits.max_parallel)
        for subject in subjects:
            key, limits = (subject.kind, subject.id), subject.limits
            if limits.rpm is not None:
                self._requests[key] = (minute, self._window(self._requests, key, minute) + 1)
            if limits.max_parallel is not None:
                self._running.setdefault(key, set()).add(request_id)

    async def settle(self, request_id: str, subjects: Sequence[Subject], tokens: int | None) -> None:
        now = self._clock()
        minute = int(now.timestamp()) // 60
        for subject in subjects:
            key = (subject.kind, subject.id)
            running = self._running.get(key)
            if running is not None:
                running.discard(request_id)
                if not running:
                    del self._running[key]
            if not tokens:
                continue
            if subject.limits.tpm is not None:
                self._tokens[key] = (minute, self._window(self._tokens, key, minute) + tokens)
            if subject.quota is not None:
                # A period that was never seeded picks this call up from the logs later.
                seeded = self._quota_used.get(subject.id)
                if seeded is not None and seeded[0] == subject.quota.period_start(now):
                    self._quota_used[subject.id] = (seeded[0], seeded[1] + tokens)

    async def _seed_quota(self, key_id: str, quota: Quota, now: datetime) -> None:
        start = quota.period_start(now)
        seeded = self._quota_used.get(key_id)
        if seeded is not None and seeded[0] == start:
            return
        used = await self._usage_since(key_id, start) if self._usage_since is not None else 0
        self._quota_used[key_id] = (start, used)

    @staticmethod
    def _window(table: dict[_SubjectKey, tuple[int, int]], key: _SubjectKey, minute: int) -> int:
        entry = table.get(key)
        return entry[1] if entry is not None and entry[0] == minute else 0
