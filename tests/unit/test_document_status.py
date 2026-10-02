"""Strict status contracts and draining a cancelled owned write."""

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from api.apps.document_app import ChangeStatusRequest
from api.apps.restful_apis.document_api import BatchDocumentStatusRequest, batch_update_document_status
from api.db.services import document_status_service as service
from api.identity.principal import AuthenticatedActor, AuthenticationContext, AuthenticationSource, IdentityAssurance, TenantMembershipEvidence, build_principal_from_authenticated_actor


@pytest.mark.parametrize("value", [0, 1, "0", "1"])
def test_status_contract(value: Any) -> None:
    assert BatchDocumentStatusRequest(doc_ids=["a"], status=value).status == str(value)
    assert ChangeStatusRequest(doc_id="a", status=value).doc_ids == ["a"]
    assert ChangeStatusRequest(doc_ids="a", status=value).doc_ids == ["a"]


@pytest.mark.parametrize("value", [True, False, 0.0, 1.0, 0.5, 2, -1, "01", " 0", "", None, [], {}])
def test_status_rejects_coercion(value: Any) -> None:
    for model in [BatchDocumentStatusRequest, ChangeStatusRequest]:
        with pytest.raises(ValidationError):
            model(doc_ids=["a"], status=value)


@pytest.mark.parametrize("ids", [[], "a", [1], [True], [None], [""], ["  "], None])
def test_strict_batch_ids(ids: Any) -> None:
    with pytest.raises(ValidationError):
        BatchDocumentStatusRequest(doc_ids=ids, status=0)


@pytest.mark.asyncio
async def test_cancel_waits_until_owned_write_finishes() -> None:
    started, finish = asyncio.Event(), asyncio.Event()
    events: list[str] = []

    async def write() -> str:
        started.set()
        await finish.wait()
        events.append("store/sql/lock finished")
        return "done"

    task = asyncio.create_task(service.finish_status_write(asyncio.create_task(write())))
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done(), repr(task.exception())
    task.cancel()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert events == ["store/sql/lock finished"]


@pytest.mark.asyncio
async def test_batch_deduplicates_and_keeps_partial_wire(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def write(db: Any, doc: str, status: str, principal: str, dataset: str | None) -> str | None:
        calls.append(doc)
        return "Document not found in this dataset." if doc == "missing" else None

    async def permitted(*args: Any) -> Any:
        return SimpleNamespace(id="kb")

    monkeypatch.setattr(service, "change_document_status", write)
    monkeypatch.setattr("api.apps.restful_apis.document_api.writable_dataset", permitted)
    response = await batch_update_document_status(
        "kb",
        BatchDocumentStatusRequest(doc_ids=["a", "missing", "a", "b"], status=0),
        db=AsyncSession(),
        principal=build_principal_from_authenticated_actor(
            actor=AuthenticatedActor(platform_user_id="owner"),
            membership=TenantMembershipEvidence(platform_user_id="owner", tenant_id="owner"),
            authentication=AuthenticationContext(source=AuthenticationSource.WEB_SESSION, assurance=IdentityAssurance.AUTHENTICATED, validated_at=datetime.now(UTC)),
        ),
    )
    body = json.loads(response.body)
    assert calls == ["a", "missing", "b"]
    assert body == {"code": 500, "message": "Partial failure", "data": {"a": {"status": "0"}, "missing": {"error": "Document not found in this dataset."}, "b": {"status": "0"}}}
