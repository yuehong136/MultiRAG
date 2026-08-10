"""Bounded API-side garbage collection for private Channel candidates.

Canvas generation still requires a database-backed working session.  Its
metadata is explicit, while strict legacy markers are retained only so a
bounded background sweep can remove candidates left by older MultiRAG builds.
The worker is process-local; PostgreSQL row locking provides multi-API-instance
coordination without involving Provider workers or Redis.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any, Protocol, cast, runtime_checkable

from sqlalchemy import BigInteger, delete, exists, func, select
from sqlalchemy import cast as sql_cast
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from api.channel_execution.candidate_metadata import (
    CANVAS_CANDIDATE_LEGACY_NAME,
    CANVAS_CANDIDATE_STATE_ACTIVE,
    CANVAS_CANDIDATE_STATE_FINALIZING,
)
from api.db.db_models import API4Conversation, ChannelCanvasCandidate, Conversation, async_session_factory
from common.app_config import ChannelCandidateGCConfig, get_app_config

LOGGER = logging.getLogger(__name__)

_COLLECTIBLE_EXPLICIT_STATES = (
    CANVAS_CANDIDATE_STATE_ACTIVE,
    CANVAS_CANDIDATE_STATE_FINALIZING,
)


@runtime_checkable
class CandidateGCSessionFactory(Protocol):
    """Create one task-local asynchronous session per cleanup batch."""

    def __call__(self) -> AbstractAsyncContextManager[AsyncSession]: ...


@runtime_checkable
class CandidateBatchCollector(Protocol):
    """Delete at most one configured batch inside one short transaction."""

    async def __call__(
        self,
        db: AsyncSession,
        *,
        max_age_seconds: int,
        batch_size: int,
    ) -> CandidateCleanupBatch: ...


@dataclass(frozen=True, slots=True)
class CandidateCleanupBatch:
    """Counts from one committed cleanup transaction."""

    explicit_canvas: int = 0
    legacy_canvas: int = 0
    legacy_dialog: int = 0

    @property
    def total(self) -> int:
        return self.explicit_canvas + self.legacy_canvas + self.legacy_dialog


@dataclass(frozen=True, slots=True)
class CandidateCleanupCycle:
    """Bounded aggregate emitted once per periodic worker cycle."""

    explicit_canvas: int
    legacy_canvas: int
    legacy_dialog: int
    batches: int
    has_more: bool

    @property
    def total(self) -> int:
        return self.explicit_canvas + self.legacy_canvas + self.legacy_dialog


async def _delete_candidate_rows(
    db: AsyncSession,
    model: type[API4Conversation] | type[Conversation],
    candidate_ids: list[str],
) -> int:
    if not candidate_ids:
        return 0
    result = await db.execute(delete(model).where(model.id.in_(candidate_ids)).execution_options(synchronize_session=False))
    return int(result.rowcount or 0)


async def _lock_explicit_canvas_candidate_ids(
    db: AsyncSession,
    *,
    limit: int,
) -> list[str]:
    if limit <= 0:
        return []
    statement = (
        select(API4Conversation.id)
        .select_from(API4Conversation)
        .join(
            ChannelCanvasCandidate,
            ChannelCanvasCandidate.candidate_session_id == API4Conversation.id,
        )
        .where(
            ChannelCanvasCandidate.state.in_(_COLLECTIBLE_EXPLICIT_STATES),
            ChannelCanvasCandidate.expires_at <= func.now(),
        )
        .order_by(
            ChannelCanvasCandidate.expires_at,
            ChannelCanvasCandidate.candidate_session_id,
        )
        .limit(limit)
        # Candidate is the lock anchor.  Deleting it cascades metadata; locking
        # metadata first would invert the terminal promotion/delete lock order.
        .with_for_update(of=API4Conversation, skip_locked=True)
    )
    return [str(candidate_id) for candidate_id in (await db.scalars(statement)).all()]


def _legacy_cutoff_ms(max_age_seconds: int) -> ColumnElement[Any]:
    database_now_ms = sql_cast(func.extract("epoch", func.now()) * 1_000, BigInteger)
    return database_now_ms - max_age_seconds * 1_000


async def _lock_legacy_canvas_candidate_ids(
    db: AsyncSession,
    *,
    max_age_seconds: int,
    limit: int,
) -> list[str]:
    if limit <= 0:
        return []
    has_explicit_metadata = exists(select(ChannelCanvasCandidate.id).where(ChannelCanvasCandidate.candidate_session_id == API4Conversation.id))
    statement = (
        select(API4Conversation.id)
        .where(
            API4Conversation.name == CANVAS_CANDIDATE_LEGACY_NAME,
            API4Conversation.user_id == API4Conversation.id,
            API4Conversation.exp_user_id == API4Conversation.id,
            API4Conversation.update_time < _legacy_cutoff_ms(max_age_seconds),
            ~has_explicit_metadata,
        )
        .order_by(API4Conversation.update_time, API4Conversation.id)
        .limit(limit)
        .with_for_update(of=API4Conversation, skip_locked=True)
    )
    return [str(candidate_id) for candidate_id in (await db.scalars(statement)).all()]


async def _lock_legacy_dialog_candidate_ids(
    db: AsyncSession,
    *,
    max_age_seconds: int,
    limit: int,
) -> list[str]:
    if limit <= 0:
        return []
    statement = (
        select(Conversation.id)
        .where(
            Conversation.name == CANVAS_CANDIDATE_LEGACY_NAME,
            Conversation.user_id == Conversation.id,
            Conversation.update_time < _legacy_cutoff_ms(max_age_seconds),
        )
        .order_by(Conversation.update_time, Conversation.id)
        .limit(limit)
        .with_for_update(of=Conversation, skip_locked=True)
    )
    return [str(candidate_id) for candidate_id in (await db.scalars(statement)).all()]


async def collect_expired_candidate_batch(
    db: AsyncSession,
    *,
    max_age_seconds: int,
    batch_size: int,
) -> CandidateCleanupBatch:
    """Delete one candidate-first, strictly bounded batch.

    The three categories share one budget.  Each selected candidate row is
    locked with ``SKIP LOCKED`` until deletion commits, so API replicas process
    disjoint rows and never wait behind an active terminal promotion.
    """

    if max_age_seconds <= 0 or batch_size <= 0:
        raise ValueError("candidate cleanup limits must be positive")

    async with db.begin():
        remaining = batch_size

        explicit_ids = await _lock_explicit_canvas_candidate_ids(db, limit=remaining)
        explicit_canvas = await _delete_candidate_rows(db, API4Conversation, explicit_ids)
        remaining -= explicit_canvas

        legacy_canvas_ids = await _lock_legacy_canvas_candidate_ids(
            db,
            max_age_seconds=max_age_seconds,
            limit=remaining,
        )
        legacy_canvas = await _delete_candidate_rows(db, API4Conversation, legacy_canvas_ids)
        remaining -= legacy_canvas

        legacy_dialog_ids = await _lock_legacy_dialog_candidate_ids(
            db,
            max_age_seconds=max_age_seconds,
            limit=remaining,
        )
        legacy_dialog = await _delete_candidate_rows(db, Conversation, legacy_dialog_ids)

    return CandidateCleanupBatch(
        explicit_canvas=explicit_canvas,
        legacy_canvas=legacy_canvas,
        legacy_dialog=legacy_dialog,
    )


class ChannelCandidateGCWorker:
    """Run startup and periodic cleanup without holding a long-lived session."""

    def __init__(
        self,
        session_factory: CandidateGCSessionFactory,
        config: ChannelCandidateGCConfig,
        *,
        collector: CandidateBatchCollector = collect_expired_candidate_batch,
        jitter: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self._session_factory = session_factory
        self._config = config
        self._collector = collector
        self._jitter = jitter

    async def collect_cycle(self) -> CandidateCleanupCycle:
        explicit_canvas = 0
        legacy_canvas = 0
        legacy_dialog = 0
        batches = 0
        has_more = False

        for _round in range(self._config.max_batches_per_cycle):
            async with self._session_factory() as db:
                batch = await self._collector(
                    db,
                    max_age_seconds=self._config.max_age_seconds,
                    batch_size=self._config.batch_size,
                )
            batches += 1
            explicit_canvas += batch.explicit_canvas
            legacy_canvas += batch.legacy_canvas
            legacy_dialog += batch.legacy_dialog
            has_more = batch.total == self._config.batch_size
            if not has_more:
                break
            await asyncio.sleep(0)

        return CandidateCleanupCycle(
            explicit_canvas=explicit_canvas,
            legacy_canvas=legacy_canvas,
            legacy_dialog=legacy_dialog,
            batches=batches,
            has_more=has_more,
        )

    def _next_delay(self) -> float:
        interval = float(self._config.interval_seconds)
        spread = interval * self._config.jitter_ratio
        return float(self._jitter(interval - spread, interval + spread))

    async def run(self, stop_event: asyncio.Event) -> None:
        """Run immediately at startup, then wait with cancellation-aware jitter."""

        while not stop_event.is_set():
            started_at = time.monotonic()
            try:
                result = await self.collect_cycle()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOGGER.exception(
                    "channel_execution_event=candidate_gc_cycle result=failed error_code=CANDIDATE_GC_FAILED error_type=%s elapsed_ms=%s",
                    type(exc).__name__,
                    round((time.monotonic() - started_at) * 1_000),
                )
            else:
                LOGGER.info(
                    "channel_execution_event=candidate_gc_cycle result=ok error_code= explicit_canvas=%s legacy_canvas=%s legacy_dialog=%s batches=%s has_more=%s elapsed_ms=%s",
                    result.explicit_canvas,
                    result.legacy_canvas,
                    result.legacy_dialog,
                    result.batches,
                    str(result.has_more).lower(),
                    round((time.monotonic() - started_at) * 1_000),
                )

            if stop_event.is_set():
                return
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self._next_delay())
            except TimeoutError:
                pass


def build_channel_candidate_gc_worker() -> ChannelCandidateGCWorker | None:
    """Build the configured API-process worker, or explicitly disable it."""

    config = get_app_config().channels.execution.candidate_gc
    if not config.enabled:
        LOGGER.info(
            "channel_execution_event=candidate_gc_start result=disabled error_code=",
        )
        return None
    if async_session_factory is None:
        LOGGER.warning(
            "channel_execution_event=candidate_gc_start result=disabled error_code=ASYNC_DATABASE_UNAVAILABLE",
        )
        return None
    return ChannelCandidateGCWorker(
        cast(CandidateGCSessionFactory, async_session_factory),
        config,
    )
