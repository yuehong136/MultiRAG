"""Closed, synchronous telemetry for identity reconciliation.

Events are deliberately represented only by enums.  Implementations never
receive a tenant, provider account, directory subject, lease owner, or raw
error text, so recording a failure cannot become a second identity data store.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from threading import Lock
from types import MappingProxyType
from typing import Protocol, runtime_checkable


class ReconciliationTelemetryEvent(StrEnum):
    CHECKPOINTS_SEEDED = "checkpoints_seeded"
    CLAIM_IDLE = "claim_idle"
    OBSERVED_RESOLVED = "observed_resolved"
    OBSERVED_INACTIVE = "observed_inactive"
    OBSERVED_NOT_FOUND = "observed_not_found"
    OBSERVED_NOT_IN_SCOPE = "observed_not_in_scope"
    OBSERVED_UNAVAILABLE = "observed_unavailable"
    OBSERVED_CONFLICT = "observed_conflict"
    OBSERVED_INVALID = "observed_invalid"
    APPLY_APPLIED = "apply_applied"
    APPLY_CONFIRMATION_PENDING = "apply_confirmation_pending"
    APPLY_RETRY_SCHEDULED = "apply_retry_scheduled"
    APPLY_TARGET_REJECTED = "apply_target_rejected"
    APPLY_CIRCUIT_OPEN = "apply_circuit_open"
    APPLY_FENCE_REJECTED = "apply_fence_rejected"
    REPOSITORY_UNAVAILABLE = "repository_unavailable"


@dataclass(frozen=True, slots=True)
class ReconciliationTelemetrySnapshot:
    counters: Mapping[ReconciliationTelemetryEvent, int]


@runtime_checkable
class ReconciliationTelemetry(Protocol):
    """Non-blocking, process-local observation seam."""

    def increment(
        self,
        event: ReconciliationTelemetryEvent,
        *,
        count: int = 1,
    ) -> None: ...


class NoopReconciliationTelemetry:
    def increment(
        self,
        event: ReconciliationTelemetryEvent,
        *,
        count: int = 1,
    ) -> None:
        del event, count


class InMemoryReconciliationTelemetry:
    """Bounded diagnostic counters for tests and local process health."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._counters: dict[ReconciliationTelemetryEvent, int] = {}

    def increment(
        self,
        event: ReconciliationTelemetryEvent,
        *,
        count: int = 1,
    ) -> None:
        if not isinstance(event, ReconciliationTelemetryEvent):
            raise ValueError("identity reconciliation telemetry event is invalid")
        if type(count) is not int or count < 0:
            raise ValueError("identity reconciliation telemetry count is invalid")
        if count == 0:
            return
        with self._lock:
            self._counters[event] = self._counters.get(event, 0) + count

    def snapshot(self) -> ReconciliationTelemetrySnapshot:
        with self._lock:
            counters = MappingProxyType(dict(self._counters))
        return ReconciliationTelemetrySnapshot(counters=counters)


NOOP_RECONCILIATION_TELEMETRY: ReconciliationTelemetry = NoopReconciliationTelemetry()
PROCESS_RECONCILIATION_TELEMETRY = InMemoryReconciliationTelemetry()


__all__ = [
    "NOOP_RECONCILIATION_TELEMETRY",
    "PROCESS_RECONCILIATION_TELEMETRY",
    "InMemoryReconciliationTelemetry",
    "ReconciliationTelemetry",
    "ReconciliationTelemetryEvent",
    "ReconciliationTelemetrySnapshot",
]
