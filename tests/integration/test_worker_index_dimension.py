"""Real worker SQL/Redis/Milvus receipts with controlled parsing and providers."""

import copy
import logging
from collections.abc import Iterator
from functools import partial
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock
from uuid import uuid4

import numpy as np
import pytest
import redis
from pymilvus import MilvusClient, connections
from sqlalchemy import delete, event, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from api.db.db_models import Document, Knowledgebase, SourceRecoveryRecord, Task
from api.db.services.document_image_lock import task_image_reservation_key
from api.db.services.document_source_recovery import source_recovery_key
from common.config_utils import CONFIGS
from core.svr import task_executor
from core.utils.milvus_conn import MilvusConnection
from core.utils.redis_conn import REDIS_CONN
from tests.support.resource_journal import ResourceJournal
from tests.support.services import ServiceManager, split_address


@pytest.fixture
def worker_index(bootstrapped_engine: Engine, service_manager: ServiceManager, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    for service in ("postgresql", "redis", "milvus"):
        service_manager.ensure(service)
    journal = ResourceJournal("worker-index-dimension")
    tenant, dataset, document, task, chunk = (uuid4().hex for _ in range(5))
    collection = "worker_t_" + uuid4().hex
    alias = "worker_t_" + uuid4().hex
    keys = [f"{task}-cancel", source_recovery_key(document, task), task_image_reservation_key(task)]
    journal.event(
        "reserve-worker-resources",
        database=bootstrapped_engine.url.database,
        tenant=tenant,
        dataset=dataset,
        document=document,
        task=task,
        chunk=chunk,
        collection=collection,
        alias=alias,
        redis_keys=keys,
    )
    config = CONFIGS["milvus"]
    options = {
        "uri": config["hosts"],
        "user": config.get("username", ""),
        "password": config.get("password", ""),
        "db_name": config.get("db_name") or "default",
        "token": config.get("token", ""),
        "timeout": 30,
    }
    reader = MilvusClient(**options)
    redis_config = CONFIGS["redis"]
    host, port = split_address(redis_config["host"], 6379)
    cache = redis.Redis(
        host=host,
        port=port,
        db=int(redis_config.get("db", 1)),
        username=redis_config.get("username") or None,
        password=redis_config.get("password") or None,
        socket_connect_timeout=3,
        socket_timeout=3,
    )
    try:
        assert not reader.has_collection(collection) and not cache.exists(*keys)
        connections.connect(alias=alias, **options)
        cls = next(cell.cell_contents for cell in MilvusConnection.__closure__ or [] if isinstance(cell.cell_contents, type))
        store = cls.__new__(cls)
        store.logger = logging.getLogger("test.worker.dimension")
        store._using = alias
        monkeypatch.setitem(vars(task_executor.settings), "docStoreConn", store)
        monkeypatch.setattr(REDIS_CONN, "REDIS", cache)
        monkeypatch.setattr(task_executor.search, "index_name_one", lambda *args: collection)
        monkeypatch.setattr(task_executor, "db_connection", lambda: Session(bootstrapped_engine))
        with Session(bootstrapped_engine) as db:
            db.add(Knowledgebase(id=dataset, tenant_id=tenant, created_by=tenant, name="dimension-owned", embd_id="controlled"))
            db.add(Document(id=document, kb_id=dataset, created_by=tenant, type="txt", parser_id="naive", name="title", run="1"))
            db.add(Task(id=task, doc_id=document, progress=0))
            db.commit()

        def require_scratch(connection: Any, *args: Any) -> None:
            assert connection.engine.url == bootstrapped_engine.url, "worker test attempted SQL outside its registered scratch endpoint"

        event.listen(Engine, "before_cursor_execute", require_scratch)
        try:
            yield {
                "engine": bootstrapped_engine,
                "reader": reader,
                "store": store,
                "cache": cache,
                "journal": journal,
                "tenant": tenant,
                "dataset": dataset,
                "document": document,
                "task": task,
                "chunk": chunk,
                "collection": collection,
            }
        finally:
            event.remove(Engine, "before_cursor_execute", require_scratch)
    finally:
        if reader.has_collection(collection):
            reader.drop_collection(collection)
        cache.delete(*keys)
        with Session(bootstrapped_engine) as db:
            db.execute(delete(SourceRecoveryRecord).where(SourceRecoveryRecord.document_id == document))
            db.execute(delete(Task).where(Task.id == task))
            db.execute(delete(Document).where(Document.id == document))
            db.execute(delete(Knowledgebase).where(Knowledgebase.id == dataset))
            db.commit()
        with bootstrapped_engine.connect() as db:
            absent = all(db.execute(select(model.id).where(model.id == identifier)).first() is None for model, identifier in [(Task, task), (Document, document), (Knowledgebase, dataset)])
        readback = {"sql_rows_absent": absent, "collection_absent": not reader.has_collection(collection), "redis_keys_absent": not cache.exists(*keys)}
        journal.event("worker-cleanup-readback", **readback)
        connections.disconnect(alias)
        reader.close()
        cache.close()
        assert all(readback.values())


@pytest.mark.parametrize("default_model", [False, True])
@pytest.mark.parametrize("failure", [None, "probe", "create", "insert"])
async def test_worker_single_probe_real_dimension_and_failure_side_effects(worker_index: dict[str, Any], monkeypatch: pytest.MonkeyPatch, default_model: bool, failure: str | None) -> None:
    env = worker_index
    calls: list[list[str]] = []
    bundles: list[Any] = []

    class Encoder:
        max_length = 128

        def encode(self, texts: list[str]) -> tuple[np.ndarray, int]:
            calls.append(list(texts))
            if failure == "probe":
                raise ValueError("controlled probe failure")
            return np.array([[9.0] * 5 if text == "title" else [1.0] * 5 for text in texts], dtype=np.float32), len(texts) * 3

    def model(*args: Any, **kwargs: Any) -> Encoder:
        bundle = Encoder()
        bundles.append(bundle)
        return bundle

    async def parse(*args: Any) -> list[dict[str, Any]]:
        return [{"pk": env["chunk"], "id": env["chunk"], "doc_id": env["document"], "kb_id": [env["dataset"]], "docnm_kwd": "title", "content_with_weight": "body"}]

    monkeypatch.setattr(task_executor, "LLMBundle", model)
    monkeypatch.setattr(task_executor, "get_model_config_by_type_and_name", lambda *args: {})
    monkeypatch.setattr(task_executor, "get_tenant_default_model_by_type", lambda *args: {})
    monkeypatch.setattr(task_executor, "build_chunks", parse)
    real_create = env["store"].create_collection_with_mapping

    def create(*args: Any, **kwargs: Any) -> None:
        if failure == "create":
            raise ValueError("controlled create failure")
        real_create(*args, **kwargs)

    monkeypatch.setattr(env["store"], "create_collection_with_mapping", create)
    if failure == "insert":
        monkeypatch.setattr(env["store"], "insert", lambda *args: (_ for _ in ()).throw(ValueError("controlled insert failure")))
    task = {
        "id": env["task"],
        "doc_id": env["document"],
        "kb_id": env["dataset"],
        "tenant_id": env["tenant"],
        "task_type": "",
        "from_page": 0,
        "to_page": 1,
        "embd_id": "" if default_model else "controlled",
        "llm_id": "chat",
        "language": "Chinese",
        "name": "title",
        "parser_id": "naive",
        "parser_config": {"filename_embd_weight": 0},
    }
    with Session(env["engine"]) as db:
        if failure in {"probe", "create"}:
            with pytest.raises(ValueError, match=f"controlled {failure} failure"):
                await task_executor.do_handle_task(db, task)
        else:
            await task_executor.do_handle_task(db, task)
    assert len(bundles) == 1 and calls.count(["ok"]) == 1
    exists = env["reader"].has_collection(env["collection"])
    assert exists == (failure not in {"probe", "create"})
    rows: list[dict[str, Any]] = []
    dimensions = {}
    if exists:
        schema = env["reader"].describe_collection(env["collection"])
        dimensions = {field["name"]: field["params"]["dim"] for field in schema["fields"] if field["name"] in {"vector", "q_5_vec"}}
        assert dimensions == {"vector": 5, "q_5_vec": 5}
        rows = env["reader"].query(collection_name=env["collection"], filter=f'doc_id == "{env["document"]}"', output_fields=["*", "vector", "q_5_vec"], consistency_level="Strong")
    with Session(env["engine"]) as reader:
        doc, kb, state = reader.get(Document, env["document"]), reader.get(Knowledgebase, env["dataset"]), reader.get(Task, env["task"])
        assert doc is not None and kb is not None and state is not None
        counters = [doc.chunk_num, doc.token_num, kb.chunk_num, kb.token_num]
        if failure:
            assert counters == [0, 0, 0, 0] and state.progress < 1 and rows == []
        else:
            assert counters == [1, 6, 1, 6] and state.progress == 1
            assert len(rows) == 1 and rows[0]["vector"] == rows[0]["q_5_vec"] == [1.0] * 5
            assert calls == [["ok"], ["title"], ["body"]]
        env["journal"].event(
            "worker-independent-readback",
            failure=failure,
            default_model=default_model,
            probe_count=calls.count(["ok"]),
            calls=calls,
            dimensions=dimensions,
            counters=counters,
            progress=state.progress,
            rows=rows,
        )
    if failure is None:
        # The delivery seam is controlled; completed-state lookup and all
        # before/after SQL/native reads remain real.
        acknowledged = Mock()
        message = SimpleNamespace(get_message=lambda: task, ack=acknowledged)
        monkeypatch.setattr(task_executor, "UNACKED_ITERATOR", iter([message]))
        monkeypatch.setattr(task_executor, "DONE_TASKS", 0)
        with Session(env["engine"]) as db:
            assert await task_executor.collect(db) == (None, None)
        acknowledged.assert_called_once()
        assert task_executor.DONE_TASKS == 1 and calls == [["ok"], ["title"], ["body"]]
        after = env["reader"].query(collection_name=env["collection"], filter=f'doc_id == "{env["document"]}"', output_fields=["*", "vector", "q_5_vec"], consistency_level="Strong")
        with Session(env["engine"]) as db:
            doc, kb, state = db.get(Document, env["document"]), db.get(Knowledgebase, env["dataset"]), db.get(Task, env["task"])
            assert doc is not None and kb is not None and state is not None
            assert [doc.chunk_num, doc.token_num, kb.chunk_num, kb.token_num] == counters and state.progress == 1 and after == rows
        env["journal"].event("redelivery-independent-readback", controlled_delivery=True, ack_count=acknowledged.call_count, calls=calls, counters=counters, native_unchanged=after == rows)


async def test_raptor_checkpoint_keeps_sql_progress_nonterminal(worker_index: dict[str, Any]) -> None:
    env = worker_index
    await task_executor.init_kb({"tenant_id": env["tenant"], "kb_id": env["dataset"]}, "dimension-owned", 5)
    assert (
        env["store"].insert(
            [
                {
                    "pk": env["chunk"],
                    "id": env["chunk"],
                    "doc_id": env["document"],
                    "kb_id": env["dataset"],
                    "content_with_weight": "summary",
                    "vector": [0.0] * 5,
                    "q_5_vec": [0.0] * 5,
                    "raptor_kwd": "raptor",
                    "extra": {"raptor_method": "raptor", "raptor_clustering_method": "gmm"},
                }
            ],
            env["collection"],
            env["dataset"],
        )
        == []
    )
    before = env["reader"].query(collection_name=env["collection"], filter=f'doc_id == "{env["document"]}"', output_fields=["*"], consistency_level="Strong")
    with Session(env["engine"]) as db:
        state = db.get(Task, env["task"])
        assert state is not None
        state.progress = 0.4
        db.commit()
        output = await task_executor.run_raptor_for_kb(
            {"id": env["task"], "tenant_id": env["tenant"], "kb_id": env["dataset"], "name": "dimension-owned"},
            {"raptor": {"scope": "file"}},
            None,
            None,
            5,
            callback=partial(task_executor.set_progress, db, env["task"]),
            doc_ids=[env["document"]],
            db=db,
        )
        assert output == ([], 0, {})
    after = env["reader"].query(collection_name=env["collection"], filter=f'doc_id == "{env["document"]}"', output_fields=["*"], consistency_level="Strong")
    with Session(env["engine"]) as db:
        state = db.get(Task, env["task"])
        assert state is not None and state.progress == pytest.approx(0.4)
        assert "checkpoint" in (state.progress_msg or "")
        assert before == after
        env["journal"].event("checkpoint-independent-readback", progress=state.progress, message=state.progress_msg, native_before=copy.deepcopy(before), native_after=after)
