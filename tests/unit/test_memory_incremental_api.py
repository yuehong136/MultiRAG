"""Strict incremental commands preserve scope, preview CAS and recovery IDs."""

from typing import Any

import pytest
from fastapi.testclient import TestClient

from memory.revisions.domain import RevisionPersistenceUnknown


def test_preview_is_readonly_and_exact_scope_is_not_trimmed(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from api.apps.services import memory_maintenance_service

    calls = []

    class Discoveries:
        async def preview(self, memory_id: str, **options: Any) -> dict[str, Any]:
            calls.append(options)
            return {
                "registered_active_only": True,
                "token_fit": "not_checked",
                "preview_sha256": "a" * 64,
                "batches": [{"primary_sources": [{"source_id": str(2**53 + 1), "source_revision": "1", "source_generation": "1", "content_sha256": "b" * 64}]}],
            }

    async def factory(db: Any, memory_id: str, *, actor_id: str) -> Discoveries:
        assert memory_id == "bank" and actor_id == "user-unit"
        return Discoveries()

    monkeypatch.setattr(memory_maintenance_service, "discoveries", factory)
    response = client.post("/api/v1/memories/bank/maintenance-discoveries/preview", json={"scope": {"user_id": "", "agent_id": " ", "session_id": None}})
    assert response.status_code == 200
    assert calls[0]["scope"] == {"user_id": "", "agent_id": " ", "session_id": None}
    assert response.json()["data"]["batches"][0]["primary_sources"][0]["source_id"] == str(2**53 + 1)


@pytest.mark.parametrize(
    "options", [{"batch_size": True}, {"max_sources": "128"}, {"batch_size": 33}, {"source_ids": ["1"]}, {"strategy_sha256": "a" * 64}, {"scope": {"user_id": None, "trusted": True}}]
)
def test_selection_rejects_forged_strategy_sources_and_ambiguous_limits(client: TestClient, options: dict[str, Any]) -> None:
    assert client.post("/api/v1/memories/bank/maintenance-discoveries/preview", json=options).status_code == 422


def test_unknown_create_ack_keeps_public_operation_handle(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from api.apps.services import memory_maintenance_service

    class Discoveries:
        async def create(self, memory_id: str, **options: Any) -> dict[str, Any]:
            assert options["expected_preview_sha256"] == "a" * 64
            raise RevisionPersistenceUnknown("private-inner-handle")

    async def factory(db: Any, memory_id: str, *, actor_id: str) -> Discoveries:
        return Discoveries()

    monkeypatch.setattr(memory_maintenance_service, "discoveries", factory)
    response = client.post("/api/v1/memories/bank/maintenance-discoveries", json={"operation_id": "original-create", "expected_preview_sha256": "a" * 64})
    assert response.status_code == 503 and response.json()["data"]["operation_id"] == "original-create"
