"""Lifecycle tests for the disabled-by-default EIM-I8 worker."""

from __future__ import annotations

import asyncio

import pytest

from api.identity.reconciliation.runtime import (
    IdentityReconciliationRuntimeLimits,
    IdentityReconciliationWorker,
    build_identity_reconciliation_worker,
)
from api.identity.reconciliation.service import (
    ReconciliationRunResult,
    ReconciliationRunStatus,
    ReconciliationSeedResult,
    ReconciliationSeedStatus,
)
from api.identity.reconciliation.telemetry import (
    InMemoryReconciliationTelemetry,
    ReconciliationTelemetryEvent,
)


class _CycleService:
    def __init__(self, stop_event: asyncio.Event) -> None:
        self.stop_event = stop_event
        self.seed_calls = 0
        self.reconcile_calls = 0
        self.owners: list[str] = []

    async def seed_checkpoints(self) -> ReconciliationSeedResult:
        self.seed_calls += 1
        return ReconciliationSeedResult(ReconciliationSeedStatus.SEEDED, 1)

    async def reconcile_one(self, *, owner: str) -> ReconciliationRunResult:
        self.reconcile_calls += 1
        self.owners.append(owner)
        self.stop_event.set()
        return ReconciliationRunResult(ReconciliationRunStatus.IDLE)


async def test_disabled_worker_has_no_service_or_clock_side_effect() -> None:
    stop_event = asyncio.Event()
    service = _CycleService(stop_event)

    def forbidden_clock() -> float:
        raise AssertionError("disabled worker must not read its clock")

    worker = IdentityReconciliationWorker(
        service,
        IdentityReconciliationRuntimeLimits(),
        monotonic=forbidden_clock,
    )
    await worker.run(stop_event)

    assert service.seed_calls == 0
    assert service.reconcile_calls == 0


async def test_enabled_worker_seeds_then_processes_one_serial_target() -> None:
    stop_event = asyncio.Event()
    service = _CycleService(stop_event)
    worker = IdentityReconciliationWorker(
        service,
        IdentityReconciliationRuntimeLimits(
            poll_seconds=0.01,
            seed_interval_seconds=60.0,
        ),
        enabled=True,
        owner="worker-safe",
        monotonic=lambda: 10.0,
    )

    await worker.run(stop_event)

    assert service.seed_calls == 1
    assert service.reconcile_calls == 1
    assert service.owners == ["worker-safe"]


async def test_unexpected_cycle_failure_is_sanitized_and_worker_continues() -> None:
    stop_event = asyncio.Event()
    telemetry = InMemoryReconciliationTelemetry()

    class _RecoveringService(_CycleService):
        async def reconcile_one(self, *, owner: str) -> ReconciliationRunResult:
            self.reconcile_calls += 1
            self.owners.append(owner)
            if self.reconcile_calls == 1:
                raise RuntimeError("raw-directory-payload-sensitive")
            self.stop_event.set()
            return ReconciliationRunResult(ReconciliationRunStatus.IDLE)

    service = _RecoveringService(stop_event)
    times = iter([10.0, 10.5])
    worker = IdentityReconciliationWorker(
        service,
        IdentityReconciliationRuntimeLimits(
            poll_seconds=0.001,
            seed_interval_seconds=60.0,
        ),
        enabled=True,
        owner="worker-safe",
        monotonic=lambda: next(times),
        telemetry=telemetry,
    )

    await worker.run(stop_event)

    assert service.seed_calls == 1
    assert service.reconcile_calls == 2
    assert telemetry.snapshot().counters == {
        ReconciliationTelemetryEvent.REPOSITORY_UNAVAILABLE: 1,
    }
    assert "raw-directory-payload-sensitive" not in repr(
        telemetry.snapshot(),
    )


async def test_worker_cancellation_propagates() -> None:
    stop_event = asyncio.Event()
    entered = asyncio.Event()

    class _BlockingService(_CycleService):
        async def seed_checkpoints(self) -> ReconciliationSeedResult:
            entered.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    service = _BlockingService(stop_event)
    worker = IdentityReconciliationWorker(
        service,
        IdentityReconciliationRuntimeLimits(),
        enabled=True,
        owner="worker-safe",
    )
    task = asyncio.create_task(worker.run(stop_event))
    await entered.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert service.reconcile_calls == 0


def test_builder_is_opt_in_and_does_not_construct_disabled_worker() -> None:
    service = _CycleService(asyncio.Event())
    assert (
        build_identity_reconciliation_worker(
            service,
        )
        is None
    )


def test_builder_enables_only_an_explicit_service() -> None:
    service = _CycleService(asyncio.Event())
    worker = build_identity_reconciliation_worker(
        service,
        enabled=True,
        poll_seconds=2.0,
        seed_interval_seconds=30.0,
    )

    assert isinstance(worker, IdentityReconciliationWorker)


@pytest.mark.parametrize(
    ("poll_seconds", "seed_interval_seconds"),
    [(0.0, 1.0), (1.0, float("nan")), (float("inf"), 1.0)],
)
def test_runtime_limits_reject_non_positive_or_non_finite_values(
    poll_seconds: float,
    seed_interval_seconds: float,
) -> None:
    with pytest.raises(ValueError, match="runtime limits"):
        IdentityReconciliationRuntimeLimits(
            poll_seconds=poll_seconds,
            seed_interval_seconds=seed_interval_seconds,
        )
