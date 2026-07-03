from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session

from api.apps.services import document_api_service
from api.db import UserTenantRole
from api.db.services.user_service import UserTenantService
from api.utils.validation_utils import UpdateDocumentReq


def test_update_document_req_accepts_scalar_meta_fields():
    req = UpdateDocumentReq(meta_fields={"author": "Ada", "score": 0.95, "tags": ["ai", 1, 2.0]})

    assert req.meta_fields == {"author": "Ada", "score": 0.95, "tags": ["ai", 1, 2.0]}


def test_update_document_req_rejects_nested_meta_fields():
    with pytest.raises(ValidationError):
        UpdateDocumentReq(meta_fields={"nested": {"key": "value"}})

    with pytest.raises(ValidationError):
        UpdateDocumentReq(meta_fields={"tags": ["ok", {"bad": "value"}]})


def test_document_update_write_permission_is_admin_or_owner_only():
    assert UserTenantService.can_update_tenant_resources(UserTenantRole.OWNER)
    assert UserTenantService.can_update_tenant_resources(UserTenantRole.ADMIN)
    assert not UserTenantService.can_update_tenant_resources(UserTenantRole.NORMAL)


def test_update_document_name_only_updates_doc_store_index(monkeypatch):
    calls = {}

    class FakeDocumentService:
        @staticmethod
        def update_by_id(db, document_id, values):
            calls["document_update"] = (db, document_id, values)
            return True

        @staticmethod
        def get_tenant_id(db, document_id):
            calls["tenant_lookup"] = (db, document_id)
            return "tenant-1"

        @staticmethod
        def get_by_id(db, document_id):
            calls["doc_lookup"] = (db, document_id)
            return SimpleNamespace(id=document_id, kb_id="kb-1")

    class FakeFile2DocumentService:
        @staticmethod
        def get_by_document_id(db, document_id):
            calls["file_link_lookup"] = (db, document_id)
            return [SimpleNamespace(file_id="file-1")]

    class FakeFileService:
        @staticmethod
        def get_by_id(db, file_id):
            calls["file_lookup"] = (db, file_id)
            return SimpleNamespace(id=file_id)

        @staticmethod
        def update_by_id(db, file_id, values):
            calls["file_update"] = (db, file_id, values)
            return True

    class FakeKnowledgebaseService:
        @staticmethod
        def get_by_id(db, kb_id):
            calls["kb_lookup"] = (db, kb_id)
            return SimpleNamespace(id=kb_id, name="kb-name")

    class FakeDocStore:
        def index_exist(self, index_name, kb_id):
            calls["index_exist"] = (index_name, kb_id)
            return True

        def update(self, condition, values, index_name, kb_id):
            calls["doc_store_update"] = (condition, values, index_name, kb_id)
            return True

    monkeypatch.setattr(document_api_service, "DocumentService", FakeDocumentService)
    monkeypatch.setattr(document_api_service, "File2DocumentService", FakeFile2DocumentService)
    monkeypatch.setattr(document_api_service, "FileService", FakeFileService)
    monkeypatch.setattr(document_api_service, "KnowledgebaseService", FakeKnowledgebaseService)
    monkeypatch.setattr(document_api_service.settings, "docStoreConn", FakeDocStore())
    monkeypatch.setattr(
        document_api_service.rag_tokenizer,
        "tokenize",
        lambda name: f"tokens:{name}",
    )
    monkeypatch.setattr(
        document_api_service.rag_tokenizer,
        "fine_grained_tokenize",
        lambda tokens: f"fine:{tokens}",
    )
    monkeypatch.setattr(
        document_api_service.search,
        "index_name_one",
        lambda tenant_id, kb_name: f"idx:{tenant_id}:{kb_name}",
    )

    db = Session()
    result = document_api_service.update_document_name_only(db, "doc-1", "new.pdf")

    assert result is None
    assert calls["document_update"] == (db, "doc-1", {"name": "new.pdf"})
    assert calls["file_update"] == (db, "file-1", {"name": "new.pdf"})
    assert calls["doc_store_update"] == (
        {"doc_id": "doc-1"},
        {
            "docnm_kwd": "new.pdf",
            "title_tks": "tokens:new.pdf",
            "title_sm_tks": "fine:tokens:new.pdf",
        },
        "idx:tenant-1:kb-name",
        "kb-1",
    )
