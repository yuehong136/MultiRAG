"""API lifecycle and bounded-loop contracts for Channel candidate cleanup."""

from __future__ import annotations

import asyncio
import logging
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import cast

import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from api.channel_execution.candidate_gc import (
    CandidateCleanupBatch,
    ChannelCandidateGCWorker,
)
from api.channels.telemetry import (
    ChannelMetric,
    ChannelReason,
    ChannelResult,
    InMemoryChannelTelemetry,
)
from common.app_config import ChannelCandidateGCConfig


class _FakeSessionFactory:
    def __init__(self) -> None:
        self.created: list[object] = []
        self.closed: list[object] = []

    def __call__(self) -> AbstractAsyncContextManager[AsyncSession]:
        token = object()
        self.created.append(token)

        @asynccontextmanager
        async def context() -> AsyncSession:
            try:
                yield cast(AsyncSession, token)
            finally:
                self.closed.append(token)

        return context()


def _config(**overrides: object) -> ChannelCandidateGCConfig:
    return ChannelCandidateGCConfig.model_validate(
        {
            "interval_seconds": 10,
            "max_age_seconds": 300,
            "batch_size": 2,
            "max_batches_per_cycle": 2,
            "jitter_ratio": 0,
            **overrides,
        }
    )


async def test_cycle_uses_one_session_per_batch_and_respects_round_cap() -> None:
    sessions = _FakeSessionFactory()
    seen_sessions: list[AsyncSession] = []

    async def collect(
        db: AsyncSession,
        *,
        max_age_seconds: int,
        batch_size: int,
    ) -> CandidateCleanupBatch:
        seen_sessions.append(db)
        assert max_age_seconds == 300
        assert batch_size == 2
        return CandidateCleanupBatch(explicit_canvas=2)

    worker = ChannelCandidateGCWorker(sessions, _config(), collector=collect)

    result = await worker.collect_cycle()

    assert result.explicit_canvas == 4
    assert result.batches == 2
    assert result.has_more is True
    assert len(seen_sessions) == 2
    assert seen_sessions[0] is not seen_sessions[1]
    assert sessions.closed == sessions.created


async def test_cycle_stops_after_a_partial_batch() -> None:
    sessions = _FakeSessionFactory()

    async def collect(
        _db: AsyncSession,
        *,
        max_age_seconds: int,
        batch_size: int,
    ) -> CandidateCleanupBatch:
        del max_age_seconds, batch_size
        return CandidateCleanupBatch(legacy_dialog=1)

    worker = ChannelCandidateGCWorker(sessions, _config(), collector=collect)

    result = await worker.collect_cycle()

    assert result.legacy_dialog == 1
    assert result.batches == 1
    assert result.has_more is False
    assert len(sessions.created) == 1


async def test_run_logs_cycle_failure_and_continues(caplog: pytest.LogCaptureFixture) -> None:
    sessions = _FakeSessionFactory()
    stop_event = asyncio.Event()
    calls = 0
    telemetry = InMemoryChannelTelemetry()

    async def collect(
        _db: AsyncSession,
        *,
        max_age_seconds: int,
        batch_size: int,
    ) -> CandidateCleanupBatch:
        nonlocal calls
        del max_age_seconds, batch_size
        calls += 1
        if calls == 1:
            raise RuntimeError("database unavailable")
        stop_event.set()
        return CandidateCleanupBatch(explicit_canvas=1)

    worker = ChannelCandidateGCWorker(
        sessions,
        _config(max_batches_per_cycle=1),
        collector=collect,
        jitter=lambda _lower, _upper: 0,
        telemetry=telemetry,
    )
    caplog.set_level(logging.INFO)

    await worker.run(stop_event)

    assert calls == 2
    assert "candidate_gc_cycle result=failed" in caplog.text
    assert "database unavailable" in caplog.text
    assert sessions.closed == sessions.created
    cycles = [event for event in telemetry.snapshot().recent_events if event.metric is ChannelMetric.CANDIDATE_GC_CYCLES_TOTAL]
    assert [(event.labels.result, event.labels.reason) for event in cycles] == [
        (ChannelResult.FAILED, ChannelReason.CANDIDATE_GC_FAILURE),
        (ChannelResult.OK, ChannelReason.NONE),
    ]
    deleted = [event for event in telemetry.snapshot().recent_events if event.metric is ChannelMetric.CANDIDATE_GC_DELETED_TOTAL]
    assert [event.value for event in deleted] == [1]


