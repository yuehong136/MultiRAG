"""Canonical Run lifecycle and replay projection contracts."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from api.run_platform.domain import (
    ALLOWED_RUN_TRANSITIONS,
    TERMINAL_RUN_STATUSES,
    DesiredRunState,
    RunLifecycle,
    RunStatus,
    RunTransitionError,
)
from api.run_platform.events import RunEventProjectionError, apply_run_event
from api.run_platform.schemas import RunEventEnvelope, parse_event_envelope


def _event(seq: int, event_type: str, data: dict) -> RunEventEnvelope:
    return parse_event_envelope(
        {
            "v": 2,
            "event_id": f"evt_{seq}",
            "run_id": "run_1",
            "thread_id": "thread_1",
            "turn_id": "turn_1",
            "seq": seq,
            "type": event_type,
            "created_at": datetime(2026, 8, 13, 0, 0, seq, tzinfo=UTC),
            "data": data,
        }
    )


def _accepted() -> RunEventEnvelope:
    return _event(1, "run.accepted", {"target": {"id": "dialog_1"}})


def test_transition_matrix_is_total_and_terminal_states_never_revive() -> None:
    assert set(ALLOWED_RUN_TRANSITIONS) == set(RunStatus)
    assert set(TERMINAL_RUN_STATUSES) == {
        RunStatus.COMPLETED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
        RunStatus.INTERRUPTED,
    }

    for current in RunStatus:
        lifecycle = RunLifecycle(status=current)
        for target in RunStatus:
            if target in ALLOWED_RUN_TRANSITIONS[current]:
                transitioned = lifecycle.transition(target)
                assert transitioned.status is target
                assert transitioned.version == lifecycle.version + 1
            else:
                with pytest.raises(RunTransitionError):
                    lifecycle.transition(target)

    for terminal in TERMINAL_RUN_STATUSES:
        assert ALLOWED_RUN_TRANSITIONS[terminal] == frozenset()


def test_cancel_intent_can_reconcile_to_interrupted_and_enforces_version() -> None:
    cancel_requested = RunLifecycle().transition(
        RunStatus.CANCEL_REQUESTED,
        expected_version=1,
    )
    interrupted = cancel_requested.transition(
        RunStatus.INTERRUPTED,
        expected_version=2,
    )

    assert cancel_requested.desired_state is DesiredRunState.CANCELLED
    assert interrupted.status is RunStatus.INTERRUPTED
    with pytest.raises(RunTransitionError, match="version conflict"):
        cancel_requested.transition(
            RunStatus.CANCELLED,
            expected_version=1,
        )


def test_accepted_starts_at_version_one_and_queued_is_version_two() -> None:
    accepted = RunLifecycle()
    queued = accepted.transition(RunStatus.QUEUED)

    assert accepted.status is RunStatus.ACCEPTED
    assert accepted.version == 1
    assert queued.status is RunStatus.QUEUED
    assert queued.version == 2


def test_projection_accepts_ordered_completion_and_unknown_additive_event() -> None:
    projection = None
    for event in (
        _accepted(),
        _event(2, "usage.updated", {"input_tokens": 3}),
        _event(3, "run.queued", {}),
        _event(4, "run.started", {}),
        _event(5, "message.delta", {"part_id": "answer", "delta": "hello"}),
        _event(6, "message.snapshot", {"message_id": "message_1"}),
        _event(7, "run.completed", {"usage": {"output_tokens": 1}}),
    ):
        projection = apply_run_event(projection, event)

    assert projection is not None
    assert projection.lifecycle.status is RunStatus.COMPLETED
    assert projection.terminal is True
    assert projection.last_event_seq == 7


def test_projection_requires_accepted_at_seq_one() -> None:
    with pytest.raises(RunEventProjectionError, match="seq 1"):
        apply_run_event(None, _event(2, "run.accepted", {}))
    with pytest.raises(RunEventProjectionError, match=r"run\.accepted"):
        apply_run_event(None, _event(1, "run.queued", {}))


def test_projection_rejects_gap_identity_drift_and_events_after_terminal() -> None:
    projection = apply_run_event(None, _accepted())

    with pytest.raises(RunEventProjectionError, match="contiguous"):
        apply_run_event(projection, _event(3, "run.queued", {}))

    drifted = _event(2, "run.queued", {}).model_copy(update={"thread_id": "thread_other"})
    with pytest.raises(RunEventProjectionError, match="identity"):
        apply_run_event(projection, drifted)

    projection = apply_run_event(
        projection,
        _event(2, "run.cancelled", {"stage": "before_dispatch"}),
    )
    with pytest.raises(RunEventProjectionError, match="terminal"):
        apply_run_event(
            projection,
            _event(3, "message.delta", {"part_id": "answer", "delta": "late"}),
        )


def test_projection_ignores_exact_transport_duplicate_only() -> None:
    accepted = _accepted()
    projection = apply_run_event(None, accepted)

    assert apply_run_event(projection, accepted) is projection

    conflicting = accepted.model_copy(update={"data": {"target": {"id": "other"}}})
    with pytest.raises(RunEventProjectionError, match="identical content"):
        apply_run_event(projection, conflicting)

    with pytest.raises(RunEventProjectionError, match="only be the first"):
        apply_run_event(projection, _event(2, "run.accepted", {}))


def test_projection_uses_canonical_content_free_event_digest() -> None:
    accepted = _event(
        1,
        "run.accepted",
        {"target": {"revision": 1, "id": "dialog_1"}},
    )
    projection = apply_run_event(None, accepted)
    reordered = _event(
        1,
        "run.accepted",
        {"target": {"id": "dialog_1", "revision": 1}},
    )

    assert apply_run_event(projection, reordered) is projection
    assert len(projection.last_event_digest) == 64
    assert "dialog_1" not in projection.last_event_digest


def test_message_delta_is_only_valid_while_executing() -> None:
    projection = apply_run_event(None, _accepted())
    with pytest.raises(RunEventProjectionError, match=r"message\.delta"):
        apply_run_event(
            projection,
            _event(2, "message.delta", {"part_id": "answer", "delta": "early"}),
        )

    projection = apply_run_event(projection, _event(2, "run.queued", {}))
    projection = apply_run_event(projection, _event(3, "run.started", {}))
    with pytest.raises(ValidationError):
        apply_run_event(
            projection,
            _event(
                4,
                "message.delta",
                {
                    "part_id": "answer",
                    "delta": "hello",
                    "unvalidated": True,
                },
            ),
        )
