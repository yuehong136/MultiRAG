"""Actual settings -> row task -> parser -> Milvus and SQL metadata readback.

Only the embedding provider and model catalog boundary are controlled. HTTP,
file/object storage, queue, worker orchestration and retrieval storage are real.
"""

import asyncio
import copy
import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from uuid import uuid4

import numpy as np
import pytest
import requests
import sqlalchemy as sa
from alembic import command
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from api.apps.services import dataset_search_service
from api.db import db_models
from api.db.db_models import Document, DocumentMetadata, Knowledgebase, Task
from api.db.services.doc_metadata_service import DocMetadataService
from api.db.services.document_task_service import SupersededDocumentTask, write_task_table_metadata
from api.db.services.file_service import FileService
from api.db.services.metadata_store_sql import SqlMetadataStore
from api.db.services.task_service import TaskService, prepare_parse_tasks
from common import settings
from core.nlp import search
from core.svr import task_executor as worker
from tests.support.document_parse_retirement import index_snapshot
from tests.support.document_parse_retirement import parse_api as parse_api
from tests.support.runtime_upload import runtime_upload_api as runtime_upload_api


class Encoder:
    max_length = 8192

    def encode_queries(self, text: str) -> tuple[np.ndarray, int]:
        return np.array([0.2] * 768), len(text)

    def encode(self, texts: list[str]) -> tuple[np.ndarray, int]:
        return np.array([[0.2] * 768 for _ in texts]), len(texts)


def test_migration_preserves_existing_metadata(bootstrapped_engine: sa.Engine, alembic_cfg: Any) -> None:
    identifier = uuid4().hex
    with bootstrapped_engine.begin() as db:
        db.execute(sa.insert(DocumentMetadata).values(id=identifier, tenant_id=identifier, kb_id=identifier, meta_fields={"manual": ["keep"]}))
        alembic_cfg.attributes["connection"] = db
        command.downgrade(alembic_cfg, "d6f8a0b2c4e6")
        assert "table_metadata_state" not in {c["name"] for c in sa.inspect(db).get_columns("t_ai_document_metadata", schema="usr_ai")}
        command.upgrade(alembic_cfg, "head")
    alembic_cfg.attributes.pop("connection", None)
    with bootstrapped_engine.begin() as db:
        row = db.execute(sa.select(DocumentMetadata.__table__).where(DocumentMetadata.id == identifier)).mappings().one()
        assert row["meta_fields"] == {"manual": ["keep"]} and row["table_metadata_state"] == {}
        db.execute(sa.delete(DocumentMetadata).where(DocumentMetadata.id == identifier))


