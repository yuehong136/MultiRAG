"""Empty embeddings remain measurable in memory quotas and FIFO selection."""

import sys
from typing import Any
from unittest.mock import Mock

import numpy as np
import pytest

from common import settings
from memory.services.messages import MessageService


@pytest.mark.parametrize("content", [None, "", "hello", "中文记忆"])
@pytest.mark.parametrize("embedding", [None, [], np.array([]), [0.0, 1.0], np.array([0.0, 1.0])])
def test_message_size_handles_optional_and_numpy_embeddings(content: str | None, embedding: Any) -> None:
    message = {"content": content, "content_embed": embedding}
    values = embedding.tolist() if isinstance(embedding, np.ndarray) else embedding
    expected = sys.getsizeof(content or "") + (sys.getsizeof(values[0]) * len(values) if values else 0)

    assert MessageService.calculate_message_size(message) == expected
    assert message["content"] == content
    assert message["content_embed"] is embedding


@pytest.mark.parametrize("message", [{}, {"content": "hello"}, {"content_embed": [1.0]}])
def test_missing_fields_still_count_existing_content_or_embedding(message: dict[str, Any]) -> None:
    expected = sys.getsizeof(message.get("content", ""))
    if message.get("content_embed"):
        expected += sys.getsizeof(1.0)
    assert MessageService.calculate_message_size(message) == expected


def test_memory_totals_include_empty_embeddings_without_losing_other_memories(monkeypatch: pytest.MonkeyPatch) -> None:
    store = Mock()
    store.search.return_value = ({}, 3)
    store.get_fields.return_value = {
        "missing": {"memory_id": "first", "content": "hello"},
        "empty": {"memory_id": "first", "content": None, "content_embed": []},
        "numpy": {"memory_id": "second", "content_embed": np.array([0.0, 1.0])},
    }
    monkeypatch.setitem(settings.__dict__, "msgStoreConn", store)

    assert MessageService.calculate_memory_size(["first", "second"], ["owner"]) == {
        "first": sys.getsizeof("hello") + sys.getsizeof(""),
        "second": sys.getsizeof("") + 2 * sys.getsizeof(0.0),
    }
    assert store.search.call_args.kwargs["index_names"] == ["memory_owner"]
    assert store.search.call_args.kwargs["memory_ids"] == ["first", "second"]


@pytest.mark.parametrize("forgotten", [False, True])
def test_fifo_can_reach_quota_with_unembedded_messages(monkeypatch: pytest.MonkeyPatch, forgotten: bool) -> None:
    rows = {
        "old": {"message_id": "old", "content": "first", "content_embed": []},
        "next": {"message_id": "next", "content": None, "content_embed": None},
        "retained": {"message_id": "retained", "content": "retain", "content_embed": np.array([1.0])},
    }
    store = Mock()
    store.get_forgotten_messages.return_value = {"forgotten": True} if forgotten else None
    store.search.return_value = ({}, len(rows))
    store.get_fields.return_value = rows
    monkeypatch.setitem(settings.__dict__, "msgStoreConn", store)
    quota = sys.getsizeof("first") + sys.getsizeof("")

    assert MessageService.pick_messages_to_delete_by_fifo("memory", "owner", quota) == (["old", "next"], quota)
    assert store.search.call_count == (0 if forgotten else 1)
    store.get_forgotten_messages.assert_called_once_with(["message_id", "content", "content_embed"], "memory_owner", "memory")
