"""Owned SQL/ES result sets, complete scrolls and non-mutating lazy reads.

ES is an explicitly supplied disposable listener (METADATA_TEST_ES_URL), never
an application index. PostgreSQL uses the normal scratch database fixture.
"""

import json
import os
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import sqlalchemy as sa
from elasticsearch import Elasticsearch
from elasticsearch.helpers import bulk
from sqlalchemy.orm import Session

from api.db.db_models import DocumentMetadata, Knowledgebase
from api.db.services.doc_metadata_service import DocMetadataService
from api.db.services.metadata_store_engine import EngineMetadataStore
from api.db.services.metadata_store_sql import SqlMetadataStore
from common import settings
from common.metadata_utils import aggregate_metadata_rows, apply_meta_data_filter, meta_filter


@pytest.fixture
def metadata_rows(bootstrapped_engine: sa.Engine) -> Iterator[dict[str, Any]]:
    owners = [uuid4().hex, uuid4().hex]
    kb_ids = [uuid4().hex for _ in range(3)]
    rows = [
        (uuid4().hex, {"tag": ["Alice", "Bob"], "number": 0, "boolean": False}),
        (uuid4().hex, {"tag": ["Bob"], "number": "0", "boolean": "False"}),
        (uuid4().hex, {"tag": "ALICE", "number": 1, "boolean": True, "blank": ""}),
        (uuid4().hex, {"tag": "K", "blank": None}),
        (uuid4().hex, {}),
        (uuid4().hex, {"tag": "F", "empty_array": []}),
    ]
    binding = {doc_id: kb_ids[position % 2] for position, (doc_id, _) in enumerate(rows)}
    foreign = uuid4().hex
    with Session(bootstrapped_engine) as db:
        for position, kb_id in enumerate(kb_ids):
            db.add(Knowledgebase(id=kb_id, tenant_id=owners[position % 2], created_by=owners[position % 2], name=kb_id, embd_id="controlled", parser_id="naive", parser_config={}))
        for doc_id, fields in [*rows, (foreign, {"tag": "Alice", "number": 0, "boolean": False})]:
            kb_id = binding.get(doc_id, kb_ids[2])
            owner = owners[kb_ids.index(kb_id) % 2]
            db.add(DocumentMetadata(id=doc_id, tenant_id=owner, kb_id=kb_id, meta_fields=fields))
        db.commit()
    try:
        yield {"engine": bootstrapped_engine, "owners": owners, "kb_ids": kb_ids, "rows": rows, "binding": binding, "foreign": foreign}
    finally:
        with Session(bootstrapped_engine) as db:
            db.execute(sa.delete(DocumentMetadata).where(DocumentMetadata.kb_id.in_(kb_ids)))
            db.execute(sa.delete(Knowledgebase).where(Knowledgebase.id.in_(kb_ids)))
            db.commit()
        with Session(bootstrapped_engine) as db:
            assert db.scalar(sa.select(sa.func.count()).select_from(DocumentMetadata).where(DocumentMetadata.kb_id.in_(kb_ids))) == 0


