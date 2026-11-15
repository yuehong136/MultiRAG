"""Append HTTP validation and queue readiness remain separate from publication."""

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from api.apps.services import memory_revision_service


def body() -> dict[str, Any]:
    return {"operation_id": "stable-append", "user_input": "controlled", "agent_response": "controlled answer", "agent_id": "agent", "session_id": "session", "user_id": "forged-subject"}


@pytest.mark.parametrize("queue_state", ["pending", "unknown", "submitted", "not_required"])
def test_append_route_preserves_string_ids_and_does_not_complete_unknown_queue(client: TestClient, monkeypatch: pytest.MonkeyPatch, queue_state: str) -> None:
    calls: list[dict[str, Any]] = []

    async def append(db: AsyncSession, memory_id: str, message: dict[str, Any], *, actor_id: str, operation_id: str) -> dict[str, Any]:
        assert isinstance(db, AsyncSession)
        calls.append({"memory_id": memory_id, "message": message, "actor_id": actor_id, "operation_id": operation_id})
        return {
            "operation_id": operation_id,
            "kind": "append_raw",
            "status": "published",
            "index_sync_status": "ready",
            "raw_source_id": 2**53 + 1,
            "message_ids": [2**53 + 1],
            "queue_state": queue_state,
            "extraction_task_id": None if queue_state == "not_required" else "stable-task",
            "evicted_sources": [],
        }

    monkeypatch.setattr(memory_revision_service, "append_message", append)
    response = client.post("/api/v1/memories/owned/messages", json=body())
    assert response.status_code == (200 if queue_state in {"submitted", "not_required"} else 202)
    data = response.json()["data"]
    assert data["queue_state"] == queue_state and data["raw_source_id"] == str(2**53 + 1)
    assert data["message_ids"] == [str(2**53 + 1)]
    assert calls[0]["actor_id"] == calls[0]["message"]["user_id"] == "user-unit"


@pytest.mark.parametrize("change", [{"tenant_id": "forged"}, {"agent_id": "字" * 86}])
def test_append_rejects_identity_overrides_or_oversized_utf8_before_service(client: TestClient, monkeypatch: pytest.MonkeyPatch, change: dict[str, Any]) -> None:
    async def forbidden(*args: Any, **kwargs: Any) -> dict[str, Any]:
        pytest.fail("invalid request reached append service")

    monkeypatch.setattr(memory_revision_service, "append_message", forbidden)
    response = client.post("/api/v1/memories/owned/messages", json={**body(), **change})
    assert response.status_code == 422
