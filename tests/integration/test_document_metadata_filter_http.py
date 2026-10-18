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
    empty = uuid4().hex
    env["documents"].append(empty)
    file_ids = [uuid4().hex for _ in env["documents"]]
    link_ids = [uuid4().hex for _ in env["documents"]]
    # Exercise the real SQL metadata backend with the fixture's scratch session.
    monkeypatch.setattr(DocMetadataService, "_store", staticmethod(SqlMetadataStore))
    with env["engine"].begin() as db:
        db.execute(sa.update(Document).where(Document.id == other).values(kb_id=dataset))
        db.execute(sa.insert(Document).values(id=empty, kb_id=dataset, created_by=env["users"][0], name="empty.txt", type="txt", parser_id="naive", parser_config={}))
        db.execute(
            sa.insert(File),
            [
                {"id": file_id, "parent_id": "", "tenant_id": env["users"][1 if index == 2 else 0], "created_by": env["users"][1 if index == 2 else 0], "name": "filter.txt", "type": "txt"}
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
            ({"score": []}, {matched, other, empty}),
            ({"score": None}, {matched, other, empty}),
            ({}, {matched, other, empty}),
            ({"empty_metadata": False}, {matched, other, empty}),
            ({"empty_metadata": False, "score": 0}, {matched}),
            ({"empty_metadata": False, "approved": False}, {matched}),
            ({"empty_metadata": False, "author": "Ada"}, {matched}),
            ({"empty_metadata": True}, {empty}),
            ({"empty_metadata": True, "score": 0, "approved": False}, {empty}),
        ]
        for metadata, expected in cases:
            response = request_api(env, "GET", f"/datasets/{dataset}/documents", credential=env[kind][0], params={"metadata": json.dumps(metadata)})
            assert response.status_code == 200 and response.json()["code"] == 0, response.text
            data = response.json()["data"]
            assert data["total"] == len(expected), (metadata, data)
            assert {doc["id"] for doc in data["docs"]} == expected, (metadata, data)
        for metadata, query_empty in [({"empty_metadata": False, "score": 0}, "true"), ({"empty_metadata": True, "score": 0}, "false")]:
            response = request_api(
                env,
                "GET",
                f"/datasets/{dataset}/documents",
                credential=env[kind][0],
                params={
                    "metadata": json.dumps(metadata),
                    "return_empty_metadata": query_empty,
                    "metadata_condition": json.dumps({"logic": "and", "conditions": [{"name": "author", "comparison_operator": "is", "value": "Ada"}]}),
                },
            )
            assert response.status_code == 200 and response.json()["code"] == 0, response.text
            assert response.json()["data"]["total"] == 1 and [doc["id"] for doc in response.json()["data"]["docs"]] == [empty]
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


def test_filter_aggregates_all_linked_documents_and_ignores_metadata_pagination(management_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = management_api
    dataset, first, second = env["datasets"][0], env["documents"][0], env["documents"][1]
    monkeypatch.setattr(DocMetadataService, "_store", staticmethod(SqlMetadataStore))
    files, links = [uuid4().hex for _ in range(3)], [uuid4().hex for _ in range(3)]
    with env["engine"].begin() as db:
        db.execute(sa.update(Document).where(Document.id == first).values(type="pdf", suffix="pdf", run="3"))
        db.execute(sa.update(Document).where(Document.id == second).values(kb_id=dataset, type="txt", suffix="txt", run="0"))
        db.execute(sa.insert(File), [{"id": identifier, "parent_id": "", "tenant_id": env["users"][0], "created_by": env["users"][0], "name": "facet", "type": "txt"} for identifier in files])
        db.execute(sa.insert(File2Document), [{"id": identifier, "file_id": file_id, "document_id": doc} for identifier, file_id, doc in zip(links, files, [first, first, second], strict=True)])
        db.execute(sa.insert(DocumentMetadata), [{"id": first, "kb_id": dataset, "tenant_id": env["users"][0], "meta_fields": {"author": "Ada"}}])
    try:
        before = sql_state(env)
        response = request_api(
            env,
            "GET",
            f"/datasets/{dataset}/documents",
            credential=env["jwt"][0],
            params={"type": "filter", "page": 999, "page_size": 1, "metadata": "invalid JSON", "metadata_condition": "invalid JSON", "id": "ignored", "ids": "also-ignored"},
        )
        assert response.status_code == 200 and response.json()["code"] == 0, response.text
        assert response.json()["data"] == {"total": 2, "filter": {"suffix": {"pdf": 1, "txt": 1}, "run_status": {"0": 1, "3": 1}, "metadata": {"author": {"Ada": 1}, "empty_metadata": {"true": 1}}}}
        response = request_api(env, "GET", f"/datasets/{dataset}/documents", credential=env["keys"][0], params={"type": "filter", "run": "DONE", "types": "pdf", "suffix": "pdf"})
        assert response.json()["data"]["total"] == 1, response.text
        assert response.json()["data"]["filter"]["suffix"] == {"pdf": 1}
        for query in [{"type": "filter", "run": "bogus"}, {"type": "filter", "types": "exe"}]:
            assert request_api(env, "GET", f"/datasets/{dataset}/documents", credential=env["keys"][0], params=query).json()["code"] == 102
        assert request_api(env, "GET", f"/datasets/{dataset}/documents", credential=env["keys"][1], params={"type": "filter"}).json()["code"] == 102
        assert sql_state(env) == before
    finally:
        with env["engine"].begin() as db:
            db.execute(sa.delete(DocumentMetadata).where(DocumentMetadata.id == first))
            db.execute(sa.delete(File2Document).where(File2Document.id.in_(links)))
            db.execute(sa.delete(File).where(File.id.in_(files)))
        with env["engine"].connect() as db:
            assert db.scalar(sa.select(sa.func.count()).select_from(File).where(File.id.in_(files))) == 0
