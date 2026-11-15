"""In-process admission limits: request rate, token rate, concurrency and quota."""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from api.model_gateway.errors import GatewayError
from api.model_gateway.limits import NO_LIMITS, Limits, MemoryLimiter, Quota, Subject


class Clock:
    def __init__(self) -> None:
        # Five seconds into a minute, in the middle of a month.
        self.now = datetime(2026, 11, 15, 10, 0, 5, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: int) -> None:
        self.now += timedelta(**delta)


class Usage:
    """Stands in for the request-log query that seeds a quota."""

    def __init__(self, used: int = 0) -> None:
        self.used = used
        self.calls: list[tuple[str, datetime]] = []

    async def __call__(self, key_id: str, since: datetime) -> int:
        self.calls.append((key_id, since))
        return self.used


def key(limits: Limits = NO_LIMITS, quota: Quota | None = None, key_id: str = "key-1") -> Subject:
    return Subject("key", key_id, "this API key", limits, quota)


def model(limits: Limits, model_id: str = "model-1") -> Subject:
    return Subject("model", model_id, "the model 'chat-default'", limits)


async def refused(limiter: MemoryLimiter, request_id: str, subjects: list[Subject]) -> GatewayError:
    with pytest.raises(GatewayError) as raised:
        await limiter.admit(request_id, subjects)
    assert raised.value.status_code == 429
    return raised.value


async def test_requests_per_minute_refuses_until_the_window_ends():
    clock = Clock()
    limiter = MemoryLimiter(clock=clock)
    subjects = [key(Limits(rpm=2))]
    await limiter.admit("r1", subjects)
    await limiter.admit("r2", subjects)

    error = await refused(limiter, "r3", subjects)

    assert (error.code, error.error_type) == ("rate_limit_exceeded", "requests")
    assert error.headers == {"retry-after": "55", "x-ratelimit-limit-requests": "2", "x-ratelimit-remaining-requests": "0"}
    assert "this API key" in error.message
    clock.advance(seconds=55)
    await limiter.admit("r4", subjects)


async def test_tokens_per_minute_is_settled_after_the_call_that_crosses_it():
    clock = Clock()
    limiter = MemoryLimiter(clock=clock)
    subjects = [key(Limits(tpm=100))]
    await limiter.admit("r1", subjects)
    await limiter.settle("r1", subjects, 60)
    # 60 < 100: still admitted; this call takes the total past the limit and completes.
    await limiter.admit("r2", subjects)
    await limiter.settle("r2", subjects, 60)

    error = await refused(limiter, "r3", subjects)

    assert (error.code, error.error_type) == ("rate_limit_exceeded", "tokens")
    assert error.headers["x-ratelimit-limit-tokens"] == "100" and error.headers["retry-after"] == "55"
    clock.advance(minutes=1)
    await limiter.admit("r4", subjects)


@pytest.mark.parametrize("tokens", [None, 0])
async def test_calls_with_unknown_usage_do_not_count_towards_token_limits(tokens):
    limiter = MemoryLimiter(clock=Clock())
    subjects = [key(Limits(tpm=1), Quota(1, "day"))]
    for request_id in ("r1", "r2", "r3"):
        await limiter.admit(request_id, subjects)
        await limiter.settle(request_id, subjects, tokens)


async def test_concurrency_slots_are_held_until_settled():
    limiter = MemoryLimiter(clock=Clock())
    subjects = [key(Limits(max_parallel=1))]
    await limiter.admit("r1", subjects)

    error = await refused(limiter, "r2", subjects)

    assert (error.code, error.headers) == ("concurrency_limit_exceeded", {"retry-after": "1"})
    await limiter.settle("r1", subjects, 10)
    await limiter.admit("r2", subjects)
    # Settling twice, or settling a request that was never admitted, frees nothing extra.
    await limiter.settle("r1", subjects, None)
    await limiter.settle("never-admitted", subjects, None)
    await refused(limiter, "r3", subjects)


