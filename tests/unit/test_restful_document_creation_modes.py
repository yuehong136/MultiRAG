"""Document creation modes share the REST route while keeping legacy consumers usable."""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from api.apps.services import document_api_service
from api.db import FileType
from api.db.db_models import Knowledgebase
from api.db.services.document_service import DocumentService
from api.db.services.file_service import FileService
from api.db.services.knowledgebase_service import KnowledgebaseService
from common.constants import RetCode

_PATH = "/api/v1/datasets/kb1/documents"


def test_empty_mode_accepts_json_and_uses_request_session(client, monkeypatch):
    calls = []

    def _create(db, dataset_id, tenant_id, name):
        calls.append((db, dataset_id, tenant_id, name))
        return {"id": "doc1", "name": name, "dataset_id": dataset_id, "run": "UNSTART"}

    monkeypatch.setattr(document_api_service, "create_empty_document", _create)
    response = client.post(f"{_PATH}?type=empty", json={"name": "  blank.txt  "})

    assert response.status_code == 200
    assert response.json()["code"] == 0
    assert response.json()["data"] == {"id": "doc1", "name": "blank.txt", "dataset_id": "kb1", "run": "UNSTART"}
    assert calls[0][1:] == ("kb1", "tenant-unit", "blank.txt")


@pytest.mark.parametrize("payload", [{}, {"name": "  "}, [], "bad"])
def test_empty_mode_rejects_missing_or_invalid_name(client, monkeypatch, payload):
    monkeypatch.setattr(document_api_service, "create_empty_document", lambda *args: pytest.fail("must not create"))

    response = client.post(f"{_PATH}?type=empty", json=payload)

    assert response.json()["code"] == int(RetCode.ARGUMENT_ERROR)


def test_web_mode_accepts_form_and_maps_service_result(client, monkeypatch):
    calls = []

    async def _create(dataset_id, tenant_id, name, url):
        calls.append((dataset_id, tenant_id, name, url))
        return {"id": "doc-web", "name": "page.pdf", "dataset_id": dataset_id, "run": "UNSTART"}

    monkeypatch.setattr(document_api_service, "create_web_document_async", _create)
    response = client.post(f"{_PATH}?type=web", data={"name": " page ", "url": "https://example.com"})

    assert response.json()["code"] == 0
    assert response.json()["data"]["id"] == "doc-web"
    assert calls == [("kb1", "tenant-unit", "page", "https://example.com")]


@pytest.mark.parametrize("data", [{"url": "https://example.com"}, {"name": "page"}])
def test_web_mode_rejects_missing_fields_before_crawl(client, monkeypatch, data):
    monkeypatch.setattr(document_api_service, "create_web_document_async", lambda *args: pytest.fail("must not crawl"))

    response = client.post(f"{_PATH}?type=web", data=data)

    assert response.json()["code"] == int(RetCode.ARGUMENT_ERROR)


def test_invalid_mode_is_business_error(client):
    response = client.post(f"{_PATH}?type=bogus")

    assert response.json()["code"] == int(RetCode.ARGUMENT_ERROR)


def test_creation_routes_keep_legacy_paths_deprecated(client):
    paths = client.app.openapi()["paths"]

    assert paths["/api/v1/datasets/{dataset_id}/documents"]["post"].get("deprecated") is None
    assert paths["/v1/document/web_crawl"]["post"]["deprecated"] is True
    assert paths["/v1/document/create"]["post"]["deprecated"] is True


def test_empty_mode_creates_virtual_document_and_file_link(db, monkeypatch):
    kb = Knowledgebase(id="kb1", tenant_id="owner", name="dataset", parser_id="naive", pipeline_id="pipe1", parser_config={"chunk_token_num": 128})
    seen = {}
    monkeypatch.setattr(KnowledgebaseService, "get_by_id", classmethod(lambda cls, s, dataset_id: kb))
    monkeypatch.setattr(document_api_service, "check_kb_team_permission", lambda s, dataset, tenant_id: True)
    monkeypatch.setattr(DocumentService, "query", classmethod(lambda cls, s, **kwargs: []))
    monkeypatch.setattr(FileService, "get_kb_folder", classmethod(lambda cls, s, tenant_id: {"id": "root"}))
    monkeypatch.setattr(FileService, "new_a_file_from_kb", classmethod(lambda cls, s, tenant_id, name, parent_id: {"id": "folder"}))
    monkeypatch.setattr(DocumentService, "insert", classmethod(lambda cls, s, doc: seen.update(doc=doc.copy())))
    monkeypatch.setattr(FileService, "add_file_from_kb", classmethod(lambda cls, s, doc, folder_id, tenant_id: seen.update(link=(doc.copy(), folder_id, tenant_id))))
    monkeypatch.setattr(DocumentService, "get_by_id", classmethod(lambda cls, s, doc_id: SimpleNamespace(id=doc_id)))
    monkeypatch.setattr(document_api_service, "map_doc_keys", lambda s, doc: {"id": doc.id})

    result = document_api_service.create_empty_document(db, "kb1", "member", "blank.txt")

    assert result == {"id": seen["doc"]["id"]}
    assert seen["doc"]["type"] == FileType.VIRTUAL
    assert seen["doc"]["pipeline_id"] == "pipe1"
    assert seen["doc"]["created_by"] == "member"
    assert seen["link"] == (seen["doc"], "folder", "owner")


