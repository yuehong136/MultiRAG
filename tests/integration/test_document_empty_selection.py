"""No-filter, no-match and explicit selections on real document SQL joins."""

from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from api.db.db_models import File, File2Document
from api.db.services.doc_metadata_service import DocMetadataService
from api.db.services.document_service import DocumentService
from api.db.services.metadata_store_sql import SqlMetadataStore
from tests.support.dataset_management_http import management_api as management_api


@pytest.mark.parametrize("method", ["get_list", "get_by_kb_id"])
def test_document_selection_contract_on_real_sql(management_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, method: str) -> None:
    env = management_api
    dataset, document, owner = env["datasets"][0], env["documents"][0], env["users"][0]
    file_id, link_id = uuid4().hex, uuid4().hex
    monkeypatch.setattr(DocMetadataService, "_store", staticmethod(SqlMetadataStore))
    with env["engine"].begin() as db:
        db.execute(sa.insert(File).values(id=file_id, parent_id="", tenant_id=owner, created_by=owner, name="filter.txt", type="txt"))
        db.execute(sa.insert(File2Document).values(id=link_id, file_id=file_id, document_id=document))
    try:
        for ids, expected in [(None, {document}), ([], set()), ([document], {document}), ([env["documents"][2]], set())]:
            with Session(env["engine"]) as db:
                args = {"kb_id": dataset, "page_number": 1, "items_per_page": 10, "orderby": "create_time", "desc": True, "keywords": None, "doc_ids": ids}
                rows, count = getattr(DocumentService, method)(db, **args)
                assert {row["id"] for row in rows} == expected and count == len(expected)
    finally:
        with env["engine"].begin() as db:
            db.execute(sa.delete(File2Document).where(File2Document.id == link_id))
            db.execute(sa.delete(File).where(File.id == file_id))
        with env["engine"].connect() as db:
            assert db.scalar(sa.select(sa.func.count()).select_from(File2Document).where(File2Document.id == link_id)) == 0
            assert db.scalar(sa.select(sa.func.count()).select_from(File).where(File.id == file_id)) == 0
