"""Both registered SDK downloads infer MIME from the stored, authorized document."""

import sys
from types import SimpleNamespace
from typing import Any
from urllib.parse import quote

import pytest

from api.utils.api_utils import token_required


@pytest.mark.parametrize("route", ["beta", "dataset"])
@pytest.mark.parametrize(
    ("name", "kind", "expected"),
    [
        ("报告 空间.PDF", "doc", "application/pdf"),
        ("note.TXT", "doc", "text/plain; charset=utf-8"),
        ("table.csv", "doc", "text/csv; charset=utf-8"),
        ("photo.PNG", "visual", "image/png"),
        ("readme.md", "doc", "text/markdown; charset=utf-8"),
        ("page.html", "doc", "text/html; charset=utf-8"),
        ("data.json", "doc", "application/json"),
        ("folder/报告.PDF", "doc", "application/pdf"),
        ("unknown.custom", "doc", "application/custom"),
        ("unknown.custom", "visual", "image/custom"),
        ("no-extension", "doc", "application/octet-stream"),
        ("invalid.bad;header", "doc", "application/octet-stream"),
        ("invalid." + "a" * 33, "visual", "application/octet-stream"),
    ],
)
def test_registered_download_mime(client: Any, monkeypatch: pytest.MonkeyPatch, route: str, name: str, kind: str, expected: str) -> None:
    module = sys.modules["api.apps.sdk.doc"]
    document = SimpleNamespace(name=name, type=kind, kb_id="dataset")
    monkeypatch.setattr(module.DocumentService, "query", lambda *args, **kwargs: [document])
    monkeypatch.setattr(module.KnowledgebaseService, "query", lambda *args, **kwargs: [object()])
    monkeypatch.setattr(module.APIToken, "query", lambda *args, **kwargs: [SimpleNamespace(tenant_id="owner")])
    monkeypatch.setattr(module.Tenant, "query", lambda *args, **kwargs: [object()])
    reads = []

    def address(*args: Any, **kwargs: Any) -> tuple[str, str]:
        assert args[1:] == ("document", "owner")
        return "authorized-bucket", "nested/key"

    def read(bucket: str, key: str) -> bytes:
        reads.append((bucket, key))
        return b"\x00exact\xffbytes"

    monkeypatch.setattr(module.File2DocumentService, "get_authorized_document_storage_address", address)
    monkeypatch.setattr(module.settings, "STORAGE_IMPL", SimpleNamespace(get=read))
    client.app.dependency_overrides[token_required] = lambda: "owner"
    path = "/api/v1/documents/document" if route == "beta" else "/api/v1/datasets/dataset/documents/document"
    response = client.get(path, headers={"Authorization": "Bearer test-beta"})
    assert response.status_code == 200 and response.content == b"\x00exact\xffbytes"
    assert response.headers["Content-Type"] == expected
    assert response.headers["Content-Disposition"] == f"attachment; filename*=UTF-8''{quote(name, safe='')}"
    assert reads == [("authorized-bucket", "nested/key")]
