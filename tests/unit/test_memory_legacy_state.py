"""Snapshot validation for coordinated legacy state changes."""

from typing import Any

import pytest

from memory.revisions.domain import RevisionConflict
from memory.writes.legacy_state import _matches_state, _verify_snapshot


def _message(**changes: Any) -> dict[str, Any]:
    return {
        "memory_id": "bank",
        "message_id": 11,
        "id": "bank_11",
        "message_type": "semantic",
        "source_id": 10,
        "user_id": "",
        "agent_id": "agent",
        "session_id": "session",
        "content": "a fact",
        "content_embed": [0.1, 0.2],
        "valid_at": None,
        "invalid_at": None,
        "forget_at": None,
        "status": True,
        **changes,
    }


@pytest.mark.parametrize("status", [True, 1, "1"])
def test_storage_status_normalization_preserves_the_exact_forget_marker(status: Any) -> None:
    assert _matches_state(_message(status=status, forget_at="-"), status=True, forget_at=None)
    assert not _matches_state(_message(status=status, forget_at="2026-11-15"), status=True, forget_at=None)


def test_only_state_fields_may_change_during_a_confirmed_state_mutation() -> None:
    _verify_snapshot(_message(status=False, forget_at="2026-11-15"), _message())
    for changes in ({"content": "other"}, {"source_id": 12}, {"user_id": "other"}, {"content_embed": [0.2, 0.1]}):
        with pytest.raises(RevisionConflict):
            _verify_snapshot(_message(**changes), _message())
