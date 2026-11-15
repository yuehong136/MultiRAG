"""Public contracts for optional provenance and bounded memory reads."""

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from api.apps.services import memory_api_service, memory_context_service
from common.constants import RetCode
from common.exceptions import NotFoundException
from memory.services.context import SourceContentChangedError
from memory.services.messages import MessageService, RecentMessageCursorStaleError, SourceMessageBindingError


def test_context_routes_bind_authenticated_actor_and_exact_subject(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    async def build(db: AsyncSession, memory_id: str, query: str, **options: Any) -> dict[str, Any]:
        assert isinstance(db, AsyncSession)
        calls.append({"memory_id": memory_id, "query": query, **options})
        return {"items": [], "source_snapshot_only": True}

    monkeypatch.setattr(memory_context_service, "query_context", build)
    response = client.post("/api/v1/memories/memory-a/context", json={"query": "  next action  ", "user_id": " external subject ", "budget_tokens": 512})
    assert response.status_code == 200 and response.json()["retcode"] == 0
    assert calls[0]["actor_id"] == "user-unit"
    assert calls[0]["subject_id"] == " external subject "
    assert calls[0]["query"] == "next action"
    assert calls[0]["memory_id"] == "memory-a"
    assert "mode" not in calls[0]


def test_capabilities_are_authorized_before_reading_backend_features(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def denied(db: AsyncSession, memory_id: str, *, actor_id: str) -> None:
        assert actor_id == "user-unit"
        calls.append("authorization")
        raise NotFoundException("Memory not found.")

    def backend(**options: Any) -> dict[str, Any]:
        calls.append("backend")
        assert options == {"tenant_id": "tenant-unit", "memory_id": "m"}
        return {"pagination": "live_seek_cursor"}

    monkeypatch.setattr(memory_context_service, "require_memory", denied)
    monkeypatch.setattr(MessageService, "get_recent_read_capabilities", backend)
    denied_response = client.get("/api/v1/memories/m/capabilities")
    assert denied_response.status_code == 404 and calls == ["authorization"]

    async def allowed(db: AsyncSession, memory_id: str, *, actor_id: str) -> Any:
        calls.append("authorization")
        return SimpleNamespace(tenant_id="tenant-unit")

    monkeypatch.setattr(memory_context_service, "require_memory", allowed)
    data = client.get("/api/v1/memories/m/capabilities").json()["data"]
    assert data["recent_messages"]["pagination"] == "live_seek_cursor"
    assert data["context"]["mutates_memory"] is False
    assert data["sources"]["verification"] == "provenance_only"
    assert calls == ["authorization", "authorization", "backend"]


@pytest.mark.parametrize(
    "body", [{"query": " "}, {"query": "q", "budget_tokens": True}, {"query": "q", "candidate_limit": 101}, {"query": "q", "tenant_id": "foreign"}, {"query": "q", "mode": "startup"}]
)
def test_context_rejects_ambiguous_or_unsupported_requests(client: TestClient, monkeypatch: pytest.MonkeyPatch, body: dict[str, Any]) -> None:
    async def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("invalid requests must not reach service")

    monkeypatch.setattr(memory_context_service, "query_context", forbidden)
    assert client.post("/api/v1/memories/m/context", json=body).status_code == 422


@pytest.mark.parametrize(
    ("error", "status", "retcode"),
    [
        (NotFoundException("Memory not found."), 404, RetCode.NOT_FOUND),
        (RecentMessageCursorStaleError("Restart pagination"), 409, RetCode.CONFLICT),
        (SourceMessageBindingError("private stored payload"), 500, RetCode.SERVER_ERROR),
    ],
)
def test_recent_read_error_states(client: TestClient, monkeypatch: pytest.MonkeyPatch, error: Exception, status: int, retcode: RetCode) -> None:
    async def fail(*args: Any, **kwargs: Any) -> None:
        raise error

    monkeypatch.setattr(memory_context_service, "recent_messages", fail)
    response = client.get("/api/v1/memories/m/messages")
    assert response.status_code == status
    assert response.json()["retcode"] == int(retcode)
    assert response.json()["data"] is False
    assert "private stored payload" not in response.text


def test_source_continuation_rechecks_hash_and_authorization(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    async def changed(db: AsyncSession, memory_id: str, source_id: int, **options: Any) -> None:
        calls.append({"memory_id": memory_id, "source_id": source_id, **options})
        raise SourceContentChangedError("Source content changed; request a new excerpt")

    monkeypatch.setattr(memory_context_service, "source_excerpt", changed)
    response = client.get("/api/v1/memories/m/sources/12", params={"offset": 9, "limit": 7, "content_sha256": "a" * 64, "user_id": "subject"})
    assert response.status_code == 409 and response.json()["retcode"] == int(RetCode.CONFLICT)
    assert calls == [{"memory_id": "m", "source_id": 12, "actor_id": "user-unit", "offset": 9, "limit": 7, "content_sha256": "a" * 64, "subject_id": "subject", "agent_id": None, "session_id": None}]


def test_legacy_recent_passes_only_explicit_filters(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    async def read(memory_ids: list[str], agent_id: str, session_id: str, limit: int, **options: Any) -> list[dict[str, Any]]:
        calls.append(options)
        return []

    monkeypatch.setattr(memory_api_service, "get_messages_async", read)
    assert client.get("/api/v1/messages?memory_id=m").json()["data"] == []
    assert client.get("/api/v1/messages?memory_id=m&message_type=raw&user_id=subject&status=false").json()["data"] == []
    assert calls == [{"user_id": "user-unit"}, {"user_id": "user-unit", "message_type": "raw", "subject_id": "subject", "status": False}]


def test_optional_source_search_preserves_actor_and_legacy_default(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def legacy(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        calls.append("legacy")
        return [{"message_id": 1}]

    async def enriched(db: AsyncSession, memory_ids: list[str], query: str, **options: Any) -> list[dict[str, Any]]:
        assert memory_ids == ["m"] and options["actor_id"] == "user-unit"
        assert options["subject_id"] == "subject"
        calls.append("enriched")
        return [{"message_id": 1, "source": {"state": "missing"}}]

    monkeypatch.setattr(memory_api_service, "search_message_async", legacy)
    monkeypatch.setattr(memory_context_service, "search_with_sources", enriched)
    old = client.get("/api/v1/messages/search?memory_id=m&query=q").json()["data"]
    new = client.get("/api/v1/messages/search?memory_id=m&query=q&user_id=subject&include_sources=true").json()["data"]
    assert old == [{"message_id": 1}]
    assert new[0]["source"]["state"] == "missing"
    assert calls == ["legacy", "enriched"]


def test_source_search_bounds_distinct_memories_before_service_calls(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    async def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("oversized scope reached the service")

    monkeypatch.setattr(memory_context_service, "search_with_sources", forbidden)
    response = client.get("/api/v1/messages/search", params={"memory_id": ",".join(f"memory-{i}" for i in range(101)), "query": "q", "include_sources": "true"})
    assert response.json()["code"] == int(RetCode.ARGUMENT_ERROR)
    assert response.json()["message"] == "Invalid bounded source-search parameters."