def test_web_mode_checks_access_before_crawl_and_reuses_upload_chain(db, monkeypatch):
    kb = Knowledgebase(id="kb1", tenant_id="owner")
    events = []

    @contextmanager
    def _connection():
        yield db

    monkeypatch.setattr(document_api_service, "db_connection", _connection)
    monkeypatch.setattr(KnowledgebaseService, "get_by_id", classmethod(lambda cls, s, dataset_id: events.append("lookup") or kb))
    monkeypatch.setattr(document_api_service, "check_kb_team_permission", lambda s, dataset, tenant_id: events.append("authorized") or True)
    monkeypatch.setattr(document_api_service, "is_valid_url", lambda url: events.append("url_checked") or True)
    monkeypatch.setattr(document_api_service, "html2pdf", lambda url: events.append("crawled") or b"pdf")

    def _upload(cls, s, dataset, file_contents, tenant_id):
        events.append(("uploaded", file_contents, tenant_id))
        return [], [({"id": "doc-web", "kb_id": "kb1", "name": "page.pdf"}, b"pdf")]

    monkeypatch.setattr(FileService, "upload_document", classmethod(_upload))

    result = document_api_service.create_web_document("kb1", "member", "page", "https://example.com")

    assert result["dataset_id"] == "kb1"
    assert events == ["lookup", "authorized", "url_checked", "crawled", "lookup", "authorized", ("uploaded", [(b"pdf", "page.pdf")], "member")]


def test_web_mode_blocks_private_url_without_fetch_or_upload(db, monkeypatch):
    kb = Knowledgebase(id="kb1", tenant_id="owner")

    @contextmanager
    def _connection():
        yield db

    monkeypatch.setattr(document_api_service, "db_connection", _connection)
    monkeypatch.setattr(KnowledgebaseService, "get_by_id", classmethod(lambda cls, s, dataset_id: kb))
    monkeypatch.setattr(document_api_service, "check_kb_team_permission", lambda s, dataset, tenant_id: True)
    monkeypatch.setattr(document_api_service, "html2pdf", lambda url: pytest.fail("private URL must not be fetched"))
    monkeypatch.setattr(FileService, "upload_document", classmethod(lambda cls, *args: pytest.fail("private URL must not be uploaded")))

    with pytest.raises(document_api_service.DocumentCreationError) as exc_info:
        document_api_service.create_web_document("kb1", "member", "page", "http://127.0.0.1/private")

    assert exc_info.value.retcode == RetCode.ARGUMENT_ERROR


def test_web_mode_rejects_unshared_dataset_before_crawl(db, monkeypatch):
    kb = Knowledgebase(id="kb1", tenant_id="owner")

    @contextmanager
    def _connection():
        yield db

    monkeypatch.setattr(document_api_service, "db_connection", _connection)
    monkeypatch.setattr(KnowledgebaseService, "get_by_id", classmethod(lambda cls, s, dataset_id: kb))
    monkeypatch.setattr(document_api_service, "check_kb_team_permission", lambda s, dataset, tenant_id: False)
    monkeypatch.setattr(document_api_service, "is_valid_url", lambda url: pytest.fail("unauthorized URL must not be resolved"))
    monkeypatch.setattr(document_api_service, "html2pdf", lambda url: pytest.fail("unauthorized URL must not be fetched"))

    with pytest.raises(document_api_service.DocumentCreationError) as exc_info:
        document_api_service.create_web_document("kb1", "member", "page", "https://example.com")

    assert exc_info.value.retcode == RetCode.AUTHENTICATION_ERROR


def test_empty_mode_rejects_duplicate_without_creating_file(db, monkeypatch):
    kb = Knowledgebase(id="kb1", tenant_id="owner")
    monkeypatch.setattr(KnowledgebaseService, "get_by_id", classmethod(lambda cls, s, dataset_id: kb))
    monkeypatch.setattr(document_api_service, "check_kb_team_permission", lambda s, dataset, tenant_id: True)
    monkeypatch.setattr(DocumentService, "query", classmethod(lambda cls, s, **kwargs: [object()]))
    monkeypatch.setattr(FileService, "get_kb_folder", classmethod(lambda cls, *args: pytest.fail("duplicate must not create a file")))

    with pytest.raises(document_api_service.DocumentCreationError, match="Duplicated document name"):
        document_api_service.create_empty_document(db, "kb1", "member", "blank.txt")
