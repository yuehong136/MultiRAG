"""Binary response regressions: active content, unknown hints and safe errors."""

from typing import Any

import pytest

from api.apps.services.document_file_http import document_file_response, generated_attachment_response
from api.db.services.document_file_service import DocumentFile, DocumentFileStorageError, DocumentFileUnavailable


def test_active_content_is_download_only_with_encoded_filename() -> None:
    response = document_file_response(DocumentFile(data=b"<script>untrusted</script>", name='报告 "x".html'))
    assert response.body == b"<script>untrusted</script>"
    assert response.headers["content-disposition"] == "attachment; filename*=UTF-8''%E6%8A%A5%E5%91%8A%20%22x%22.html"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_unknown_type_hint_cannot_inject_headers() -> None:
    response = generated_attachment_response(b"raw bytes", "txt\r\nX-Injected: true")
    assert response.body == b"raw bytes"
    assert response.headers["content-type"] == "application/octet-stream"
    assert "x-injected" not in response.headers


@pytest.mark.parametrize(
    "error,status,code,message", [(DocumentFileUnavailable(), 404, 102, "Document not found!"), (DocumentFileStorageError("private-storage-detail"), 500, 500, "Document could not be read.")]
)
def test_registered_route_hides_internal_failures(client: Any, monkeypatch: pytest.MonkeyPatch, error: Exception, status: int, code: int, message: str) -> None:
    import sys

    async def fail(*args: Any, **kwargs: Any) -> DocumentFile:
        raise error

    monkeypatch.setattr(sys.modules["api.apps.restful_apis.document"], "read_document_file", fail)
    response = client.get("/api/v1/documents/known/preview")
    assert response.status_code == status
    assert response.json() == {"code": code, "message": message, "data": None}
    assert response.headers["cache-control"] == "no-store"
    assert "private-storage-detail" not in response.text
