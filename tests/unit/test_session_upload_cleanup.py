"""Deletion authority, storage uncertainty and session endpoint contracts."""

import json
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.services.conversation_service import ConversationService
from api.db.services.dialog_service import DialogService
from api.db.services.file_service import FileService, RuntimeUploadCleanupError
from api.db.services.runtime_attachment_service import RuntimeAttachmentService
from api.db.services.user_service import UserTenantService
from common import settings
from common.constants import RetCode

OWNER = "user-unit"
LOCATION = "a" * 32
BUCKET = f"{OWNER}-downloads"
DESCRIPTOR = f"{LOCATION}.upload.json"


class CleanupStorage:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.removed: list[tuple[str, str]] = []
        self.failure: str | None = None

    def obj_exist(self, bucket: str, key: str) -> bool | None:
        if self.failure == "unknown":
            return None
        return (bucket, key) in self.objects

    def get_bytes(self, bucket: str, key: str) -> bytes | None:
        if self.failure in {"read", "unknown"}:
            raise OSError("storage transport failed")
        return self.objects.get((bucket, key))

    def rm(self, bucket: str, key: str) -> bool | None:
        self.removed.append((bucket, key))
        if self.failure == key:
            return False
        self.objects.pop((bucket, key), None)
        return None


@pytest.fixture
def storage(monkeypatch: pytest.MonkeyPatch) -> CleanupStorage:
    storage = CleanupStorage()
    storage.objects[BUCKET, LOCATION] = b"owned bytes"
    storage.objects[BUCKET, DESCRIPTOR] = json.dumps(
        {"id": LOCATION, "created_by": OWNER, "name": "notes.txt", "mime_type": "text/plain", "extension": "txt", "size": 11, "created_at": 1.0, "preview_url": None}
    ).encode()
    monkeypatch.setattr(settings, "STORAGE_IMPL", storage)
    return storage


def test_cleanup_deduplicates_and_checks_both_objects(storage: CleanupStorage) -> None:
    storage.objects[BUCKET, "unrelated"] = b"keep"
    FileService.remove_runtime_uploads(OWNER, [LOCATION, LOCATION])
    assert storage.removed == [(BUCKET, LOCATION), (BUCKET, DESCRIPTOR)]
    assert storage.objects == {(BUCKET, "unrelated"): b"keep"}
    FileService.remove_runtime_uploads(OWNER, [LOCATION])
    assert len(storage.removed) == 2


@pytest.mark.parametrize("failure", [LOCATION, DESCRIPTOR, "unknown", "read"])
def test_cleanup_failure_is_retryable(storage: CleanupStorage, failure: str) -> None:
    storage.failure = failure
    with pytest.raises(RuntimeUploadCleanupError, match="retry"):
        FileService.remove_runtime_uploads(OWNER, [LOCATION])
    assert (BUCKET, DESCRIPTOR) in storage.objects
    if failure != DESCRIPTOR:
        assert (BUCKET, LOCATION) in storage.objects
        assert (BUCKET, DESCRIPTOR) not in storage.removed
    storage.failure = None
    FileService.remove_runtime_uploads(OWNER, [LOCATION])
    assert storage.objects == {}


@pytest.mark.parametrize("descriptor", [None, b"invalid json", b"null", b'{"id": "forged"}'])
def test_untrusted_descriptor_cannot_delete_payload(storage: CleanupStorage, descriptor: bytes | None) -> None:
    if descriptor is None:
        storage.objects.pop((BUCKET, DESCRIPTOR))
    else:
        storage.objects[BUCKET, DESCRIPTOR] = descriptor
    FileService.remove_runtime_uploads(OWNER, [LOCATION, "../foreign", "not-an-upload"])
    assert storage.removed == []
    assert storage.objects[BUCKET, LOCATION] == b"owned bytes"


def test_foreign_owner_descriptor_cannot_grant_authority(storage: CleanupStorage) -> None:
    value = json.loads(storage.objects[BUCKET, DESCRIPTOR])
    value["created_by"] = "foreign"
    storage.objects[BUCKET, DESCRIPTOR] = json.dumps(value).encode()
    FileService.remove_runtime_uploads(OWNER, [LOCATION])
    FileService.remove_runtime_uploads("foreign", [LOCATION])
    assert storage.removed == []


def test_missing_payload_still_cleans_trusted_descriptor(storage: CleanupStorage) -> None:
    storage.objects.pop((BUCKET, LOCATION))
    FileService.remove_runtime_uploads(OWNER, [LOCATION])
    assert storage.objects == {} and storage.removed == [(BUCKET, LOCATION), (BUCKET, DESCRIPTOR)]


