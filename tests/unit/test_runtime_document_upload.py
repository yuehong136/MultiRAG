"""REST upload descriptors, shared compatibility behavior and failure boundaries."""

import asyncio
import threading
from io import BytesIO
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import Headers, UploadFile

from api.db.services.file_service import FileService
from common import settings
from common.constants import RetCode


@pytest.fixture
def uploaded(monkeypatch: pytest.MonkeyPatch) -> list[tuple[AsyncSession, str, str | None, str | None]]:
    calls: list[tuple[AsyncSession, str, str | None, str | None]] = []

    async def upload(db: AsyncSession, user_id: str, file: Any | None, url: str | None = None) -> dict[str, Any]:
        calls.append((db, user_id, getattr(file, "filename", None), url))
        binary = await file.read() if file else b"crawled"
        return {
            "id": f"location-{len(calls)}",
            "name": file.filename if file else "page.pdf",
            "size": len(binary),
            "extension": "txt" if file else "pdf",
            "mime_type": file.content_type if file else "application/pdf",
            "created_by": user_id,
            "created_at": 123.0,
            "preview_url": None,
        }

    monkeypatch.setattr(FileService, "upload_info", staticmethod(upload))
    return calls


@pytest.mark.parametrize("count", [1, 2])
def test_rest_upload_returns_consumable_descriptors(client: TestClient, uploaded: list, count: int) -> None:
    response = client.post("/api/v1/documents/upload", files=[("file", (f"{i}.txt", f"text-{i}".encode(), "text/plain")) for i in range(count)])
    body = response.json()
    assert response.status_code == 200 and body["code"] == 0
    assert isinstance(body["data"], dict if count == 1 else list)
    descriptors = [body["data"]] if count == 1 else body["data"]
    assert len(descriptors) == count
    for index, descriptor in enumerate(descriptors):
        assert descriptor == {
            "id": f"location-{index + 1}",
            "name": f"{index}.txt",
            "size": 6,
            "extension": "txt",
            "mime_type": "text/plain",
            "created_by": "user-unit",
            "created_at": 123.0,
            "preview_url": None,
        }
    assert all(call[1] == "user-unit" and call[3] is None for call in uploaded)
    assert len({id(call[0]) for call in uploaded}) == 1


def test_rest_upload_url_shape(client: TestClient, uploaded: list) -> None:
    body = client.post("/api/v1/documents/upload", params={"url": "https://example.com/page"}).json()
    assert body["code"] == 0 and isinstance(body["data"], dict)
    assert body["data"]["created_by"] == "user-unit"
    assert uploaded[0][2:] == (None, "https://example.com/page")


@pytest.mark.parametrize("kind", ["missing", "mixed", "empty", "string", "blank_url", "wrong_field"])
def test_rest_upload_rejects_bad_input_before_storage(client: TestClient, uploaded: list, kind: str) -> None:
    kwargs: dict[str, Any] = {
        "missing": {},
        "mixed": {"files": {"file": ("a.txt", b"a")}, "params": {"url": "https://example.com"}},
        "empty": {"files": {"file": ("", b"")}},
        "string": {"data": {"file": "not-a-file"}},
        "blank_url": {"params": {"url": " "}},
        "wrong_field": {"files": {"files": ("a.txt", b"a")}},
    }[kind]
    response = client.post("/api/v1/documents/upload", **kwargs)
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == RetCode.ARGUMENT_ERROR and body["message"]
    assert "data" not in body and uploaded == []


@pytest.mark.parametrize(
    "path,field,code_key,error_code",
    [
        ("/api/v1/documents/upload", "file", "code", RetCode.ARGUMENT_ERROR),
        ("/api/v1/files/upload_info", "files", "code", RetCode.ARGUMENT_ERROR),
    ],
)
def test_gateways_share_shapes_and_url_argument_errors(client: TestClient, uploaded: list, monkeypatch: pytest.MonkeyPatch, path: str, field: str, code_key: str, error_code: int) -> None:
    body = client.post(path, files=[(field, ("a.txt", b"a")), (field, ("b.txt", b"bb"))]).json()
    assert body[code_key] == 0 and [item["name"] for item in body["data"]] == ["a.txt", "b.txt"]

    async def unsafe(db: AsyncSession, user_id: str, file: Any, url: str | None = None) -> dict[str, Any]:
        raise ValueError("URL resolves to a non-public address")

    monkeypatch.setattr(FileService, "upload_info", staticmethod(unsafe))
    body = client.post(path, params={"url": "http://127.0.0.1"}).json()
    assert body[code_key] == error_code
    assert "non-public" in body.get("message", body.get("retmsg", ""))


