"""A metadata storage failure must not become a successful empty facet."""

from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from api.db.services.doc_metadata_service import DocMetadataService
from api.db.services.document_service import DocumentService


def test_filter_metadata_failure_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    db = Session()
    results = iter([SimpleNamespace(scalar=lambda: 1), SimpleNamespace(all=lambda: []), SimpleNamespace(all=lambda: []), SimpleNamespace(all=lambda: [("document",)])])
    monkeypatch.setattr(db, "execute", lambda _statement: next(results))

    def fail_metadata(_db: Session, doc_ids: list[str], kb_id: str) -> dict[str, dict]:
        assert doc_ids == ["document"] and kb_id == "dataset"
        raise RuntimeError("metadata storage unavailable")

    monkeypatch.setattr(DocMetadataService, "get_metadata_for_documents", fail_metadata)
    with pytest.raises(RuntimeError, match="metadata storage unavailable"):
        DocumentService.get_filter_by_kb_id(db, "dataset", "", [], [], [])
