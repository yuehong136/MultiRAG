"""Maintenance commands cannot widen identity, scope or publication authority."""

from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from api.apps.restful_apis.memory_maintenance_api import CreateMaintenanceRequest, PublishCandidatesRequest


@pytest.mark.parametrize("identity", [True, 0, -1, "01", "1.0", "+1", "１", 2**63])
def test_explicit_source_range_requires_exact_signed_int64(identity: Any) -> None:
    with pytest.raises(ValidationError):
        CreateMaintenanceRequest(operation_id="create", source_ids=[identity])


def test_empty_range_and_untrusted_command_fields_cannot_be_accepted() -> None:
    for extra in ({"source_ids": []}, {"tenant_id": "foreign"}, {"actor_id": "foreign"}, {"trusted": True}, {"automatic_publication": True}):
        with pytest.raises(ValidationError):
            CreateMaintenanceRequest.model_validate({"operation_id": "create", "source_ids": [1], **extra})
    with pytest.raises(ValidationError):
        PublishCandidatesRequest(operation_id="publish", candidates=[{"candidate_id": "a" * 32, "expected_revision": 1, "operation_id": "bypass-parent"}])


def test_job_create_binds_real_actor_preserves_source_precision_and_does_not_run_model(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from api.apps.services import memory_maintenance_service

    calls: list[dict[str, Any]] = []

    class ControlledJobs:
        async def create(self, memory_id: str, **options: Any) -> dict[str, Any]:
            calls.append(options)
            assert memory_id == "bank"
            return {"job_id": "a" * 32, "state": "pending", "inputs": [{"source_id": options["source_ids"][0]}]}

    async def jobs(db: Any, memory_id: str, *, actor_id: str) -> ControlledJobs:
        assert memory_id == "bank" and actor_id == "user-unit"
        return ControlledJobs()

    monkeypatch.setattr(memory_maintenance_service, "jobs", jobs)
    response = client.post("/api/v1/memories/bank/maintenance-jobs", json={"operation_id": "snapshot", "source_ids": [str(2**53 + 1)]})
    assert response.status_code == 202 and response.json()["data"]["inputs"][0]["source_id"] == str(2**53 + 1)
    assert calls[0]["source_ids"] == [2**53 + 1] and calls[0]["entry_ids"] == []


@pytest.mark.parametrize(("status", "sync", "http"), [("published", "pending", 202), ("persistence_unknown", "unknown", 202), ("ready", "ready", 200), ("stale", "unknown", 200)])
def test_batch_response_does_not_treat_pending_as_published_memory(client: TestClient, monkeypatch: pytest.MonkeyPatch, status: str, sync: str, http: int) -> None:
    from api.apps.services import memory_maintenance_service

    class ControlledPublisher:
        async def publish(self, memory_id: str, job_id: str, **options: Any) -> dict[str, Any]:
            assert options == {"operation_id": "publish", "candidates": [{"candidate_id": "b" * 32, "expected_revision": 2}]}
            return {"operation_id": "publish", "kind": "dream_publish", "status": status, "index_sync_status": sync}

    async def publications(db: Any, memory_id: str, *, actor_id: str) -> ControlledPublisher:
        assert actor_id == "user-unit"
        return ControlledPublisher()

    monkeypatch.setattr(memory_maintenance_service, "publications", publications)
    response = client.post("/api/v1/memories/bank/maintenance-jobs/" + "a" * 32 + "/publications", json={"operation_id": "publish", "candidates": [{"candidate_id": "b" * 32, "expected_revision": 2}]})
    assert response.status_code == http and response.json()["data"]["status"] == status
