"""Typed exact metadata filters through owned HTTP and PostgreSQL resources."""

import json
import os
import subprocess
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa

from api.db.db_models import Document, DocumentMetadata, File, File2Document
from api.db.services.doc_metadata_service import DocMetadataService
from api.db.services.metadata_store_sql import SqlMetadataStore
from tests.support.dataset_management_http import management_api as management_api
from tests.support.dataset_management_http import request_api, sql_state


@pytest.mark.parametrize("kind", ["jwt", "keys"])
def test_typed_falsy_filters_do_not_expand_the_document_scope(management_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    env = management_api
    dataset, matched, other, foreign = env["datasets"][0], *env["documents"]
    file_ids = [uuid4().hex for _ in env["documents"]]
    link_ids = [uuid4().hex for _ in env["documents"]]
    # Exercise the real SQL metadata backend with the fixture's scratch session.
    monkeypatch.setattr(DocMetadataService, "_store", staticmethod(SqlMetadataStore))
    with env["engine"].begin() as db:
        db.execute(sa.update(Document).where(Document.id == other).values(kb_id=dataset))
        db.execute(
            sa.insert(File),
            [
                {"id": file_id, "parent_id": "", "tenant_id": env["users"][0 if index < 2 else 1], "created_by": env["users"][0 if index < 2 else 1], "name": "filter.txt", "type": "txt"}
                for index, file_id in enumerate(file_ids)
            ],
        )
        db.execute(sa.insert(File2Document), [{"id": link, "file_id": file, "document_id": doc} for link, file, doc in zip(link_ids, file_ids, env["documents"], strict=True)])
        db.execute(
            sa.insert(DocumentMetadata),
            [
                {"id": matched, "kb_id": dataset, "tenant_id": env["users"][0], "meta_fields": {"score": 0, "approved": False, "author": "Ada"}},
                {"id": other, "kb_id": dataset, "tenant_id": env["users"][0], "meta_fields": {"score": 1, "approved": True, "author": "Bob"}},
                {"id": foreign, "kb_id": env["datasets"][2], "tenant_id": env["users"][1], "meta_fields": {"score": 0, "approved": False, "author": "Ada"}},
            ],
        )
    try:
        before = sql_state(env)
        with env["engine"].connect() as db:
            metadata_before = list(db.execute(sa.select(DocumentMetadata.__table__).where(DocumentMetadata.id.in_(env["documents"])).order_by(DocumentMetadata.id)).mappings())
        cases: list[tuple[dict[str, Any], set[str]]] = [
            ({"score": 0}, {matched}),
            ({"approved": False}, {matched}),
            ({"score": [0]}, {matched}),
            ({"approved": [False]}, {matched}),
            ({"missing": 0}, set()),
            ({"missing": False}, set()),
            ({"score": 2}, set()),
            ({"approved": False, "author": "Bob"}, set()),
            ({"score": [], "author": "Ada"}, {matched}),
            ({"score": None, "author": "Ada"}, {matched}),
            ({"score": []}, {matched, other}),
            ({"score": None}, {matched, other}),
            ({}, {matched, other}),
        ]
        for metadata, expected in cases:
            response = request_api(env, "GET", f"/datasets/{dataset}/documents", credential=env[kind][0], params={"metadata": json.dumps(metadata)})
            assert response.status_code == 200 and response.json()["code"] == 0, response.text
            data = response.json()["data"]
            assert data["total"] == len(expected), (metadata, data)
            assert {doc["id"] for doc in data["docs"]} == expected, (metadata, data)
        response = request_api(env, "GET", f"/datasets/{dataset}/documents", credential=env[kind][0], params={"metadata": json.dumps({"score": 0}), "ids": other})
        assert response.status_code == 200 and response.json() == {"code": 0, "data": {"total": 0, "docs": []}}, response.text
        if kind == "jwt":
            smoke = subprocess.run(["make", "smoke"], env={**os.environ, "SMOKE_BASE_URL": env["base"]}, capture_output=True, text=True, timeout=60)
            env["record_path"].with_suffix(".smoke.log").write_text(smoke.stdout + smoke.stderr + f"\nexit={smoke.returncode}\n")
            assert smoke.returncode == 0, smoke.stdout + smoke.stderr
        assert sql_state(env) == before
        with env["engine"].connect() as db:
            metadata_after = list(db.execute(sa.select(DocumentMetadata.__table__).where(DocumentMetadata.id.in_(env["documents"])).order_by(DocumentMetadata.id)).mappings())
            assert metadata_after == metadata_before
    finally:
        with env["engine"].begin() as db:
            db.execute(sa.delete(DocumentMetadata).where(DocumentMetadata.id.in_(env["documents"])))
            db.execute(sa.delete(File2Document).where(File2Document.id.in_(link_ids)))
            db.execute(sa.delete(File).where(File.id.in_(file_ids)))
        with env["engine"].connect() as db:
            assert db.scalar(sa.select(sa.func.count()).select_from(DocumentMetadata).where(DocumentMetadata.id.in_(env["documents"]))) == 0
            assert db.scalar(sa.select(sa.func.count()).select_from(File2Document).where(File2Document.id.in_(link_ids))) == 0
            assert db.scalar(sa.select(sa.func.count()).select_from(File).where(File.id.in_(file_ids))) == 0