@pytest.mark.parametrize("fault", [False, True])
def test_real_worker_table_config_and_metadata_filter(parse_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, fault: bool) -> None:
    env = parse_api
    headers = {"Authorization": f"Bearer {env['jwt']}"}
    base = env["base"] + "/api/v1/datasets/" + env["kb"]
    binary = b"title,country,score\nFirst article,Brazil,0\nSecond article,Japan,2\n"
    config = {"table_column_mode": "manual", "table_column_roles": {"title": "vectorize", "country": "metadata", "score": "both"}}
    response = requests.put(base, headers=headers, json={"chunk_method": "table", "parser_config": config}, timeout=30)
    assert response.status_code == 200 and response.json()["code"] == 0, response.text
    invalid = requests.put(base, headers=headers, json={"parser_config": {"table_column_roles": {"title": "invalid"}}}, timeout=30)
    assert invalid.status_code == 422
    with Session(env["engine"]) as db:
        kb = db.get(Knowledgebase, env["kb"])
        assert kb is not None and kb.parser_config["table_column_roles"]["title"] == "indexing"
        errors, files = FileService.upload_document(db, kb, [(binary, "news.csv")], env["owners"][0])
        assert errors == [] and len(files) == 1
        doc_id = files[0][0]["id"]
        assert DocMetadataService.update_document_metadata(db, doc_id, {"owner": "Ada", "country": ["manual"]})
    monkeypatch.setattr(worker, "get_model_config_by_type_and_name", lambda *a: {})
    monkeypatch.setattr(worker, "LLMBundle", lambda *a, **kw: Encoder())

    def parse() -> dict[str, Any]:
        result = requests.post(base + "/documents/parse", headers=headers, json={"document_ids": [doc_id]}, timeout=30)
        assert result.status_code == 200 and result.json()["code"] == 0, result.text
        with Session(env["engine"]) as db:
            identifier = db.scalar(sa.select(Task.id).where(Task.doc_id == doc_id, Task.progress < 1))
            assert identifier is not None
            task = TaskService.get_task(db, identifier)
            assert task is not None
            return task

    task = parse()
    before: dict[str, Any]
    with Session(env["engine"]) as db:
        before = copy.deepcopy(DocMetadataService.get_document_metadata(db, doc_id))
    original_upsert = SqlMetadataStore.upsert_in_transaction
    retrieval_engine = None
    if fault:

        def fail(*a: Any, **kw: Any) -> None:
            raise RuntimeError("controlled table metadata SQL failure")

        monkeypatch.setattr(SqlMetadataStore, "upsert_in_transaction", fail)
    try:
        with Session(env["engine"]) as db:
            if fault:
                with pytest.raises(RuntimeError, match="controlled table metadata SQL failure"):
                    asyncio.run(worker.do_handle_task(db, task))
            else:
                asyncio.run(worker.do_handle_task(db, task))
        rows = index_snapshot(env)
        assert len(rows) == 2
        assert all("country" not in row["content_with_weight"] for row in rows)
        assert all("title" not in row["chunk_data"] for row in rows)
        assert {row["chunk_data"]["country"] for row in rows} == {"Brazil", "Japan"}
        assert all(not any(key.startswith("__table_") for key in row) for row in rows)
        with Session(env["engine"]) as db:
            current = db.get(Task, task["id"])
            assert current is not None
            metadata = DocMetadataService.get_document_metadata(db, doc_id)
            if fault:
                assert metadata == before and current.progress < 1
                state = db.get(DocumentMetadata, doc_id)
                assert state is not None and state.table_metadata_state == {}
                return
            assert current.progress == 1
            assert metadata == {"owner": "Ada", "country": ["manual", "Brazil", "Japan"], "score": [0, 2]}
            assert db.get(Knowledgebase, env["kb"]).parser_config["table_column_names"] == ["title", "country", "score"]
            # Dataset role changes invalidate the real task reuse digest.
            old_digest = current.digest.partition(":ingest-tokens:")[0]
        filtered = requests.get(base + "/documents", headers=headers, params={"metadata": json.dumps({"country": ["Brazil"]})}, timeout=30)
        assert filtered.status_code == 200 and filtered.json()["code"] == 0, filtered.text
        assert [d["id"] for d in filtered.json()["data"]["docs"]] == [doc_id]
        assert filtered.json()["data"]["docs"][0]["meta_fields"]["score"] == [0, 2]
        absent = requests.get(base + "/documents", headers=headers, params={"metadata": json.dumps({"title": ["First article"]})}, timeout=30)
        assert absent.json()["code"] == 0 and absent.json()["data"]["total"] == 0

        @contextmanager
        def scratch_db() -> Iterator[Session]:
            with Session(env["engine"]) as session:
                yield session

        async def bundle(*args: Any, **kwargs: Any) -> Encoder:
            return Encoder()

        monkeypatch.setattr(search, "db_connection", scratch_db)
        retrieval_engine = create_async_engine(env["engine"].url, poolclass=NullPool)
        monkeypatch.setattr(db_models, "async_session_factory", async_sessionmaker(retrieval_engine, expire_on_commit=False))
        monkeypatch.setattr(dataset_search_service, "_bundle", bundle)
        query = {
            "question": "article",
            "search_mode": {"type": "dense"},
            "similarity_threshold": 0,
            "meta_data_filter": {"method": "manual", "manual": [{"key": "country", "op": "=", "value": "Brazil"}]},
        }
        retrieved = requests.post(base + "/search", headers=headers, json=query, timeout=30)
        assert retrieved.status_code == 200 and retrieved.json()["code"] == 0, retrieved.text
        assert retrieved.json()["data"]["total"] == 2
        assert {item["doc_id"] for item in retrieved.json()["data"]["chunks"]} == {doc_id}
        query["meta_data_filter"]["manual"][0]["key"] = "title"
        query["meta_data_filter"]["manual"][0]["value"] = "First article"
        excluded = requests.post(base + "/search", headers=headers, json=query, timeout=30)
        assert excluded.status_code == 200 and excluded.json()["code"] == 0, excluded.text
        assert excluded.json()["data"]["total"] == 0
        config["table_column_roles"] = {"title": "indexing", "country": "indexing", "score": "indexing"}
        saved = requests.put(base, headers=headers, json={"parser_config": config}, timeout=30)
        assert saved.json()["code"] == 0, saved.text
        with Session(env["engine"]) as db:
            doc = db.get(Document, doc_id)
            prepared, _ = prepare_parse_tasks(db, doc.to_dict(), env["kb"], doc.location, 0)
            assert prepared[0]["digest"] != old_digest
        reparsed = parse()
        with Session(env["engine"]) as db:
            asyncio.run(worker.do_handle_task(db, reparsed))
        with Session(env["engine"]) as db:
            assert DocMetadataService.get_document_metadata(db, doc_id) == {"owner": "Ada", "country": ["manual"]}
        assert all(row["chunk_data"] == {} for row in index_snapshot(env))
        # A superseded task cannot restore its old contribution.
        with pytest.raises(SupersededDocumentTask):
            write_task_table_metadata(env["engine"], task["id"], doc_id, env["kb"], {"country": ["forbidden"]})
        with Session(env["engine"]) as db:
            assert DocMetadataService.get_document_metadata(db, doc_id) == {"owner": "Ada", "country": ["manual"]}
        env["record_path"].with_suffix(".table-readback.json").write_text(json.dumps({"first_task": task["id"], "reparse_task": reparsed["id"], "rows": rows, "metadata": metadata}, default=str))
    finally:
        if retrieval_engine is not None:
            asyncio.run(retrieval_engine.dispose())
        monkeypatch.setattr(SqlMetadataStore, "upsert_in_transaction", original_upsert)
        with Session(env["engine"]) as db:
            db.execute(sa.delete(DocumentMetadata).where(DocumentMetadata.id == doc_id))
            db.commit()


