from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from api.apps.restful_apis import chunk as doc_routes
from api.apps.restful_apis import document_download as downloads
from api.db.services.file2document_service import File2DocumentService


async def _streaming_response_body(response: StreamingResponse) -> bytes:
    chunks: list[bytes] = []
    async for chunk in response.body_iterator:
        chunks.append(bytes(chunk))
    return b"".join(chunks)


class _StrictBinaryStorage:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.get_calls: list[tuple[str, str, str | None]] = []

    def get(self, bucket: str, filename: str, tenant_id: str | None = None) -> bytes:
        self.get_calls.append((bucket, filename, tenant_id))
        return self.content

    def obj_exist(self, *_args: object, **_kwargs: object) -> bool:
        raise AssertionError("download_document should not preflight storage with obj_exist")


async def test_download_document_uses_storage_address_and_preserves_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    db = Session()
    binary_content = b"\x00multi\nrag\xff\xfe"
    storage = _StrictBinaryStorage(binary_content)
    doc = SimpleNamespace(id="doc-1", name="resume.bin", type="doc")

    monkeypatch.setattr(doc_routes.KnowledgebaseService, "query", lambda *_args, **_kwargs: [object()])
    monkeypatch.setattr(doc_routes.DocumentService, "query", lambda *_args, **_kwargs: [doc])
    authorized_address = Mock(return_value=("bucket-1", "path/doc.bin"))
    monkeypatch.setattr(File2DocumentService, "get_authorized_document_storage_address", authorized_address)
    monkeypatch.setattr(File2DocumentService, "get_by_document_id", Mock(side_effect=AssertionError("legacy file lookup should not be used")))
    monkeypatch.setattr(doc_routes.settings, "STORAGE_IMPL", storage)

    response = downloads.download_document("kb-1", "doc-1", db=db, tenant_id="tenant-1")

    authorized_address.assert_called_once_with(db, "doc-1", "tenant-1")
    assert isinstance(response, StreamingResponse)
    assert await _streaming_response_body(response) == binary_content
    assert storage.get_calls == [("bucket-1", "path/doc.bin", None)]
    assert response.headers["content-disposition"] == "attachment; filename*=UTF-8''resume.bin"
