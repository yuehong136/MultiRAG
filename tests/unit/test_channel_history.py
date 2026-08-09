"""Pure Channel history projection and replay-safety contracts."""

from __future__ import annotations

import json

import pytest

from api.channel_execution.history import (
    _prepare_canvas_dsl,
    _rewind_latest_turn,
    _sanitize_messages,
)


def _safe_dsl() -> dict[str, object]:
    return {
        "components": {
            "begin": {"obj": {"component_name": "Begin", "params": {}}},
            "agent": {
                "obj": {
                    "component_name": "Agent",
                    "params": {"tools": [], "mcp": []},
                }
            },
            "message": {"obj": {"component_name": "Message", "params": {}}},
        },
        "history": [["assistant", "stale private history"]],
        "globals": {
            "sys.history": ["stale private history"],
            "sys.conversation_turns": 99,
        },
    }


def test_regenerate_rewinds_exactly_one_matching_completed_turn() -> None:
    messages = [
        {"role": "user", "content": "previous", "id": "turn-1"},
        {"role": "assistant", "content": "previous answer", "id": "turn-1"},
        {"role": "user", "content": "same question", "id": "turn-2"},
        {"role": "assistant", "content": "old answer", "id": "turn-2"},
    ]
    references = [{"chunks": ["previous"]}, {"chunks": ["old"]}]

    _rewind_latest_turn(messages, references, "same question")

    assert [message["content"] for message in messages] == [
        "previous",
        "previous answer",
    ]
    assert references == [{"chunks": ["previous"]}]


def test_regenerate_rejects_non_latest_or_inconsistent_turn() -> None:
    messages = [
        {"role": "user", "content": "same question", "id": "turn-1"},
        {"role": "assistant", "content": "answer", "id": "turn-2"},
    ]

    with pytest.raises(LookupError, match="inconsistent"):
        _rewind_latest_turn(messages, [], "same question")

    with pytest.raises(LookupError, match="latest matching"):
        _rewind_latest_turn(
            [
                {"role": "user", "content": "other", "id": "turn-1"},
                {"role": "assistant", "content": "answer", "id": "turn-1"},
            ],
            [],
            "same question",
        )


def test_canvas_projection_uses_only_sanitized_visible_history() -> None:
    messages = _sanitize_messages(
        [
            {"role": "user", "content": "question", "id": "turn-1"},
            {
                "role": "assistant",
                "content": "<think>private</think>visible answer",
                "id": "turn-1",
            },
        ]
    )

    projected = _prepare_canvas_dsl(
        json.dumps(_safe_dsl()),
        messages,
        validate_replay=True,
    )

    assert projected["history"] == [
        ["user", "question"],
        ["assistant", "visible answer"],
    ]
    assert projected["globals"] == {
        "sys.history": ["user: question", "assistant: visible answer"],
        "sys.conversation_turns": 1,
    }
    assert "private" not in json.dumps(projected, ensure_ascii=False)


def test_canvas_regenerate_fails_closed_for_side_effect_components() -> None:
    unsafe_dsl = _safe_dsl()
    components = unsafe_dsl["components"]
    assert isinstance(components, dict)
    components["email"] = {
        "obj": {"component_name": "Email", "params": {}},
    }

    with pytest.raises(PermissionError, match="external tools"):
        _prepare_canvas_dsl(unsafe_dsl, [], validate_replay=True)
