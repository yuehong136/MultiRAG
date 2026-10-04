"""Connector deletion acceptance with isolated SQL, object and vector readback."""

from collections.abc import Iterator
from contextlib import ExitStack
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
import requests
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from api.db import db_models
from api.db.db_models import Connector, Connector2Kb, Document, File, File2Document, Knowledgebase, SyncLogs, Task, get_db
from api.db.services.connector_service import SyncLogsService, connector_doc_id_candidates
from common import settings
from common.constants import TaskStatus
from core.nlp import search
from tests.support.runtime_upload import runtime_upload_api as runtime_upload_api


@pytest.fixture
def connector_sync_api(runtime_upload_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    from api import apps

    env = runtime_upload_api
    sessions = sessionmaker(env["engine"], expire_on_commit=False)

    def scratch_db() -> Iterator[Session]:
        with sessions() as db:
            yield db

    monkeypatch.setattr(apps, "SessionLocal", sessions)
    monkeypatch.setattr(db_models, "SessionLocal", sessions)
    monkeypatch.setitem(apps.app.dependency_overrides, get_db, scratch_db)
    owner, kb_id, name = env["owners"][0], uuid4().hex, f"connector_{uuid4().hex}"
    env["kb"] = kb_id
    env["collection"] = search.index_name_one(owner, name)
    store = settings.docStoreConn
    assert store.db_type() == "milvus" and not store.has_collection(env["collection"])

    def remove_index() -> None:
        store.delete_idx(env["collection"], kb_id)
        assert not store.has_collection(env["collection"])

    def remove_sql() -> None:
        with Session(env["engine"]) as db:
            ids = list(db.scalars(sa.select(Document.id).where(Document.kb_id == kb_id)))
            scoped = [
                (Task, Task.doc_id.in_(ids)),
                (File2Document, File2Document.document_id.in_(ids)),
                (Document, Document.kb_id == kb_id),
                (File, File.tenant_id == owner),
                (Knowledgebase, Knowledgebase.id == kb_id),
            ]
            for model, condition in scoped:
                db.execute(sa.delete(model).where(condition))
            db.commit()
        with env["engine"].connect() as db:
            assert not any(db.scalar(sa.select(sa.func.count()).select_from(model).where(condition)) for model, condition in scoped)

    with ExitStack() as cleanup:
        cleanup.callback(remove_sql)
        cleanup.callback(remove_index)
        with Session(env["engine"]) as db:
            db.add(Knowledgebase(id=kb_id, tenant_id=owner, created_by=owner, name=name, embd_id="scratch-embedding", parser_id="naive", parser_config={}))
            db.commit()
        yield env


async def assert_deleted_sync(env: dict[str, Any], monkeypatch: pytest.MonkeyPatch, driver_type: Any, source_name: str, source_ids: dict[str, str], conf: dict[str, Any], mode: str) -> None:
    owner, kb_id = env["owners"][0], env["kb"]
    connector_id, task_id, link_id = uuid4().hex, uuid4().hex, uuid4().hex
    source = f"{source_name}/{connector_id}"
    original = datetime(2025, 12, 1, tzinfo=UTC)
    ids = {name: connector_doc_id_candidates(kb_id, connector_id, source_ids[name])[-1] for name in ["retained", "stale"]}
    with Session(env["engine"]) as db:
        db.add(Connector(id=connector_id, tenant_id=owner, name="Dropbox scratch", source=source_name, input_type="poll", config=conf, status=TaskStatus.SCHEDULE))
        db.add(Connector2Kb(id=link_id, connector_id=connector_id, kb_id=kb_id))
        db.add(SyncLogs(id=task_id, connector_id=connector_id, kb_id=kb_id, status=TaskStatus.SCHEDULE, poll_range_start=original))
        db.commit()
        errors, created = SyncLogsService.duplicate_and_parse(
            db,
            db.get(Knowledgebase, kb_id),
            [{"id": identifier, "semantic_identifier": name, "extension": ".txt", "blob": name.encode()} for name, identifier in ids.items()],
            owner,
            source,
            auto_parse=False,
        )
        assert not errors and set(created) == set(ids.values())
        locations = {doc.id: doc.location for doc in db.scalars(sa.select(Document).where(Document.id.in_(ids.values())))}
    settings.docStoreConn.create_idx(env["collection"], kb_id, 768)
    assert (
        settings.docStoreConn.insert(
            [{"id": uuid4().hex, "doc_id": identifier, "kb_id": kb_id, "content_with_weight": identifier, "vector": [0.1] * 768, "q_768_vec": [0.1] * 768} for identifier in ids.values()],
            env["collection"],
            kb_id,
        )
        == []
    )
    before_objects = {obj.object_name for obj in env["storage"].list_objects(env["bucket"], recursive=True)}
    assert len(before_objects) == 2
    if mode == "ingestion-error":
        monkeypatch.setattr(SyncLogsService, "duplicate_and_parse", lambda *args: (["controlled ingestion error"], []))
    driver = driver_type(conf)
    current = {"id": task_id, "connector_id": connector_id, "kb_id": kb_id, "tenant_id": owner, "poll_range_start": original, "reindex": "0", "auto_parse": False, "timeout_secs": 30}
    try:
        await driver(current)
        success = mode in {"complete", "empty"}
        expected = {ids["retained"]} if mode == "complete" else set() if mode == "empty" else set(ids.values())
        with Session(env["engine"]) as db:
            log = db.get(SyncLogs, task_id)
            assert log is not None and log.status == (TaskStatus.DONE if success else TaskStatus.FAIL), log.full_exception_trace if log else "missing log"
            assert log.docs_removed_from_index == (2 - len(expected) if success else 0)
            if not success:
                assert log.poll_range_start == original
                assert db.scalar(sa.select(sa.func.count()).select_from(SyncLogs).where(SyncLogs.connector_id == connector_id)) == 1
            assert set(db.scalars(sa.select(Document.id).where(Document.source_type == source))) == expected
            assert db.scalar(sa.select(Knowledgebase.doc_num).where(Knowledgebase.id == kb_id)) == len(expected)
            assert set(db.scalars(sa.select(File2Document.document_id).where(File2Document.document_id.in_(ids.values())))) == expected
        indexed = settings.docStoreConn.query(env["collection"], filter=f'kb_id == "{kb_id}"', output_fields=["doc_id"], consistency_level="Strong")
        assert {row["doc_id"] for row in indexed} == expected
        objects = {obj.object_name for obj in env["storage"].list_objects(env["bucket"], recursive=True)}
        assert objects == {name for name in before_objects if any(locations[identifier] in name for identifier in expected)}
    finally:
        with Session(env["engine"]) as db:
            db.execute(sa.delete(SyncLogs).where(SyncLogs.connector_id == connector_id))
            db.execute(sa.delete(Connector2Kb).where(Connector2Kb.id == link_id))
            db.execute(sa.delete(Connector).where(Connector.id == connector_id))
            db.commit()


def assert_configuration_readback(env: dict[str, Any], source: str, config: dict[str, Any]) -> None:
    base = env["base"] + "/api/v1/connectors"
    headers = {"Authorization": f"Bearer {env['jwt']}"}
    response = requests.post(base, headers=headers, json={"name": "Dropbox configuration", "source": source, "config": config}, timeout=30)
    assert response.status_code == 200 and response.json()["retcode"] == 0
    connector_id = response.json()["data"]["id"]
    try:
        for enabled in [True, False]:
            config = {**config, "sync_deleted_files": enabled}
            response = requests.patch(f"{base}/{connector_id}", headers=headers, json={"config": config}, timeout=30)
            assert response.status_code == 200 and response.json()["retcode"] == 0
            readback = requests.get(f"{base}/{connector_id}", headers=headers, timeout=30)
            assert readback.status_code == 200 and readback.json()["retcode"] == 0
            assert readback.json()["data"]["config"] == config
            with Session(env["engine"]) as db:
                stored = db.get(Connector, connector_id)
                assert stored is not None and stored.config == config
    finally:
        with Session(env["engine"]) as db:
            db.execute(sa.delete(Connector).where(Connector.id == connector_id))
            db.commit()
