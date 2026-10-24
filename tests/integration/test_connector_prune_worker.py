"""Prune workers use real SQL, object/index storage and Redis cancellation signals."""

import asyncio
import json
import threading
from collections.abc import Iterator, Sequence
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from api.db.db_models import Connector, Connector2Kb, Document, Knowledgebase, SyncLogs, Task
from api.db.services.connector_service import ConnectorService, SyncLogsService, connector_doc_id_candidates
from api.db.services.document_service import DocumentDeletePrecondition
from api.db.services.file_service import FileService
from common import settings
from common.constants import ConnectorTaskType, FileSource, TaskStatus
from common.data_source.models import SlimDocument
from core.svr.sync_data_source import SyncBase
from core.utils.redis_conn import REDIS_CONN
from tests.support.connector_deleted_sync import connector_sync_api as connector_sync_api
from tests.support.runtime_upload import read_object
from tests.support.runtime_upload import runtime_upload_api as runtime_upload_api


@pytest.mark.parametrize("outcome", ["concurrent", "delete-race", "empty", "partial", "typeerror", "cancel", "timeout"])
async def test_prune_fence_failures_and_cancellation_readback(connector_sync_api: dict[str, Any], outcome: str, monkeypatch: pytest.MonkeyPatch) -> None:
    env = connector_sync_api
    owner, kb_id = env["owners"][0], env["kb"]
    connector_id, task_id, parse_id, updated_parse_id = uuid4().hex, uuid4().hex, uuid4().hex, uuid4().hex
    ids = {name: connector_doc_id_candidates(kb_id, connector_id, name)[-1] for name in ("stale", "updated", "new")}
    source_type = f"s3/{connector_id}"
    redis_key = f"{parse_id}-cancel"
    updated_redis_key = f"{updated_parse_id}-cancel"
    record: dict[str, Any] = {
        "connector": connector_id,
        "task": task_id,
        "parse_task": parse_id,
        "redis_key": redis_key,
        "updated_parse_task": updated_parse_id,
        "updated_redis_key": updated_redis_key,
        "documents": ids,
        "outcome": outcome,
    }
    env["record"]["prune_worker"] = record
    env["record_path"].write_text(json.dumps(env["record"]))

    def upload(names: list[str], prefix: bytes) -> None:
        with Session(env["engine"]) as db:
            errors, imported = SyncLogsService.duplicate_and_parse(
                db,
                db.get(Knowledgebase, kb_id),
                [{"id": ids[name], "semantic_identifier": name, "extension": ".txt", "blob": prefix + name.encode()} for name in names],
                owner,
                source_type,
                auto_parse=False,
            )
            assert not errors and set(imported) == {ids[name] for name in names}

    with Session(env["engine"]) as db:
        db.add(Connector(id=connector_id, tenant_id=owner, name="prune worker", source="s3", input_type="poll", config={"sync_deleted_files": True}, prune_freq=5, status=TaskStatus.SCHEDULE))
        db.add(Connector2Kb(id=uuid4().hex, connector_id=connector_id, kb_id=kb_id))
        db.add(SyncLogs(id=task_id, connector_id=connector_id, kb_id=kb_id, task_type=ConnectorTaskType.PRUNE, status=TaskStatus.SCHEDULE))
        db.commit()
    upload(["stale", "updated"], b"initial ")
    with Session(env["engine"]) as db:
        db.add(Task(id=parse_id, doc_id=ids["stale"], task_type="Parse"))
        db.add(Task(id=updated_parse_id, doc_id=ids["updated"], task_type="Parse"))
        db.commit()
        initial = {row.id: (row.location, row.content_hash, row.update_time) for row in db.scalars(sa.select(Document).where(Document.id.in_(ids.values())))}
    store = settings.docStoreConn
    store.create_idx(env["collection"], kb_id, 768)
    assert (
        store.insert(
            [{"id": uuid4().hex, "doc_id": identifier, "kb_id": kb_id, "content_with_weight": identifier, "vector": [0.1] * 768, "q_768_vec": [0.1] * 768} for identifier in initial],
            env["collection"],
            kb_id,
        )
        == []
    )
    assert not REDIS_CONN.REDIS.exists(redis_key)
    assert not REDIS_CONN.REDIS.exists(updated_redis_key)
    before_objects = {item.object_name for item in env["storage"].list_objects(env["bucket"], recursive=True)}
    release, finished = threading.Event(), threading.Event()
    calls = 0
    injected = False

    def concurrent_upload() -> None:
        upload(["updated", "new"], b"concurrent ")
        # Millisecond ticks alone are insufficient as a version fence.
        with Session(env["engine"]) as db:
            db.execute(sa.update(Document).where(Document.id == ids["updated"]).values(update_time=initial[ids["updated"]][2]))
            db.commit()

    if outcome == "delete-race":
        original_delete = FileService.delete_docs

        def delete_after_update(cls: type[FileService], db: Session, doc_ids: Sequence[str], tenant_id: str, *, delete_preconditions: dict[str, DocumentDeletePrecondition] | None = None) -> str:
            nonlocal injected
            if ids["updated"] in doc_ids:
                assert not injected and delete_preconditions is not None
                injected = True
                # This committed update happens after the outer precheck, but
                # before DocumentService opens its owned deletion transaction.
                concurrent_upload()
            return original_delete(db, doc_ids, tenant_id, delete_preconditions=delete_preconditions)

        monkeypatch.setattr(FileService, "delete_docs", classmethod(delete_after_update))

    class Source:
        def retrieve_all_slim_docs_perm_sync(self, callback: Any = None) -> Iterator[list[SlimDocument]]:
            nonlocal calls
            calls += 1
            try:
                if outcome == "concurrent":
                    concurrent_upload()
                if outcome == "cancel":
                    with Session(env["engine"]) as db:
                        ConnectorService.cancel_tasks(db, connector_id)
                yield []
                if outcome == "partial":
                    raise PermissionError("controlled later page failure")
                if outcome == "typeerror":
                    raise TypeError("controlled snapshot failure")
                if outcome == "timeout":
                    assert release.wait(5)
            finally:
                finished.set()

    class Driver(SyncBase):
        SOURCE_NAME = FileSource.S3

        async def _initialize_connector(self, task: dict[str, Any]) -> None:
            self.connector = Source()

        async def _generate(self, task: dict[str, Any]) -> Any:
            raise AssertionError("Prune must not call a content generator")

    task = {
        "id": task_id,
        "connector_id": connector_id,
        "kb_id": kb_id,
        "tenant_id": owner,
        "task_type": ConnectorTaskType.PRUNE,
        "poll_range_start": None,
        "reindex": "0",
        "auto_parse": False,
        "timeout_secs": 0.05 if outcome == "timeout" else 30,
    }
    try:
        await Driver({"sync_deleted_files": True})(task)
        release.set()
        assert await asyncio.to_thread(finished.wait, 5)
        assert calls == 1
        raced = outcome in {"concurrent", "delete-race"}
        expected = {ids["updated"], ids["new"]} if raced else set() if outcome == "empty" else set(initial)
        success = raced or outcome == "empty"
        assert injected == (outcome == "delete-race")
        with Session(env["engine"]) as db:
            log = db.get(SyncLogs, task_id)
            assert log.status == (TaskStatus.DONE if success else TaskStatus.CANCEL if outcome == "cancel" else TaskStatus.FAIL), log.full_exception_trace
            assert log.docs_removed_from_index == (1 if raced else 2 if outcome == "empty" else 0)
            if success:
                assert log.error_count == 0
            assert log.poll_range_start is None and log.poll_range_end is None
            assert set(db.scalars(sa.select(Document.id).where(Document.source_type == source_type))) == expected
            assert (db.get(Task, parse_id) is None) == success
            assert (db.get(Task, updated_parse_id) is None) == (outcome == "empty")
            following = list(db.scalars(sa.select(SyncLogs).where(SyncLogs.connector_id == connector_id, SyncLogs.id != task_id)))
            assert len(following) == (1 if success else 0)
            assert all(row.task_type == ConnectorTaskType.PRUNE and row.status == TaskStatus.SCHEDULE for row in following)
            for identifier in expected:
                doc = db.get(Document, identifier)
                body = read_object(env["storage"], env["bucket"], f"{kb_id}/{doc.location}")
                assert body == (b"concurrent " if raced else b"initial ") + doc.name.removesuffix(".txt").encode()
        indexed = {row["doc_id"] for row in store.query(env["collection"], filter=f'kb_id == "{kb_id}"', output_fields=["doc_id"], consistency_level="Strong")}
        assert indexed == (set(initial) & expected)
        assert bool(REDIS_CONN.REDIS.exists(redis_key)) == success
        assert bool(REDIS_CONN.REDIS.exists(updated_redis_key)) == (outcome == "empty")
        objects = {item.object_name for item in env["storage"].list_objects(env["bucket"], recursive=True)}
        assert len(objects) == len(expected)
        if not success:
            assert objects == before_objects
        record.update(verified=True, status=log.status, removed=log.docs_removed_from_index)
    finally:
        release.set()
        REDIS_CONN.REDIS.delete(redis_key, updated_redis_key)
        assert not REDIS_CONN.REDIS.exists(redis_key)
        assert not REDIS_CONN.REDIS.exists(updated_redis_key)
        with Session(env["engine"]) as db:
            db.execute(sa.delete(SyncLogs).where(SyncLogs.connector_id == connector_id))
            db.execute(sa.delete(Connector2Kb).where(Connector2Kb.connector_id == connector_id))
            db.execute(sa.delete(Connector).where(Connector.id == connector_id))
            db.commit()
        record["redis_removed"] = True
