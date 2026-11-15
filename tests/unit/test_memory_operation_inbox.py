"""Operation discovery is scoped, read-only and independent of bank configuration."""

import base64
import json
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.apps.services.memory_operation_service import _cursor, _decode_cursor
from api.db.memory_revision_models import MemoryMutationOperation
from memory.revisions.domain import RevisionValidationError


def test_cursor_preserves_exact_boundary_and_rejects_scope_changes() -> None:
    stamp = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
    row = MemoryMutationOperation(created_at=stamp, operation_id="original-operation")
    cursor = _cursor("authorized-page", row)
    assert _decode_cursor(cursor, "authorized-page") == (stamp, "original-operation")
    with pytest.raises(RevisionValidationError, match="invalid_operation_cursor"):
        _decode_cursor(cursor, "different-actor-or-filter")


@pytest.mark.parametrize("value", ["invalid!", "e30=", "x" * 1025, base64.b64encode(json.dumps({"scope": "page", "created_at": "2026-11-15T12:00:00", "operation_id": "op"}).encode()).decode()])
def test_invalid_or_naive_cursor_is_not_accepted(value: str) -> None:
    with pytest.raises(RevisionValidationError, match="invalid_operation_cursor"):
        _decode_cursor(value, "page")


def test_route_binds_actor_and_only_calls_discovery(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from api.apps.services import memory_operation_service

    calls: list[dict[str, Any]] = []

    async def discover(db: Any, memory_id: str, **options: Any) -> dict[str, Any]:
        calls.append({"memory_id": memory_id, **options})
        return {"operations": [], "next_cursor": None, "limit": options["limit"], "view": options["view"], "memory_state": "deleted"}

    monkeypatch.setattr(memory_operation_service, "list_operations", discover)
    response = client.get("/api/v1/memories/deleted-bank/operations?view=all&limit=10")
    assert response.status_code == 200 and response.json()["data"]["memory_state"] == "deleted"
    assert calls == [{"memory_id": "deleted-bank", "actor_id": "user-unit", "view": "all", "limit": 10, "cursor": None}]


@pytest.mark.parametrize("query", ["view=foreign", "limit=0", "limit=101", "cursor=" + "x" * 1025])
def test_inbox_rejects_invalid_page_contract(client: TestClient, query: str) -> None:
    assert client.get("/api/v1/memories/bank/operations?" + query).status_code == 422


def test_run_only_submits_durable_work(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from api.apps.services import memory_maintenance_service

    calls: list[str] = []

    class ControlledExecutions:
        async def submit(self, memory_id: str, job_id: str, *, operation_id: str) -> dict[str, Any]:
            assert (memory_id, job_id, operation_id) == ("bank", "a" * 32, "original-analysis")
            calls.append("submit")
            return {"state": "pending", "analysis_operation_id": operation_id, "execution": {"state": "queued"}}

    async def executions(db: Any, memory_id: str, *, actor_id: str) -> ControlledExecutions:
        assert actor_id == "user-unit"
        return ControlledExecutions()

    monkeypatch.setattr(memory_maintenance_service, "executions", executions)
    response = client.post("/api/v1/memories/bank/maintenance-jobs/" + "a" * 32 + "/runs", json={"operation_id": "original-analysis"})
    assert response.status_code == 202
    assert response.json()["data"]["execution"]["state"] == "queued" and calls == ["submit"]