async def test_run_propagates_cancellation_and_closes_batch_session() -> None:
    sessions = _FakeSessionFactory()
    started = asyncio.Event()
    release = asyncio.Event()

    async def collect(
        _db: AsyncSession,
        *,
        max_age_seconds: int,
        batch_size: int,
    ) -> CandidateCleanupBatch:
        del max_age_seconds, batch_size
        started.set()
        await release.wait()
        return CandidateCleanupBatch()

    worker = ChannelCandidateGCWorker(
        sessions,
        _config(max_batches_per_cycle=1),
        collector=collect,
    )
    task = asyncio.create_task(worker.run(asyncio.Event()))
    await started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert sessions.closed == sessions.created


async def test_router_lifespan_starts_one_worker_and_cancels_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from api.apps.restful_apis import channel_execution_api

    started = asyncio.Event()
    stopped = asyncio.Event()
    cancelled = asyncio.Event()
    build_calls = 0

    class FakeWorker:
        async def run(self, stop_event: asyncio.Event) -> None:
            started.set()
            try:
                await stop_event.wait()
            except asyncio.CancelledError:
                assert stop_event.is_set()
                cancelled.set()
                raise
            finally:
                stopped.set()

    fake_worker = FakeWorker()

    def build() -> ChannelCandidateGCWorker:
        nonlocal build_calls
        build_calls += 1
        return cast(ChannelCandidateGCWorker, fake_worker)

    monkeypatch.setattr(channel_execution_api, "build_channel_candidate_gc_worker", build)
    app = FastAPI()
    # Including the component twice must not create two database sweepers.
    app.include_router(channel_execution_api.router)
    app.include_router(channel_execution_api.router)

    async with app.router.lifespan_context(app):
        await started.wait()
        assert build_calls == 1
        handle = getattr(app.state, channel_execution_api._CANDIDATE_GC_STATE_KEY)
        assert handle.task.done() is False

    assert stopped.is_set()
    assert cancelled.is_set()
    assert not hasattr(app.state, channel_execution_api._CANDIDATE_GC_STATE_KEY)


async def test_router_lifespan_reports_an_unexpected_worker_exit(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from api.apps.restful_apis import channel_execution_api

    class FakeWorker:
        async def run(self, _stop_event: asyncio.Event) -> None:
            return

    monkeypatch.setattr(
        channel_execution_api,
        "build_channel_candidate_gc_worker",
        lambda: cast(ChannelCandidateGCWorker, FakeWorker()),
    )
    caplog.set_level(logging.ERROR)
    app = FastAPI()
    app.include_router(channel_execution_api.router)

    async with app.router.lifespan_context(app):
        for _attempt in range(3):
            await asyncio.sleep(0)

    assert "error_code=CANDIDATE_GC_TASK_STOPPED" in caplog.text


async def test_router_lifespan_starts_one_interaction_callback_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from api.apps.restful_apis import channel_execution_api
    from api.channel_execution.interaction_worker import InteractionCallbackProcessor

    started = asyncio.Event()
    cancelled = asyncio.Event()
    build_calls = 0

    class _Processor:
        async def run_once(self, *, owner: str) -> int:
            assert owner.startswith("api-")
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return 0

    def build_runtime() -> tuple[InteractionCallbackProcessor, float]:
        nonlocal build_calls
        build_calls += 1
        return cast(InteractionCallbackProcessor, _Processor()), 0.01

    monkeypatch.setattr(
        channel_execution_api,
        "build_channel_candidate_gc_worker",
        lambda: None,
    )
    monkeypatch.setattr(
        channel_execution_api,
        "_build_interaction_callback_runtime",
        build_runtime,
    )
    app = FastAPI()
    app.include_router(channel_execution_api.router)
    app.include_router(channel_execution_api.router)

    async with app.router.lifespan_context(app):
        await started.wait()
        assert build_calls == 1
        handle = getattr(
            app.state,
            channel_execution_api._INTERACTION_CALLBACK_STATE_KEY,
        )
        assert handle.task.done() is False

    assert cancelled.is_set()
    assert not hasattr(
        app.state,
        channel_execution_api._INTERACTION_CALLBACK_STATE_KEY,
    )
