"""Request log retention: batches, the time budget and repeated failures."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from api.model_gateway import retention
from api.model_gateway.retention import BATCH_SIZE, MAX_CONSECUTIVE_FAILURES, LogCleaner
from common.app_config import ModelGatewayConfig

NOW = datetime(2026, 11, 25, 3, 0, tzinfo=UTC)


class Pruner:
    def __init__(self, expired: int, *, fail: int = 0) -> None:
        self.expired = expired
        self.fail = fail
        self.calls: list[tuple[datetime, int]] = []

    async def delete_logs_before(self, cutoff: datetime, limit: int) -> int:
        self.calls.append((cutoff, limit))
        if self.fail:
            self.fail -= 1
            raise RuntimeError("database unavailable")
        count = min(self.expired, limit)
        self.expired -= count
        return count


def _cleaner(pruner: Pruner, **overrides: object) -> LogCleaner:
    return LogCleaner(pruner, retention_days=90, interval_seconds=86400.0, clock=lambda: NOW, **overrides)  # type: ignore[arg-type]


def test_expired_rows_go_in_batches_until_none_are_left():
    pruner = Pruner(2 * BATCH_SIZE + 7)

    assert asyncio.run(_cleaner(pruner).run_once()) == 2 * BATCH_SIZE + 7
    assert pruner.calls == [(NOW - timedelta(days=90), BATCH_SIZE)] * 3


def test_a_full_last_batch_is_followed_by_an_empty_one():
    pruner = Pruner(BATCH_SIZE)

    assert asyncio.run(_cleaner(pruner).run_once()) == BATCH_SIZE
    assert len(pruner.calls) == 2


def test_a_run_stops_at_its_time_budget_and_leaves_the_rest():
    pruner = Pruner(10 * BATCH_SIZE)
    ticks = iter([0.0, 0.0, 100.0, retention.RUN_BUDGET_SECONDS + 1])

    assert asyncio.run(_cleaner(pruner, monotonic=lambda: next(ticks)).run_once()) == 2 * BATCH_SIZE
    assert pruner.expired == 8 * BATCH_SIZE


def test_a_failed_batch_is_retried_and_repeated_failures_end_the_run():
    recovering = Pruner(5, fail=MAX_CONSECUTIVE_FAILURES - 1)
    assert asyncio.run(_cleaner(recovering).run_once()) == 5

    broken = Pruner(5, fail=MAX_CONSECUTIVE_FAILURES)
    assert asyncio.run(_cleaner(broken).run_once()) == 0
    assert len(broken.calls) == MAX_CONSECUTIVE_FAILURES


def test_retention_must_be_off_or_cover_quota_and_overview_windows():
    assert ModelGatewayConfig().log_retention_days == 0
    assert ModelGatewayConfig(log_retention_days=62).log_retention_days == 62
    with pytest.raises(ValidationError):
        ModelGatewayConfig(log_retention_days=30)
    with pytest.raises(ValueError):
        LogCleaner(Pruner(0), retention_days=0, interval_seconds=1.0)
