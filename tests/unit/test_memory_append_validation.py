"""Readback validation rejects wrong rows without guessing vector precision."""

from typing import Any
from unittest.mock import MagicMock

import pytest

from memory.revisions.domain import RevisionConflict, RevisionValidationError
from memory.writes.admission import legacy_task_ids_sha256
from memory.writes.service import _content, _verify


def _message(**changes: Any) -> dict[str, Any]:
    return {
        "memory_id": "bank",
        "message_id": 3,
        "id": "bank_3",
        "message_type": "raw",
        "source_id": 0,
        "user_id": "",
        "agent_id": "agent",
        "session_id": "session",
        "content": "a fact",
        "content_embed": [0.1, 0.2],
        "status": False,
        "forget_at": None,
        "valid_at": "2026-11-15 10:00:00",
        "invalid_at": None,
        **changes,
    }


def test_float32_readback_keeps_the_actual_approved_vector_identity() -> None:
    expected = _message()
    _verify(_message(content_embed=[0.10000000149011612, 0.20000000298023224], invalid_at="-"), expected, active=False)
    with pytest.raises(RevisionConflict, match="vector_mismatch"):
        _verify(_message(content_embed=[0.2, 0.1]), expected, active=False)


@pytest.mark.parametrize(
    "change",
    [
        {"memory_id": "other"},
        {"message_id": 4},
        {"id": "bank_4"},
        {"source_id": False},
        {"source_id": 9},
        {"user_id": "other"},
        {"content": "different"},
        {"status": True},
        {"forget_at": "2026-11-15"},
        {"valid_at": "changed"},
        {"content_embed": [float("nan"), 0.0]},
    ],
)
def test_scoped_plan_readback_does_not_accept_a_different_record(change: dict[str, Any]) -> None:
    with pytest.raises((RevisionConflict, RevisionValidationError)):
        _verify(_message(**change), _message(), active=False)


def test_nonfinite_or_overflowing_provider_vectors_are_rejected() -> None:
    with pytest.raises(RevisionValidationError, match="invalid_append_vector"):
        _verify(_message(content_embed=[1e100, 0.2]), _message(content_embed=[1e100, 0.2]), active=False)


def test_source_body_utf8_bound_is_enforced_before_the_tokenizer() -> None:
    tokenizer = MagicMock(side_effect=AssertionError("must not tokenize over-byte content"))
    with pytest.raises(RevisionValidationError, match="invalid_append_content"):
        _content("中" * 22000, tokenizer)
    tokenizer.assert_not_called()


def test_legacy_task_attestation_hash_covers_the_exact_set() -> None:
    assert legacy_task_ids_sha256(["one", "two"]) == legacy_task_ids_sha256(["two", "one", "one"])
    assert legacy_task_ids_sha256(["one", "two"]) != legacy_task_ids_sha256(["one", "three"])
