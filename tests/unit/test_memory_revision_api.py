"""Public revision commands reject identity overrides and ambiguous edits."""

from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from api.apps.restful_apis.memory_revision_api import AdoptEntryRequest, ReviseEntryRequest, WithdrawSourceRequest


@pytest.mark.parametrize("value", [True, 0, -1, 1.5, "1.0", "+1", "01", "１", 2**63])
def test_adoption_requires_a_positive_integer_message_id(value: Any) -> None:
    with pytest.raises(ValidationError):
        AdoptEntryRequest(operation_id="adopt-entry", legacy_message_id=value)


def test_message_identifiers_preserve_signed_int64_precision_on_the_wire() -> None:
    from api.apps.restful_apis.memory_revision_api import _wire_data

    identity = 2**53 + 1
    assert AdoptEntryRequest(operation_id="adopt-entry", legacy_message_id=str(identity)).legacy_message_id == identity
    assert _wire_data({"current_message_id": identity, "current_revision": 3, "sources": [{"source_id": identity, "locator": {"message_id": identity}}]}) == {
        "current_message_id": str(identity),
        "current_revision": 3,
        "sources": [{"source_id": str(identity), "locator": {"message_id": str(identity)}}],
    }


@pytest.mark.parametrize(
    "change",
    [
        {"tenant_id": "foreign"},
        {"actor_id": "foreign"},
        {"assessment": "verified"},
        {"model_id": 18},
        {"expected_revision": True},
        {"expected_revision": "1"},
        {"dry_run": "true"},
        {"reason": " "},
        {"content": " "},
        {"content": "字" * 21846},
        {"content": None},
        {"patch": {"old_text": "a", "new_text": "b"}},
        {"operation_id": "operation/foreign"},
    ],
)
def test_revision_commands_reject_untrusted_or_ambiguous_inputs(change: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ReviseEntryRequest.model_validate({"operation_id": "edit-entry", "expected_revision": 1, "content": "a fact", "reason": "explicit correction", **change})


def test_revision_preserves_literal_text_and_accepts_one_exact_patch() -> None:
    replacement = ReviseEntryRequest(operation_id="edit-entry", expected_revision=3, content="  A corrected fact.\n", reason="user correction", dry_run=True)
    assert replacement.content == "  A corrected fact.\n"
    patch = ReviseEntryRequest(operation_id="edit-entry", expected_revision=3, patch={"old_text": "old", "new_text": ""}, reason="remove one assertion")
    assert patch.patch and patch.patch.old_text == "old" and patch.patch.new_text == ""


@pytest.mark.parametrize("change", [{"expected_source_revision": True}, {"expected_source_revision": 0}, {"reason": " "}, {"physical_delete": True}])
def test_withdrawal_has_explicit_version_and_does_not_accept_physical_delete(change: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        WithdrawSourceRequest.model_validate({"operation_id": "withdraw-source", "expected_source_revision": 1, "reason": "source retracted", **change})


@pytest.mark.parametrize(("state", "sync", "http_status"), [("accepted", "pending", 202), ("ready", "ready", 200), ("stale", "failed", 200)])
def test_revision_route_binds_actor_and_distinguishes_acceptance_from_readiness(client: TestClient, monkeypatch: pytest.MonkeyPatch, state: str, sync: str, http_status: int) -> None:
    from api.apps.services import memory_revision_service

    calls: list[dict[str, Any]] = []

    async def revise(db: AsyncSession, memory_id: str, entry_id: str, **options: Any) -> dict[str, Any]:
        assert isinstance(db, AsyncSession)
        calls.append({"memory_id": memory_id, "entry_id": entry_id, **options})
        return {"operation_id": options["operation_id"], "status": state, "index_sync_status": sync}

    monkeypatch.setattr(memory_revision_service, "revise_entry", revise)
    response = client.post(
        "/api/v1/memories/memory-a/entries/entry-a/revisions", json={"operation_id": "correction-1", "expected_revision": 2, "content": " a corrected fact ", "reason": "explicit correction"}
    )
    assert response.status_code == http_status and response.json()["retcode"] == 0
    assert calls == [
        {
            "memory_id": "memory-a",
            "entry_id": "entry-a",
            "actor_id": "user-unit",
            "operation_id": "correction-1",
            "expected_revision": 2,
            "content": " a corrected fact ",
            "patch": None,
            "reason": "explicit correction",
            "dry_run": False,
        }
    ]


def test_revision_http_rejects_unknown_identity_fields_before_service(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from api.apps.services import memory_revision_service

    async def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("Untrusted identity reached the service")

    monkeypatch.setattr(memory_revision_service, "adopt_entry", forbidden)
    response = client.post("/api/v1/memories/m/entries", json={"operation_id": "adopt", "legacy_message_id": 1, "actor_id": "another-user"})
    assert response.status_code == 422


def test_operation_get_does_not_request_reconciliation(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from api.apps.services import memory_revision_service

    calls: list[str] = []

    async def read(db: AsyncSession, memory_id: str, operation_id: str, *, actor_id: str) -> dict[str, Any]:
        calls.append("read")
        assert memory_id == "m" and operation_id == "op" and actor_id == "user-unit"
        return {"operation_id": operation_id, "status": "accepted", "index_sync_status": "pending"}

    async def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("GET must not advance an operation")

    monkeypatch.setattr(memory_revision_service, "get_operation", read)
    monkeypatch.setattr(memory_revision_service, "reconcile_operation", forbidden)
    assert client.get("/api/v1/memories/m/operations/op").json()["data"]["status"] == "accepted"
    assert calls == ["read"]


@pytest.mark.parametrize("kind", ["not_found", "conflict", "unknown", "internal"])
def test_revision_errors_preserve_conflict_and_unknown_commit_contracts(client: TestClient, monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    from api.apps.services import memory_revision_service
    from memory.revisions.domain import RevisionConflict, RevisionNotFound, RevisionPersistenceUnknown

    errors = {
        "not_found": RevisionNotFound("entry_not_found"),
        "conflict": RevisionConflict("revision_changed"),
        "unknown": RevisionPersistenceUnknown("original-operation"),
        "internal": RuntimeError("private stored content and credentials"),
    }

    async def fail(*args: Any, **kwargs: Any) -> None:
        raise errors[kind]

    monkeypatch.setattr(memory_revision_service, "get_entry", fail)
    response = client.get("/api/v1/memories/m/entries/entry")
    assert response.status_code == {"not_found": 404, "conflict": 409, "unknown": 503, "internal": 500}[kind]
    assert "private stored content" not in response.text
    if kind == "unknown":
        assert response.json()["data"] == {"operation_id": "original-operation", "error_code": "operation_commit_unknown", "requires_reconciliation": True}


def test_source_adoption_passes_hash_and_keeps_actor_server_owned(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from api.apps.services import memory_revision_service

    calls: list[dict[str, Any]] = []

    async def adopt(db: AsyncSession, memory_id: str, source_id: int, **options: Any) -> dict[str, Any]:
        calls.append({"memory_id": memory_id, "source_id": source_id, **options})
        return {"status": "ready", "index_sync_status": "ready", "source_revision": 1}

    monkeypatch.setattr(memory_revision_service, "adopt_source", adopt)
    response = client.post("/api/v1/memories/m/sources/7/adoptions", json={"operation_id": "adopt-source", "expected_content_sha256": "a" * 64})
    assert response.status_code == 200
    assert calls == [{"memory_id": "m", "source_id": 7, "actor_id": "user-unit", "operation_id": "adopt-source", "expected_content_sha256": "a" * 64}]


def test_unknown_inner_commit_keeps_public_operation_handle(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from api.apps.services import memory_revision_service
    from memory.revisions.domain import RevisionPersistenceUnknown

    async def unconfirmed(*args: Any, **kwargs: Any) -> None:
        raise RevisionPersistenceUnknown("internal-outbox-identifier")

    monkeypatch.setattr(memory_revision_service, "reconcile_operation", unconfirmed)
    response = client.post("/api/v1/memories/m/operations/public-operation/reconciliations")
    assert response.status_code == 503
    assert response.json()["data"]["operation_id"] == "public-operation"
    assert "internal-outbox-identifier" not in response.text


@pytest.mark.parametrize("method", ["PUT", "DELETE"])
def test_legacy_state_error_preserves_server_generated_recovery_handle(client: TestClient, monkeypatch: pytest.MonkeyPatch, method: str) -> None:
    from api.apps.services import memory_api_service, memory_revision_service

    async def unconfirmed(*args: Any, **kwargs: Any) -> None:
        raise memory_revision_service.LegacyStateReconciliationRequired("legacy-state:original", "source_projection_unconfirmed")

    monkeypatch.setattr(memory_api_service, "update_message_status_async" if method == "PUT" else "forget_message_async", unconfirmed)
    response = client.request(method, "/api/v1/messages/m:12", json={"status": False} if method == "PUT" else None)
    assert response.status_code == 200 and response.json()["retcode"] != 0
    assert response.json()["data"] == {"operation_id": "legacy-state:original", "error_code": "source_projection_unconfirmed", "requires_reconciliation": True}


@pytest.mark.parametrize("failure", ["database", "runtime"])
def test_durable_legacy_state_command_keeps_handle_when_reconciliation_fails(client: TestClient, monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from sqlalchemy.exc import OperationalError

    from api.apps.services import memory_revision_service

    error = OperationalError("private statement", {}, RuntimeError("private database details")) if failure == "database" else RuntimeError("private provider details")
    sources = SimpleNamespace(change_physical_state=AsyncMock(return_value={"status": "accepted"}), reconcile_operation=AsyncMock(side_effect=error))
    monkeypatch.setattr(memory_revision_service, "is_managed", AsyncMock(return_value=True))
    monkeypatch.setattr(memory_revision_service, "_components", AsyncMock(return_value=(object(), object())))
    monkeypatch.setattr(memory_revision_service, "_sources", lambda *args: sources)

    response = client.put("/api/v1/messages/m:12", json={"status": False})
    sources.change_physical_state.assert_awaited_once()
    operation_id = sources.change_physical_state.await_args.kwargs["operation_id"]
    assert operation_id.startswith("legacy-state:")
    assert response.status_code == 200 and response.json()["retcode"] != 0
    assert response.json()["data"] == {"operation_id": operation_id, "error_code": "state_change_reconciliation_unconfirmed", "requires_reconciliation": True}
    assert "private" not in response.text
