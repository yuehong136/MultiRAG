"""Request log retention: delete rows older than the configured number of days.

The shape follows LiteLLM's spend log cleanup: rows go in bounded batches so
no statement holds locks for long, a run stops at a time budget and picks up
on the next one, and repeated batch failures end the run instead of spinning.
Several data plane processes may run it at once; deleting is idempotent.
"""

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)

BATCH_SIZE = 1000
# One run deletes at most this long, then leaves the rest to the next run.
RUN_BUDGET_SECONDS = 300.0
MAX_CONSECUTIVE_FAILURES = 3
# The first run waits a little so a restart loop does not hammer the table.
STARTUP_DELAY_SECONDS = 60.0


@runtime_checkable
class LogPruner(Protocol):
    async def delete_logs_before(self, cutoff: datetime, limit: int) -> int: ...


def _utc_now() -> datetime:
    return datetime.now(UTC)


class LogCleaner:
    def __init__(
        self,
        pruner: LogPruner,
        *,
        retention_days: int,
        interval_seconds: float,
        clock: Callable[[], datetime] = _utc_now,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if retention_days <= 0:
            raise ValueError("retention_days must be positive")
        self._pruner = pruner
        self._retention = timedelta(days=retention_days)
        self._interval = interval_seconds
        self._clock = clock
        self._monotonic = monotonic

    async def run_once(self) -> int:
        """Delete expired rows until none are left, the budget is spent or batches keep failing."""
        cutoff = self._clock() - self._retention
        deadline = self._monotonic() + RUN_BUDGET_SECONDS
        deleted = failures = 0
        while self._monotonic() < deadline:
            try:
                count = await self._pruner.delete_logs_before(cutoff, BATCH_SIZE)
            except Exception:
                failures += 1
                logger.exception("model gateway: deleting expired request logs failed (%s of %s)", failures, MAX_CONSECUTIVE_FAILURES)
                if failures >= MAX_CONSECUTIVE_FAILURES:
                    break
                continue
            failures = 0
            deleted += count
            if count < BATCH_SIZE:
                break
        if deleted:
            logger.info("model gateway: deleted %s request logs older than %s", deleted, cutoff.isoformat())
        return deleted

    async def run_forever(self) -> None:
        await asyncio.sleep(STARTUP_DELAY_SECONDS)
        while True:
            await self.run_once()
            await asyncio.sleep(self._interval)
