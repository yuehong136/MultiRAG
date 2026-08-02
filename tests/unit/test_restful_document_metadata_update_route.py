from __future__ import annotations

from typing import Any

import pytest

from api.apps.services import document_api_service
from api.db.services.doc_metadata_service import DocMetadataService
from api.db.services.knowledgebase_service import KnowledgebaseService


def test_restful_metadata_update_uses_canonical_static_route(client, monkeypatch):
    calls: list[tuple[Any, ...]] = []

    def _update(db, dataset_id, tenant_id, selector, updates, deletes):
        calls.append((db, dataset_id, tenant_id, selector, updates, deletes))
        return {"updated": 1, "matched_docs": 1}

    monkeypatch.setattr(document_api_service, "batch_update_document_metadata", _update)

    response = client.patch(
        "/api/v1/datasets/kb-1/documents/metadatas",
        json={"selector": {"document_ids": ["doc-1"]}, "updates": [{"key": "author", "value": "Ada"}]},
    )

    assert response.status_code == 200
    assert response.json()["data"] == {"updated": 1, "matched_docs": 1}
    assert calls[0][1:] == (
        "kb-1",
        "tenant-unit",
        {"document_ids": ["doc-1"]},
        [{"key": "author", "value": "Ada"}],
        [],
    )


def test_batch_update_filters_all_dataset_documents_when_ids_are_omitted(db, monkeypatch):
    monkeypatch.setattr(KnowledgebaseService, "accessible", classmethod(lambda cls, db, kb_id, user_id: True))
    monkeypatch.setattr(
        KnowledgebaseService,
        "list_documents_by_ids",
        classmethod(lambda cls, db, kb_ids: ["doc-1", "doc-2"]),
    )
    monkeypatch.setattr(
        DocMetadataService,
        "get_flatted_meta_by_kbs",
        classmethod(lambda cls, db, kb_ids: {"status": {"ready": ["doc-2"]}}),
    )
    monkeypatch.setattr(document_api_service, "convert_conditions", lambda condition: condition)
    monkeypatch.setattr(document_api_service, "meta_filter", lambda metadata, condition, logic: ["doc-2"])
    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        DocMetadataService,
        "batch_update_metadata",
        classmethod(lambda cls, db, kb_id, doc_ids, updates, deletes: calls.append((db, kb_id, doc_ids, updates, deletes)) or 1),
    )

    result = document_api_service.batch_update_document_metadata(
        db,
        "kb-1",
        "user-1",
        {"metadata_condition": {"conditions": [{"name": "status", "value": "ready"}]}},
        [{"key": "owner", "value": "Ada"}],
        [],
    )

    assert result == {"updated": 1, "matched_docs": 1}
    assert calls == [(db, "kb-1", ["doc-2"], [{"key": "owner", "value": "Ada"}], [])]


def test_batch_update_rejects_documents_from_another_dataset(db, monkeypatch):
    monkeypatch.setattr(KnowledgebaseService, "accessible", classmethod(lambda cls, db, kb_id, user_id: True))
    monkeypatch.setattr(KnowledgebaseService, "list_documents_by_ids", classmethod(lambda cls, db, kb_ids: ["doc-1"]))

    with pytest.raises(document_api_service.MetadataBatchUpdateError, match="doc-other"):
        document_api_service.batch_update_document_metadata(
            db,
            "kb-1",
            "user-1",
            {"document_ids": ["doc-1", "doc-other"]},
            [{"key": "owner", "value": "Ada"}],
            [],
        )


def test_metadata_update_routes_replace_removed_legacy_routes(client):
    schema = client.app.openapi()

    assert schema["paths"]["/api/v1/datasets/{dataset_id}/documents/metadatas"]["patch"].get("deprecated") is not True
    assert "/api/v1/datasets/{dataset_id}/metadata/update" not in schema["paths"]
    assert "/v1/document/metadata/update" not in schema["paths"]
    assert "put" not in schema["paths"]["/api/v1/datasets/{dataset_id}/documents/{document_id}"]
