"""Disabled-by-default process loop for durable identity reconciliation."""

from __future__ import annotations

import asyncio
import math
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from api.identity.reconciliation.service import (
    ReconciliationRunResult,
    ReconciliationSeedResult,
)
from api.identity.reconciliation.telemetry import (
    NOOP_RECONCILIATION_TELEMETRY,
    ReconciliationTelemetry,
    ReconciliationTelemetryEvent,
)


@runtime_checkable
class ReconciliationCycleService(Protocol):
    async def seed_checkpoints(self) -> ReconciliationSeedResult: ...

    async def reconcile_one(self, *, owner: str) -> ReconciliationRunResult: ...


@dataclass(frozen=True, slots=True)
class IdentityReconciliationRuntimeLimits:
    poll_seconds: float = 1.0
    seed_interval_seconds: float = 60.0

    def __post_init__(self) -> None:
        if not math.isfinite(self.poll_seconds) or self.poll_seconds <= 0 or not math.isfinite(self.seed_interval_seconds) or self.seed_interval_seconds <= 0:
            raise ValueError("identity reconciliation runtime limits are invalid")


class IdentityReconciliationWorker:
    """Serial worker; database leases coordinate multiple API processes."""

    def __init__(
        self,
        service: ReconciliationCycleService,
        limits: IdentityReconciliationRuntimeLimits,
        *,
        enabled: bool = False,
        owner: str | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        telemetry: ReconciliationTelemetry = NOOP_RECONCILIATION_TELEMETRY,
    ) -> None:
        if owner is None:
            owner = f"reconcile-{secrets.token_hex(16)}"
        if type(owner) is not str or not owner.strip() or len(owner) > 64:
            raise ValueError("identity reconciliation worker owner is invalid")
        if type(enabled) is not bool:
            raise ValueError("identity reconciliation enabled flag is invalid")
        self._service = service
        self._limits = limits
        self._enabled = enabled
        self._owner = owner
        self._monotonic = monotonic
        self._telemetry = telemetry

    async def run(self, stop_event: asyncio.Event) -> None:
        """Run until stopped; cancellation always propagates to the caller."""

        if not self._enabled:
            return
        next_seed_at = 0.0
        while not stop_event.is_set():
            try:
                current = self._monotonic_now()
                if current >= next_seed_at:
                    await self._service.seed_checkpoints()
                    next_seed_at = current + self._limits.seed_interval_seconds
                await self._service.reconcile_one(owner=self._owner)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Production repositories translate SQL failures to a closed
                # error.  This final guard keeps a replica alive without
                # logging an exception that could contain directory data.
                self._telemetry.increment(
                    ReconciliationTelemetryEvent.REPOSITORY_UNAVAILABLE,
                )

            if stop_event.is_set():
                return
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=self._limits.poll_seconds,
                )
            except TimeoutError:
                pass

    def _monotonic_now(self) -> float:
        value = self._monotonic()
        if not math.isfinite(value):
            raise ValueError("identity reconciliation monotonic clock is invalid")
        return value


def build_identity_reconciliation_worker(
    service: ReconciliationCycleService,
    *,
    enabled: bool = False,
    poll_seconds: float = 1.0,
    seed_interval_seconds: float = 60.0,
    telemetry: ReconciliationTelemetry = NOOP_RECONCILIATION_TELEMETRY,
) -> IdentityReconciliationWorker | None:
    """Return no worker unless a caller explicitly opts into I8."""

    if not enabled:
        return None
    return IdentityReconciliationWorker(
        service,
        IdentityReconciliationRuntimeLimits(
            poll_seconds=poll_seconds,
            seed_interval_seconds=seed_interval_seconds,
        ),
        enabled=True,
        telemetry=telemetry,
    )


__all__ = [
    "IdentityReconciliationRuntimeLimits",
    "IdentityReconciliationWorker",
    "ReconciliationCycleService",
    "build_identity_reconciliation_worker",
]