@pytest.fixture
def es_metadata(metadata_rows: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    url = os.environ.get("METADATA_TEST_ES_URL")
    assert url, "Provide the owned disposable ES listener; missing ES is not a skip"
    client = Elasticsearch(url, request_timeout=30)
    assert client.info()["version"]["number"].startswith("8.")
    env = metadata_rows
    indexes = {owner: "multirag_doc_meta_" + owner for owner in env["owners"]}
    mapping = json.loads(Path("configs/doc_meta_es_mapping.json").read_text())
    try:
        for index in indexes.values():
            client.indices.create(index=index, **mapping)
        actions = []
        for doc_id, fields in [*env["rows"], (env["foreign"], {"tag": "Alice", "number": 0, "boolean": False})]:
            kb_id = env["binding"].get(doc_id, env["kb_ids"][2])
            owner = env["owners"][env["kb_ids"].index(kb_id) % 2]
            actions.append({"_index": indexes[owner], "_id": doc_id, "_source": {"id": doc_id, "kb_id": kb_id, "meta_fields": fields}})
        assert bulk(client, actions, refresh=True)[0] == len(actions)
        monkeypatch.setattr(settings, "DOC_ENGINE", "elasticsearch")
        store = EngineMetadataStore()
        conn = SimpleNamespace(es=client, index_exist=lambda index, _: bool(client.indices.exists(index=index)))
        monkeypatch.setattr(settings, "docStoreConn", conn)
        monkeypatch.setattr(DocMetadataService, "_store", staticmethod(lambda: store))
        yield {**env, "client": client, "indexes": indexes}
    finally:
        for index in indexes.values():
            if client.indices.exists(index=index):
                client.indices.delete(index=index)
            assert not client.indices.exists(index=index)
        client.close()


async def _check_matrix(env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    baseline = aggregate_metadata_rows(env["rows"])
    cases: list[tuple[str, str, Any]] = [
        ("tag", "in", ["ALICE"]),
        ("tag", "not in", ["ALICE"]),
        ("tag", "not contains", "ALICE"),
        ("tag", "in", "F2,F11"),
        ("tag", "in", ["k"]),
        ("tag", "contains", "ä"),
        ("number", "in", [0]),
        ("number", "in", [False]),
        ("number", "in", ["0"]),
        ("boolean", "in", [False]),
        ("boolean", "in", [0]),
        ("boolean", "in", ["FALSE"]),
        ("blank", "empty", ""),
        ("missing", "empty", ""),
        ("empty_array", "not empty", ""),
        ("tag", "not in", []),
        ("tag", "not in", [None]),
        ("tag", "not in", [["Alice"]]),
    ]
    with Session(env["engine"]) as db:
        before = dict(db.execute(sa.select(DocumentMetadata.id, DocumentMetadata.meta_fields)).all())
        for key, op, value in cases:
            for logic in ("and", "or"):
                conditions = [{"key": key, "op": op, "value": value}, {"key": "tag", "op": "in", "value": ["nobody"]}]
                expected = set(meta_filter(baseline, conditions, logic))
                loader = AsyncMock(side_effect=AssertionError("Full aggregation should not be required"))

                async def evaluate(filters: list[dict[str, Any]], selected_logic: str) -> list[str] | None:
                    return DocMetadataService.filter_doc_ids_by_meta_pushdown(db, env["kb_ids"][:2], filters, selected_logic)

                result = await apply_meta_data_filter({"method": "manual", "manual": conditions, "logic": logic}, metas_loader=loader, metadata_filterer=evaluate)
                assert set(result or []) == (expected or {"-999"}), (key, op, value, logic, result, expected)
                assert env["foreign"] not in (result or [])
                scope = [env["rows"][0][0]]
                scoped = await apply_meta_data_filter({"method": "manual", "manual": conditions, "logic": logic}, base_doc_ids=scope, metas_loader=loader, metadata_filterer=evaluate)
                assert set(scoped or []) == ((expected & set(scope)) or {"-999"})
        assert dict(db.execute(sa.select(DocumentMetadata.id, DocumentMetadata.meta_fields)).all()) == before


async def test_real_sql_matches_existing_expanded_metadata(metadata_rows: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "DOC_ENGINE", "milvus")
    monkeypatch.setattr(DocMetadataService, "_store", staticmethod(SqlMetadataStore))
    await _check_matrix(metadata_rows, monkeypatch)


async def test_real_es_matches_existing_expanded_metadata(es_metadata: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = es_metadata
    before = {index: env["client"].count(index=index)["count"] for index in env["indexes"].values()}
    await _check_matrix(env, monkeypatch)
    assert {index: env["client"].count(index=index)["count"] for index in env["indexes"].values()} == before
    for index in env["indexes"].values():
        assert env["client"].indices.stats(index=index)["indices"][index]["total"]["search"]["open_contexts"] == 0


def test_sql_and_es_return_more_than_ten_thousand_matches(es_metadata: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = es_metadata
    kb_id, owner = env["kb_ids"][0], env["owners"][0]
    index = env["indexes"][owner]
    ids = [uuid4().hex for _ in range(10005)]
    with Session(env["engine"]) as db:
        db.execute(sa.insert(DocumentMetadata), [{"id": doc_id, "tenant_id": owner, "kb_id": kb_id, "meta_fields": {"tag": "large"}} for doc_id in ids])
        db.commit()
    assert bulk(env["client"], ({"_index": index, "_id": doc_id, "_source": {"id": doc_id, "kb_id": kb_id, "meta_fields": {"tag": "large"}}} for doc_id in ids), refresh=True)[0] == len(ids)
    conditions = [{"key": "tag", "op": "in", "value": ["LARGE"]}]
    with Session(env["engine"]) as db:
        es_result = DocMetadataService.filter_doc_ids_by_meta_pushdown(db, [kb_id], conditions)
        assert set(es_result or []) == set(ids)
        # Unsupported comparisons use the complete aggregate loader; its ES
        # scan must also cross max_result_window rather than cap or offset.
        baseline = DocMetadataService.get_flatted_meta_by_kbs(db, [kb_id])
        assert set(meta_filter(baseline, conditions)) == set(ids)
        monkeypatch.setattr(settings, "DOC_ENGINE", "milvus")
        monkeypatch.setattr(DocMetadataService, "_store", staticmethod(SqlMetadataStore))
        assert set(DocMetadataService.filter_doc_ids_by_meta_pushdown(db, [kb_id], conditions) or []) == set(ids)
    assert env["client"].count(index=index)["count"] >= len(ids)
    assert env["client"].indices.stats(index=index)["indices"][index]["total"]["search"]["open_contexts"] == 0
