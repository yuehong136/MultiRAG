"""Backend-aware Memory isolation evidence for EIM-P2 / CHN-X18."""

from __future__ import annotations

from uuid import uuid4

from common import resources, settings
from common.bootstrap import ensure_initialized
from memory.services.messages import MessageService


def _message(
    *,
    message_id: int,
    memory_id: str,
    user_id: str,
    content: str,
) -> dict[str, object]:
    return {
        "message_id": message_id,
        "message_type": "raw",
        "source_id": 0,
        "memory_id": memory_id,
        "user_id": user_id,
        "agent_id": "eim-p2-agent",
        "session_id": "eim-p2-session",
        "valid_at": "2026-08-13 00:00:00",
        "invalid_at": None,
        "forget_at": None,
        "status": True,
        "content": content,
        "content_embed": [1.0, 0.0],
    }


def test_current_message_store_filters_same_tenant_users() -> None:
    ensure_initialized()
    store = settings.msgStoreConn
    assert store is resources.msg_store()
    assert store is not None, "configured DOC_ENGINE has no msgStoreConn backend"

    suffix = uuid4().hex[:12]
    tenant_id = f"eimp2tenant{suffix}"
    memory_id = f"eimp2memory{suffix}"
    try:
        assert MessageService.create_index(tenant_id, memory_id, vector_size=2) is True
        failures = MessageService.insert_message(
            [
                _message(
                    message_id=1,
                    memory_id=memory_id,
                    user_id="principal-a",
                    content="only-a",
                ),
                _message(
                    message_id=2,
                    memory_id=memory_id,
                    user_id="principal-b",
                    content="only-b",
                ),
            ],
            tenant_id,
            memory_id,
        )
        assert failures == []

        visible_to_a = MessageService.search_message(
            [memory_id],
            {"user_id": "principal-a"},
            [tenant_id],
            [],
            10,
        )
        visible_to_b = MessageService.search_message(
            [memory_id],
            {"user_id": "principal-b"},
            [tenant_id],
            [],
            10,
        )

        assert {message["content"] for message in visible_to_a} == {"only-a"}
        assert {message["content"] for message in visible_to_b} == {"only-b"}
    finally:
        MessageService.delete_index(tenant_id, memory_id)