async def test_quota_is_seeded_from_usage_once_per_period_and_then_counted_here():
    clock, usage = Clock(), Usage(used=90)
    limiter = MemoryLimiter(usage, clock=clock)
    subjects = [key(quota=Quota(100, "day"))]
    await limiter.admit("r1", subjects)
    await limiter.settle("r1", subjects, 20)

    error = await refused(limiter, "r2", subjects)

    assert (error.code, error.error_type) == ("insufficient_quota", "insufficient_quota")
    # Retrying cannot help before the reset, so no retry hint is given.
    assert error.headers == {}
    assert usage.calls == [("key-1", datetime(2026, 11, 15, tzinfo=UTC))]


async def test_quota_resets_at_the_next_period_and_is_seeded_again():
    clock, usage = Clock(), Usage(used=100)
    limiter = MemoryLimiter(usage, clock=clock)
    subjects = [key(quota=Quota(100, "day"))]
    await refused(limiter, "r1", subjects)

    clock.advance(days=1)
    usage.used = 0
    await limiter.admit("r2", subjects)

    assert [since for _, since in usage.calls] == [datetime(2026, 11, 15, tzinfo=UTC), datetime(2026, 11, 16, tzinfo=UTC)]


async def test_tokens_settled_before_the_period_was_seeded_are_left_to_the_logs():
    usage = Usage(used=0)
    limiter = MemoryLimiter(usage, clock=Clock())
    subjects = [key(quota=Quota(10, "month"))]
    # A call admitted by a previous process ends here: nothing is seeded yet.
    await limiter.settle("old-request", subjects, 500)
    assert usage.calls == []

    usage.used = 500
    await refused(limiter, "r1", subjects)

    assert usage.calls == [("key-1", datetime(2026, 11, 1, tzinfo=UTC))]


async def test_quota_without_a_usage_source_starts_from_zero():
    limiter = MemoryLimiter(clock=Clock())
    subjects = [key(quota=Quota(5, "day"))]
    await limiter.admit("r1", subjects)
    await limiter.settle("r1", subjects, 5)

    await refused(limiter, "r2", subjects)


@pytest.mark.parametrize(
    ("period", "now", "start"),
    [
        ("day", datetime(2026, 11, 15, 23, 59, 59, tzinfo=UTC), datetime(2026, 11, 15, tzinfo=UTC)),
        ("month", datetime(2026, 11, 15, 23, 59, 59, tzinfo=UTC), datetime(2026, 11, 1, tzinfo=UTC)),
        # 07:30 at UTC+8 is still the previous UTC day, and here the previous month.
        ("day", datetime(2026, 12, 1, 7, 30, tzinfo=timezone(timedelta(hours=8))), datetime(2026, 11, 30, tzinfo=UTC)),
        ("month", datetime(2026, 12, 1, 7, 30, tzinfo=timezone(timedelta(hours=8))), datetime(2026, 11, 1, tzinfo=UTC)),
    ],
)
def test_quota_periods_are_utc_calendar_periods(period, now, start):
    assert Quota(1, period).period_start(now) == start


async def test_a_refusal_consumes_nothing_from_the_other_subjects():
    limiter = MemoryLimiter(clock=Clock())
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


async def test_model_limit_is_shared_by_keys_while_key_limits_are_separate():
    limiter = MemoryLimiter(clock=Clock())
    shared = model(Limits(rpm=2))
    await limiter.admit("r1", [key(Limits(rpm=1), key_id="key-a"), shared])
    await limiter.admit("r2", [key(Limits(rpm=1), key_id="key-b"), shared])

    assert "this API key" in (await refused(limiter, "r3", [key(Limits(rpm=1), key_id="key-a"), shared])).message
    assert "the model" in (await refused(limiter, "r4", [key(Limits(rpm=1), key_id="key-c"), shared])).message


def test_limits_are_falsy_only_when_nothing_is_set():
    assert not NO_LIMITS
    assert Limits(rpm=1) and Limits(tpm=1) and Limits(max_parallel=1)
