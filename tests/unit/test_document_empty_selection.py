"""An explicit empty metadata selection never reaches document storage."""

from typing import Any
from unittest.mock import Mock

import pytest
from sqlalchemy.orm import Session

from api.db.services.document_service import DocumentService


@pytest.mark.parametrize("method", ["get_list", "get_by_kb_id"])
def test_explicit_empty_document_ids_do_not_query_or_read_metadata(db: Session, monkeypatch: pytest.MonkeyPatch, method: str) -> None:
    forbidden = Mock(side_effect=AssertionError("An empty document selection must not query"))
    monkeypatch.setattr(db, "execute", forbidden)
    monkeypatch.setattr(DocumentService, "_attach_metadata", forbidden)
    args: dict[str, Any] = {"kb_id": "kb", "page_number": 1, "items_per_page": 10, "orderby": "create_time", "desc": True, "doc_ids": []}
    if method == "get_by_kb_id":
        args.update(keywords=None, return_empty_metadata=True)
    assert getattr(DocumentService, method)(db, **args) == ([], 0)
    forbidden.assert_not_called()