def test_multiple_row_tasks_keep_manual_edits_and_reused_contributions(bootstrapped_engine: sa.Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    tenant, dataset, document, first, second, reused = [uuid4().hex for _ in range(6)]
    monkeypatch.setattr(settings, "DOC_ENGINE", "milvus")
    try:
        with Session(bootstrapped_engine) as db:
            db.add(Knowledgebase(id=dataset, tenant_id=tenant, created_by=tenant, name="Table state scratch", embd_id="test"))
            db.add(Document(id=document, kb_id=dataset, created_by=tenant, name="rows.csv", parser_id="table", type="doc", run="1"))
            db.add_all([Task(id=first, doc_id=document, digest="first-range", progress=0), Task(id=second, doc_id=document, digest="second-range", progress=0)])
            db.commit()
            assert DocMetadataService.update_document_metadata(db, document, {"owner": "Ada", "country": ["manual"]})
        write_task_table_metadata(bootstrapped_engine, first, document, dataset, {"country": ["Brazil"], "notes": ["keep, entire; cell"]})
        write_task_table_metadata(bootstrapped_engine, second, document, dataset, {"country": ["Japan"]})
        with Session(bootstrapped_engine) as db:
            assert DocMetadataService.get_document_metadata(db, document)["country"] == ["manual", "Brazil", "Japan"]
            assert DocMetadataService.get_document_metadata(db, document)["notes"] == ["keep, entire; cell"]
            # An edit after aggregation remains independent of generated ownership.
            assert DocMetadataService.update_document_metadata(db, document, {"owner": "Bob", "country": ["manual", "Brazil", "Japan", "added"]})
            db.execute(sa.delete(Task).where(Task.id == first))
            db.add(Task(id=reused, doc_id=document, digest="first-range:ingest-tokens:9", progress=0))
            db.commit()
        # Reused task IDs retain the contribution identified by the parse digest.
        write_task_table_metadata(bootstrapped_engine, second, document, dataset, {"country": ["China"]})
        with Session(bootstrapped_engine) as db:
            metadata = DocMetadataService.get_document_metadata(db, document)
            assert metadata == {"owner": "Bob", "country": ["manual", "added", "Brazil", "China"], "notes": ["keep, entire; cell"]}
            db.execute(sa.delete(Task).where(Task.id == reused))
            db.commit()
        write_task_table_metadata(bootstrapped_engine, second, document, dataset, {})
        with Session(bootstrapped_engine) as db:
            assert DocMetadataService.get_document_metadata(db, document) == {"owner": "Bob", "country": ["manual", "added"]}
            before = copy.deepcopy(db.get(DocumentMetadata, document).table_metadata_state)
            db.execute(sa.update(Document).where(Document.id == document).values(run="2"))
            db.commit()
        with pytest.raises(SupersededDocumentTask):
            write_task_table_metadata(bootstrapped_engine, second, document, dataset, {"country": ["forbidden"]})
        with Session(bootstrapped_engine) as db:
            assert db.get(DocumentMetadata, document).table_metadata_state == before
            assert DocMetadataService.get_document_metadata(db, document) == {"owner": "Bob", "country": ["manual", "added"]}
    finally:
        with bootstrapped_engine.begin() as db:
            db.execute(sa.delete(DocumentMetadata).where(DocumentMetadata.id == document))
            db.execute(sa.delete(Task).where(Task.doc_id == document))
            db.execute(sa.delete(Document).where(Document.id == document))
            db.execute(sa.delete(Knowledgebase).where(Knowledgebase.id == dataset))
