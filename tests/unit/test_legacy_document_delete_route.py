"""The active Web fallback preflights the entire batch and deduplicates IDs."""

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from api.db.services.document_service import DocumentService
from api.db.services.file_service import FileService


@pytest.mark.parametrize("invalid", ["missing", "foreign-tenant"])
@pytest.mark.parametrize("invalid_first", [False, True])
def test_legacy_delete_rejects_whole_mixed_batch(client: TestClient, monkeypatch: pytest.MonkeyPatch, invalid: str, invalid_first: bool) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(DocumentService, "accessible4deletion", classmethod(lambda cls, db, doc_id, user_id: doc_id == "s3:bucket/file.txt"))
    monkeypatch.setattr(FileService, "delete_docs", classmethod(lambda cls, db, ids, user_id: calls.append(ids)))
    ids = ["s3:bucket/file.txt", invalid]
    response = client.post("/v1/document/rm", json={"doc_id": ids[::-1] if invalid_first else ids})
    assert response.status_code == 200
    assert response.json()["retcode"] == 109
    assert calls == []


def test_legacy_delete_accepts_connector_ids_once(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(DocumentService, "accessible4deletion", classmethod(lambda cls, db, doc_id, user_id: True))

    def delete(cls: Any, db: Session, ids: list[str], user_id: str) -> str:
        calls.append(ids)
        return ""

    monkeypatch.setattr(FileService, "delete_docs", classmethod(delete))
    response = client.post("/v1/document/rm", json={"doc_id": ["s3:bucket/file.txt", "s3:bucket/file.txt"]})
    assert response.status_code == 200
    assert response.json() == {"code": 0, "message": "success", "data": True}
    assert calls == [["s3:bucket/file.txt"]]
