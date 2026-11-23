"""Target recommendations remain explicit reads with strict confirmation values."""

from typing import Any

import pytest
from fastapi.testclient import TestClient


def test_explicit_target_read_preserves_empty_subject_and_decimal_source_identity(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from api.apps.services import memory_maintenance_service

    calls = []

    class Targets:
        async def recommend(self, memory_id: str, **options: Any) -> dict[str, Any]:
            calls.append((memory_id, options))
            return {"targets": [], "retrieval": {"mode": "lexical", "embedding_calls": 0, "inventory_complete": False}, "truncated": True}

    async def factory(db: Any, memory_id: str, *, actor_id: str) -> Targets:
        assert actor_id == "user-unit"
        return Targets()

    monkeypatch.setattr(memory_maintenance_service, "targets", factory)
    response = client.post("/api/v1/memories/bank/maintenance-targets/recommendations", json={"source_ids": [str(2**53 + 19)], "user_id": "", "agent_id": " ", "retrieval_mode": "lexical"})
    assert response.status_code == 200
    assert calls[0][1]["source_ids"] == [2**53 + 19]
    assert calls[0][1]["user_id"] == "" and calls[0][1]["agent_id"] == " "
    assert response.json()["data"]["truncated"]


@pytest.mark.parametrize(
    "change",
    [
        {"limit": True},
        {"limit": 9},
        {"source_ids": [True]},
        {"user_id": None},
        {"source_ids": []},
        {"budget_tokens": 8193},
        {"retrieval_mode": "auto_update"},
        {"tenant_id": "forged"},
        {"verified": True},
        {"query": "x" * 4097},
    ],
)
def test_bad_or_privilege_overriding_recommendations_are_rejected(client: TestClient, change: dict[str, Any]) -> None:
    assert client.post("/api/v1/memories/bank/maintenance-targets/recommendations", json={"source_ids": [20], "user_id": "user", "agent_id": "agent", **change}).status_code == 422


def test_source_or_target_recommendation_conflict_is_not_success(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from api.apps.services import memory_maintenance_service
    from memory.revisions.domain import RevisionConflict

    class Targets:
        async def recommend(self, memory_id: str, **options: Any) -> dict[str, Any]:
            raise RevisionConflict("target_preconditions_changed")

    async def factory(db: Any, memory_id: str, *, actor_id: str) -> Targets:
        return Targets()

    monkeypatch.setattr(memory_maintenance_service, "targets", factory)
    response = client.post("/api/v1/memories/bank/maintenance-targets/recommendations", json={"source_ids": [20], "user_id": "user", "agent_id": "agent"})
    assert response.status_code == 409


def test_storage_failure_is_not_a_successful_empty_recommendation(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from api.apps.services import memory_maintenance_service

    class Targets:
        async def recommend(self, memory_id: str, **options: Any) -> dict[str, Any]:
            raise RuntimeError("private storage failure")

    async def factory(db: Any, memory_id: str, *, actor_id: str) -> Targets:
        return Targets()

    monkeypatch.setattr(memory_maintenance_service, "targets", factory)
    response = client.post("/api/v1/memories/bank/maintenance-targets/recommendations", json={"source_ids": [20], "user_id": "user", "agent_id": "agent"})
    assert response.status_code == 500 and "private storage" not in response.text


@pytest.mark.parametrize("field,value", [("user_id", None), ("agent_id", None), ("user_id", "other"), ("agent_id", "other")])
def test_confirmed_target_preview_requires_the_same_exact_principal(client: TestClient, monkeypatch: pytest.MonkeyPatch, field: str, value: str | None) -> None:
    from api.apps.services import memory_maintenance_service
    from memory.maintenance.incremental import selection_request

    scope = {"user_id": "user", "agent_id": "agent", "session_id": "old"}

    def source(identity: int) -> dict[str, Any]:
        return {"source_id": str(identity), "source_revision": "1", "source_generation": "1", "content_sha256": "a" * 64, "scope": scope, "projection_sha256": "b" * 64}

    conditions = {
        "inputs": [source(20)],
        "targets": [
            {"entry_id": "a" * 32, "revision": "1", "message_id": "101", "generation": "1", "content_sha256": "c" * 64, "projection_sha256": "d" * 64, "scope": scope, "source_versions": [source(10)]}
        ],
        "model_binding": {"tenant_model_id": "00000000000000000000000000000001", "model_reference": "embedding", "dimension": 2, "config_reference": "e" * 64},
    }

    class Discoveries:
        async def preview(self, memory_id: str, **options: Any) -> dict[str, Any]:
            selection_request(**options)
            raise AssertionError("Mismatched subject must be rejected before selection")

    async def factory(db: Any, memory_id: str, *, actor_id: str) -> Discoveries:
        return Discoveries()

    monkeypatch.setattr(memory_maintenance_service, "discoveries", factory)
    requested_scope = {"user_id": "user", "agent_id": "agent", "session_id": None, field: value}
    response = client.post("/api/v1/memories/bank/maintenance-discoveries/preview", json={"scope": requested_scope, "entry_ids": ["a" * 32], "target_preconditions": conditions})
    assert response.status_code == 422