def test_message_hints_are_owner_scoped_and_deduplicated() -> None:
    owned = {"id": LOCATION, "created_by": OWNER, "bucket": "foreign", "location": "arbitrary"}
    messages = [None, {"files": None}, {"files": "bad"}, {"files": [None, "bad", owned, owned, {"id": "../foreign", "created_by": OWNER}, {"id": LOCATION, "created_by": "foreign"}]}]
    assert RuntimeAttachmentService.upload_locations(messages, OWNER) == [LOCATION]
    assert RuntimeAttachmentService.upload_locations("bad", OWNER) == []


def test_foreign_chat_denied_before_session_or_storage(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(DialogService, "query", classmethod(lambda cls, db, **kw: []))

    async def forbidden(*args: Any, **kwargs: Any) -> bool:
        pytest.fail("Unauthorized deletion accessed a session")

    monkeypatch.setattr(ConversationService, "delete_with_runtime_uploads", forbidden)
    response = client.request("DELETE", "/api/v1/chats/foreign/sessions", json={"ids": ["session"]})
    assert response.status_code == 200 and response.json()["code"] == RetCode.AUTHENTICATION_ERROR


def test_endpoint_uses_authenticated_owner_and_reports_cleanup_failure(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(DialogService, "query", classmethod(lambda cls, db, **kw: [object()]))
    calls: list[dict[str, str]] = []

    async def delete(db: AsyncSession, **kwargs: str) -> bool:
        calls.append(kwargs)
        if kwargs["session_id"] == "failed":
            raise RuntimeUploadCleanupError("retry deletion")
        return kwargs["session_id"] != "missing"

    monkeypatch.setattr(ConversationService, "delete_with_runtime_uploads", delete)
    response = client.request("DELETE", "/api/v1/chats/chat/sessions", json={"ids": ["failed"], "created_by": "foreign"})
    assert response.json()["code"] != 0 and "retry" in response.json()["message"] and "data" not in response.json()
    response = client.request("DELETE", "/api/v1/chats/chat/sessions", json={"ids": ["ok", "ok", "missing", "failed"]})
    assert response.json()["code"] == 0 and response.json()["data"]["success_count"] == 1
    assert len(response.json()["data"]["errors"]) == 3
    assert all(call["owner_id"] == OWNER and call["tenant_id"] == "tenant-unit" and call["chat_id"] == "chat" for call in calls)


def test_legacy_batch_preflights_all_sessions_before_deletion(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(UserTenantService, "query", classmethod(lambda cls, db, **kw: [SimpleNamespace(tenant_id="tenant-unit", role="normal")]))
    monkeypatch.setattr(ConversationService, "get_by_id", classmethod(lambda cls, db, sid: SimpleNamespace(id=sid, dialog_id=sid)))
    monkeypatch.setattr(DialogService, "query", classmethod(lambda cls, db, **kw: [object()] if kw["id"] == "own" else []))

    async def forbidden(*args: Any, **kwargs: Any) -> bool:
        pytest.fail("Denied legacy batch deleted an authorized earlier session")

    monkeypatch.setattr(ConversationService, "delete_with_runtime_uploads", forbidden)
    response = client.post("/v1/conversation/rm", json={"conversation_ids": ["own", "foreign"]})
    assert response.status_code == 200 and response.json()["retcode"] == RetCode.OPERATING_ERROR


def test_legacy_cleanup_failure_is_not_success(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(UserTenantService, "query", classmethod(lambda cls, db, **kw: [SimpleNamespace(tenant_id="tenant-unit", role="normal")]))
    monkeypatch.setattr(ConversationService, "get_by_id", classmethod(lambda cls, db, sid: SimpleNamespace(id=sid, dialog_id="chat")))
    monkeypatch.setattr(DialogService, "query", classmethod(lambda cls, db, **kw: [object()]))

    async def failed(*args: Any, **kwargs: Any) -> bool:
        raise RuntimeUploadCleanupError("storage unavailable")

    monkeypatch.setattr(ConversationService, "delete_with_runtime_uploads", failed)
    response = client.post("/v1/conversation/rm", json={"conversation_ids": ["session"]})
    assert response.status_code == 200 and response.json()["retcode"] == RetCode.EXCEPTION_ERROR and response.json()["data"] is False


def test_legacy_storage_without_strict_read_cannot_claim_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "STORAGE_IMPL", SimpleNamespace(obj_exist=lambda *_args: False, rm=lambda *_args: None))
    with pytest.raises(RuntimeUploadCleanupError, match="does not support"):
        FileService.remove_runtime_uploads(OWNER, [LOCATION])