def test_rest_storage_failure_has_no_success_data(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    async def failed(db: AsyncSession, user_id: str, file: Any, url: str | None = None) -> dict[str, Any]:
        raise RuntimeError("storage unavailable")

    monkeypatch.setattr(FileService, "upload_info", staticmethod(failed))
    body = client.post("/api/v1/documents/upload", files={"file": ("a.txt", b"a")}).json()
    assert body == {"code": RetCode.EXCEPTION_ERROR, "message": "Failed to upload document."}


async def test_silent_storage_failure_does_not_create_descriptor(async_db: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    from api.db.services.document_service import DocumentService

    monkeypatch.setattr(DocumentService, "check_doc_health", classmethod(lambda *_args: True))
    monkeypatch.setattr(FileService, "put_blob", staticmethod(lambda *_args: None))
    monkeypatch.setattr(settings, "STORAGE_IMPL", type("MissingStorage", (), {"obj_exist": lambda *_args: False, "rm": lambda *_args: None})())
    with pytest.raises(RuntimeError, match="Failed to store"):
        await FileService.upload_info(async_db, "owner", UploadFile(BytesIO(b"text"), filename="a.txt"))


class RuntimeStorage:
    def __init__(self, receipt: Any = True, *, fail_second: bool = False, fail_cleanup: bool = False) -> None:
        self.receipt = receipt
        self.fail_second = fail_second
        self.fail_cleanup = fail_cleanup
        self.objects: dict[tuple[str, str], bytes] = {}
        self.writes = 0

    def put(self, bucket: str, key: str, blob: bytes) -> Any:
        self.writes += 1
        if self.fail_second and self.writes == 3:
            return False
        if self.receipt is not False:
            self.objects[bucket, key] = blob
        return self.receipt

    def obj_exist(self, bucket: str, key: str) -> bool:
        return (bucket, key) in self.objects

    def get(self, bucket: str, key: str) -> bytes | None:
        return self.objects.get((bucket, key))

    def rm(self, bucket: str, key: str) -> None:
        if not self.fail_cleanup:
            self.objects.pop((bucket, key), None)


def runtime_file(name: str) -> UploadFile:
    return UploadFile(BytesIO(b"runtime bytes"), filename=name, headers=Headers({"content-type": "text/plain"}))


@pytest.mark.parametrize("receipt", [False, None, True, object()])
async def test_storage_receipt_conventions(async_db: AsyncSession, monkeypatch: pytest.MonkeyPatch, receipt: Any) -> None:
    from api.db.services.document_service import DocumentService

    storage = RuntimeStorage(receipt)
    monkeypatch.setattr(settings, "STORAGE_IMPL", storage)
    monkeypatch.setattr(DocumentService, "check_doc_health", classmethod(lambda *_args: True))
    if receipt is False:
        with pytest.raises(RuntimeError, match="Failed to store"):
            await FileService.upload_infos(async_db, "owner", [runtime_file("a.txt")])
        assert storage.objects == {}
    else:
        descriptor = await FileService.upload_infos(async_db, "owner", [runtime_file("a.txt")])
        assert storage.objects["owner-downloads", descriptor["id"]] == b"runtime bytes"


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_second_upload_failure_compensates_only_this_batch(client: TestClient, monkeypatch: pytest.MonkeyPatch, cleanup_fails: bool) -> None:
    from api.db.services.document_service import DocumentService

    storage = RuntimeStorage(fail_second=True, fail_cleanup=cleanup_fails)
    storage.objects["user-unit-downloads", "history"] = b"historical"
    monkeypatch.setattr(settings, "STORAGE_IMPL", storage)
    monkeypatch.setattr(DocumentService, "check_doc_health", classmethod(lambda *_args: True))
    response = client.post("/api/v1/documents/upload", files=[("file", ("first.txt", b"first")), ("file", ("second.txt", b"second"))])
    body = response.json()
    assert body["code"] == 100 and "data" not in body
    assert storage.objects["user-unit-downloads", "history"] == b"historical"
    if cleanup_fails:
        assert len(storage.objects) == 3 and "cleanup" in body["message"]
    else:
        assert storage.objects == {("user-unit-downloads", "history"): b"historical"}


async def test_cancelled_upload_drains_write_before_compensation(async_db: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    from api.db.services.document_service import DocumentService

    started, finish = threading.Event(), threading.Event()

    class DelayedStorage(RuntimeStorage):
        def put(self, bucket: str, key: str, blob: bytes) -> bool:
            started.set()
            assert finish.wait(5)
            return super().put(bucket, key, blob)

    storage = DelayedStorage()
    monkeypatch.setattr(settings, "STORAGE_IMPL", storage)
    monkeypatch.setattr(DocumentService, "check_doc_health", classmethod(lambda *_args: True))
    task = asyncio.create_task(FileService.upload_infos(async_db, "owner", [runtime_file("a.txt")]))
    assert await asyncio.to_thread(started.wait, 5)
    task.cancel()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert storage.writes == 2 and storage.objects == {}


def test_unknown_cleanup_readback_is_not_confirmed_success(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from api.db.services.document_service import DocumentService

    class UnknownCleanup(RuntimeStorage):
        def obj_exist(self, bucket: str, key: str) -> None:
            return None

    storage = UnknownCleanup(fail_second=True)
    monkeypatch.setattr(settings, "STORAGE_IMPL", storage)
    monkeypatch.setattr(DocumentService, "check_doc_health", classmethod(lambda *_args: True))
    response = client.post("/api/v1/documents/upload", files=[("file", ("first.txt", b"first")), ("file", ("second.txt", b"second"))])
    assert response.json()["code"] == 100 and "cleanup" in response.json()["message"]
    assert "data" not in response.json()


def test_upload_routes_expose_rest_and_sdk_without_retired_alias(client: TestClient) -> None:
    paths = client.app.openapi()["paths"]
    assert "/v1/document/upload_info" not in paths
    assert not any(getattr(route, "path", None) == "/v1/document/upload_info" for route in client.app.routes)
    assert not paths["/api/v1/documents/upload"]["post"].get("deprecated", False)
    assert not paths["/api/v1/files/upload_info"]["post"].get("deprecated", False)
    assert "post" in paths["/v1/document/upload_and_parse"]


@pytest.mark.parametrize("token", [None, "invalid-token"])
def test_rest_upload_requires_valid_identity(client: TestClient, uploaded: list, monkeypatch: pytest.MonkeyPatch, token: str | None) -> None:
    from api.db.services.api_service import APITokenService
    from api.utils.api_utils import async_current_user

    client.app.dependency_overrides.pop(async_current_user)
    monkeypatch.setattr(APITokenService, "query", classmethod(lambda *_args, **_kwargs: []))
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    response = client.post("/api/v1/documents/upload", headers=headers, files={"file": ("a.txt", b"a")})
    assert response.status_code == 401 and response.json()["code"] == 401
    assert uploaded == []
