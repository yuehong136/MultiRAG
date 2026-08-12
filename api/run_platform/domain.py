"""Pure Run lifecycle model.

This module has no database, transport, queue, target, Channel, or identity
dependency.  Persistence adapters will enforce the same transitions with CAS in
RUN-F2; this reducer is the canonical semantic table for clients and tests.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum


class RunStatus(StrEnum):
    ACCEPTED = "accepted"
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_INPUT = "waiting_input"
    CANCEL_REQUESTED = "cancel_requested"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class DesiredRunState(StrEnum):
    RUNNING = "running"
    CANCELLED = "cancelled"


TERMINAL_RUN_STATUSES = frozenset(
    {
        RunStatus.COMPLETED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
        RunStatus.INTERRUPTED,
    }
)

ALLOWED_RUN_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.ACCEPTED: frozenset(
        {
            RunStatus.QUEUED,
            RunStatus.CANCEL_REQUESTED,
            RunStatus.CANCELLED,
        }
    ),
    RunStatus.QUEUED: frozenset(
        {
            RunStatus.RUNNING,
            RunStatus.CANCEL_REQUESTED,
            RunStatus.CANCELLED,
        }
    ),
    RunStatus.RUNNING: frozenset(
        {
            RunStatus.WAITING_INPUT,
            RunStatus.CANCEL_REQUESTED,
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
            RunStatus.INTERRUPTED,
        }
    ),
    RunStatus.WAITING_INPUT: frozenset(
        {
            RunStatus.QUEUED,
            RunStatus.CANCEL_REQUESTED,
        }
    ),
    RunStatus.CANCEL_REQUESTED: frozenset(
        {
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
            RunStatus.INTERRUPTED,
        }
    ),
    RunStatus.COMPLETED: frozenset(),
    RunStatus.FAILED: frozenset(),
    RunStatus.CANCELLED: frozenset(),
    RunStatus.INTERRUPTED: frozenset(),
}


class RunTransitionError(ValueError):
    """Raised when a lifecycle transition violates the canonical state table."""


def can_transition(current: RunStatus, target: RunStatus) -> bool:
    return target in ALLOWED_RUN_TRANSITIONS[current]


@dataclass(frozen=True, slots=True)
class RunLifecycle:
    status: RunStatus = RunStatus.ACCEPTED
    desired_state: DesiredRunState = DesiredRunState.RUNNING
    version: int = 1

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_RUN_STATUSES

    def transition(
        self,
        target: RunStatus,
        *,
        expected_version: int | None = None,
    ) -> RunLifecycle:
        if expected_version is not None and expected_version != self.version:
            raise RunTransitionError(f"Run version conflict: expected {expected_version}, actual {self.version}.")
        if not can_transition(self.status, target):
            raise RunTransitionError(f"Run cannot transition from {self.status.value} to {target.value}.")

        desired_state = DesiredRunState.CANCELLED if target in {RunStatus.CANCEL_REQUESTED, RunStatus.CANCELLED} else self.desired_state
        return replace(
            self,
            status=target,
            desired_state=desired_state,
            version=self.version + 1,
        )
