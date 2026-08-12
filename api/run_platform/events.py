"""Pure Run event projection and replay validation for RUN-F1a."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace

from api.run_platform.domain import RunLifecycle, RunStatus, RunTransitionError
from api.run_platform.schemas import RunEventEnvelope, parse_message_delta


class RunEventProjectionError(ValueError):
    """Raised when an event stream cannot represent one valid Run."""


_EVENT_TARGET_STATUS: dict[str, RunStatus] = {
    "run.queued": RunStatus.QUEUED,
    "run.started": RunStatus.RUNNING,
    "interaction.required": RunStatus.WAITING_INPUT,
    "interaction.submitted": RunStatus.QUEUED,
    "run.cancel_requested": RunStatus.CANCEL_REQUESTED,
    "run.completed": RunStatus.COMPLETED,
    "run.failed": RunStatus.FAILED,
    "run.cancelled": RunStatus.CANCELLED,
    "run.interrupted": RunStatus.INTERRUPTED,
}
_INITIAL_EVENT_TYPE = "run.accepted"
_OUTPUT_EVENT_TYPES = frozenset({"message.delta", "message.snapshot"})
_OUTPUT_EVENT_ALLOWED_STATUSES = frozenset({RunStatus.RUNNING, RunStatus.CANCEL_REQUESTED})


@dataclass(frozen=True, slots=True)
class RunProjection:
    run_id: str
    thread_id: str
    turn_id: str
    lifecycle: RunLifecycle
    last_event_seq: int
    last_event_id: str
    last_event_digest: str

    @property
    def terminal(self) -> bool:
        return self.lifecycle.terminal


def _event_digest(event: RunEventEnvelope) -> str:
    canonical = json.dumps(
        event.model_dump(mode="json"),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _initial_projection(event: RunEventEnvelope) -> RunProjection:
    if event.seq != 1 or event.type != _INITIAL_EVENT_TYPE:
        raise RunEventProjectionError("The first Run event must be run.accepted at seq 1.")
    return RunProjection(
        run_id=event.run_id,
        thread_id=event.thread_id,
        turn_id=event.turn_id,
        lifecycle=RunLifecycle(),
        last_event_seq=1,
        last_event_id=event.event_id,
        last_event_digest=_event_digest(event),
    )


def apply_run_event(
    projection: RunProjection | None,
    event: RunEventEnvelope,
) -> RunProjection:
    """Apply one committed event with strict identity, ordering, and terminal rules.

    A transport-level exact duplicate of the last projected event may be
    replayed after acknowledgement loss. Global event-ID uniqueness remains a
    RUN-F2 ledger constraint so this minimal projection need not retain every
    historical event ID.
    """

    if projection is None:
        return _initial_projection(event)
    if event.seq == projection.last_event_seq and event.event_id == projection.last_event_id:
        if _event_digest(event) == projection.last_event_digest:
            return projection
        raise RunEventProjectionError("A repeated Run event ID and seq must have identical content.")
    if projection.terminal:
        raise RunEventProjectionError("No Run event may follow a terminal event.")
    if event.type == _INITIAL_EVENT_TYPE:
        raise RunEventProjectionError("run.accepted may only be the first Run event.")
    if event.run_id != projection.run_id or event.thread_id != projection.thread_id or event.turn_id != projection.turn_id:
        raise RunEventProjectionError("Run, thread, and turn identity must remain stable within an event stream.")
    expected_seq = projection.last_event_seq + 1
    if event.seq != expected_seq:
        raise RunEventProjectionError(f"Run event seq must be contiguous: expected {expected_seq}, got {event.seq}.")

    if event.type in _OUTPUT_EVENT_TYPES:
        if projection.lifecycle.status not in _OUTPUT_EVENT_ALLOWED_STATUSES:
            raise RunEventProjectionError(f"{event.type} is invalid while Run status is {projection.lifecycle.status.value}.")
        if event.type == "message.delta":
            parse_message_delta(event.model_dump(mode="python"))

    lifecycle = projection.lifecycle
    target_status = _EVENT_TARGET_STATUS.get(event.type)
    if target_status is not None:
        try:
            lifecycle = lifecycle.transition(target_status)
        except RunTransitionError as error:
            raise RunEventProjectionError(str(error)) from error

    return replace(
        projection,
        lifecycle=lifecycle,
        last_event_seq=event.seq,
        last_event_id=event.event_id,
        last_event_digest=_event_digest(event),
    )
