"""Legacy SQL counter failures must stay atomic even after a caught exception."""

import json
import os
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from api.db.db_models import Document, Knowledgebase, Task, Tenant
from api.db.services.document_ingest_service import _set_document
from api.db.services.document_service import DocumentService, queue_raptor_o_graphrag_tasks
from api.db.services.task_service import TaskService
from tests.support.database import scratch_database

Counter = Callable[[Session, str, str, int, int, float | int], int]


def _save(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, default=str, indent=2))


@pytest.fixture(scope="module")
def counter_database(_require_services: Any, tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[sa.Engine, Path]]:
    evidence = Path(os.environ.get("MULTIRAG_E_COUNTER_EVIDENCE_DIR", str(tmp_path_factory.mktemp("legacy-counters"))))
    with scratch_database(evidence) as engine:
        yield engine, evidence


@pytest.fixture
def counters(counter_database: tuple[sa.Engine, Path], request: pytest.FixtureRequest) -> Iterator[dict[str, Any]]:
    engine, evidence = counter_database
    ids = {key: uuid4().hex for key in ["kb", "other_kb", "a", "b", "missing"]}
    path = evidence / (ids["kb"] + ".rows.json")
    env = {"engine": engine, "ids": ids, "evidence": evidence, "case": request.node.name}
    # Persist ownership before creating any rows.
    _save(path, {"database": engine.url.database, "ids": ids, "case": request.node.name, "created": False})
    with Session(engine) as db:
        db.add(Tenant(id=ids["kb"], name="Counter scratch", llm_id="controlled", embd_id="controlled", asr_id="", img2txt_id="", parser_ids="naive"))
        for key in ["kb", "other_kb"]:
            db.add(
                Knowledgebase(
                    id=ids[key], tenant_id=ids["kb"], created_by=ids["kb"], name=key, embd_id="controlled", token_num=200 if key == "kb" else 0, chunk_num=20 if key == "kb" else 0, doc_num=2
                )
            )
        for key in ["a", "b"]:
            db.add(
                Document(
                    id=ids[key],
                    kb_id=ids["kb"],
                    created_by=ids["kb"],
                    parser_id="naive",
                    type="doc",
                    name=key,
                    token_num=100,
                    chunk_num=10,
                    process_duration=5,
                )
            )
        db.commit()
    # SQL Core timestamps provide a stable before/after value independent of ORM events.
    with engine.begin() as db:
        for model, selected in [(Document, [ids["a"], ids["b"]]), (Knowledgebase, [ids["kb"], ids["other_kb"]])]:
            db.execute(sa.update(model).where(model.id.in_(selected)).values(update_time=1234567890000, update_date=datetime(2009, 2, 13)))
    before = _snapshot(env)
    _save(path, {"database": engine.url.database, "ids": ids, "case": request.node.name, "created": True, "before": before})
    try:
        yield env
    finally:
        _save(path, {"database": engine.url.database, "ids": ids, "case": request.node.name, "created": True, "before": before, "after": _snapshot(env)})


def _snapshot(env: dict[str, Any]) -> dict[str, Any]:
    ids = env["ids"]
    with env["engine"].connect() as db:
        return {
            model.__tablename__: {row["id"]: dict(row) for row in db.execute(sa.select(model.__table__).where(model.id.in_(selected))).mappings()}
            for model, selected in [(Document, [ids["a"], ids["b"]]), (Knowledgebase, [ids["kb"], ids["other_kb"]])]
        }


def _assert_ledgers(env: dict[str, Any], tokens: int, chunks: int, duration: float) -> None:
    data, ids = _snapshot(env), env["ids"]
    docs = data[Document.__tablename__]
    kb = data[Knowledgebase.__tablename__][ids["kb"]]
    assert kb["token_num"] == sum(doc["token_num"] for doc in docs.values()) == tokens
    assert kb["chunk_num"] == sum(doc["chunk_num"] for doc in docs.values()) == chunks
    assert sum(doc["process_duration"] for doc in docs.values()) == duration
    for table in data.values():
        for row in table.values():
            assert row["update_time"] == 1234567890000
            assert row["update_date"] == datetime(2009, 2, 13)


@pytest.mark.parametrize("autobegin", [False, True])
@pytest.mark.parametrize("method,sign", [(DocumentService.increment_chunk_num, 1), (DocumentService.decrement_chunk_num, -1)])
def test_success_commits_scoped_deltas_and_preserves_modification_times(counters: dict[str, Any], method: Counter, sign: int, autobegin: bool) -> None:
    env, ids = counters, counters["ids"]
    with Session(env["engine"]) as db:
        if autobegin:
            db.get(Document, ids["a"])
            assert db.in_transaction()
        assert method(db, ids["a"], ids["kb"], 7, 2, 0.5) == 1
        _assert_ledgers(env, 200 + sign * 7, 20 + sign * 2, 10.5)
        # A legitimate zero delta still matches both resources.
        assert method(db, ids["a"], ids["kb"], 0, 0, 0) == 1


@pytest.mark.parametrize("method", [DocumentService.increment_chunk_num, DocumentService.decrement_chunk_num])
@pytest.mark.parametrize("fault", ["wrong_kb", "missing_doc", "fake_doc", "missing_kb", "python_second_update", "sql_second_update"])
def test_failed_pair_cannot_be_committed_after_catching_error(counters: dict[str, Any], method: Counter, fault: str, monkeypatch: pytest.MonkeyPatch) -> None:
    env, ids = counters, counters["ids"]
    doc_id, kb_id = ids["a"], ids["kb"]
    if fault == "missing_doc":
        doc_id = ids["missing"]
    elif fault == "fake_doc":
        doc_id = "graph_raptor_x"
    elif fault == "wrong_kb":
        kb_id = ids["other_kb"]
    elif fault == "missing_kb":
        with env["engine"].begin() as db:
            db.execute(sa.delete(Knowledgebase).where(Knowledgebase.id == kb_id))
    trigger = "fail_counter_" + uuid4().hex
    if fault == "sql_second_update":
        with env["engine"].begin() as db:
            db.execute(sa.text(f"CREATE FUNCTION usr_ai.{trigger}() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'injected counter failure'; END $$"))
            db.execute(sa.text(f"CREATE TRIGGER {trigger} BEFORE UPDATE ON usr_ai.t_ai_knowledgebases FOR EACH ROW WHEN (OLD.id = '{kb_id}') EXECUTE FUNCTION usr_ai.{trigger}()"))
    before = _snapshot(env)
    try:
        with Session(env["engine"]) as db:
            # Retain unrelated request work across the helper's failed savepoint.
            db.execute(sa.update(Knowledgebase).where(Knowledgebase.id == ids["other_kb"]).values(description="request work"))
            assert db.in_transaction()
            original = db.execute

            def fail_second(statement: Any, *args: Any, **kwargs: Any) -> Any:
                if getattr(statement, "is_update", False) and statement.table.name == Knowledgebase.__tablename__:
                    raise RuntimeError("injected Python counter failure")
                return original(statement, *args, **kwargs)

            with monkeypatch.context() as patch:
                if fault == "python_second_update":
                    patch.setattr(db, "execute", fail_second)
                expected = sa.exc.DBAPIError if fault == "sql_second_update" else RuntimeError if fault == "python_second_update" else LookupError
                with pytest.raises(expected):
                    method(db, doc_id, kb_id, 7, 2, 1)
            db.commit()
        before[Knowledgebase.__tablename__][ids["other_kb"]]["description"] = "request work"
        assert _snapshot(env) == before
    finally:
        if fault == "sql_second_update":
            with env["engine"].begin() as db:
                db.execute(sa.text(f"DROP TRIGGER {trigger} ON usr_ai.t_ai_knowledgebases"))
                db.execute(sa.text(f"DROP FUNCTION usr_ai.{trigger}()"))


@pytest.mark.parametrize("same_document", [True, False])
def test_concurrent_sessions_keep_exact_shared_dataset_totals(counters: dict[str, Any], same_document: bool) -> None:
    env, ids = counters, counters["ids"]
    workers, iterations = 8, 12
    barrier = threading.Barrier(workers)

    def run(index: int) -> None:
        document = ids["a"] if same_document or index % 2 == 0 else ids["b"]
        barrier.wait(timeout=15)
        with Session(env["engine"]) as db:
            for _ in range(iterations):
                assert DocumentService.increment_chunk_num(db, document, ids["kb"], 3, 1, 0.25) == 1

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for result in [pool.submit(run, index) for index in range(workers)]:
            result.result(timeout=30)
    count = workers * iterations
    _assert_ledgers(env, 200 + count * 3, 20 + count, 10 + count * 0.25)


def test_locked_clear_serializes_with_legacy_increment_and_decrement(counters: dict[str, Any]) -> None:
    env, ids = counters, counters["ids"]
    barrier = threading.Barrier(5)

    def run(index: int) -> None:
        method = DocumentService.increment_chunk_num if index % 2 == 0 else DocumentService.decrement_chunk_num
        with Session(env["engine"]) as db:
            barrier.wait(timeout=15)
            for _ in range(10):
                method(db, ids["a"], ids["kb"], 2, 1, 0.25)

    with ThreadPoolExecutor(max_workers=4) as pool, Session(env["engine"]) as db:
        doc = db.scalar(sa.select(Document).where(Document.id == ids["a"]).with_for_update())
        kb = db.get(Knowledgebase, ids["kb"])
        assert doc is not None and kb is not None
        # The actual modern ingestion ledger write runs under its Doc row lock.
        _set_document(db, doc, kb, {"token_num": 0, "chunk_num": 0, "process_duration": 0})
        futures = [pool.submit(run, index) for index in range(4)]
        barrier.wait(timeout=15)
        assert all(not future.done() for future in futures)
        _assert_ledgers(env, 200, 20, 10)  # independent reader cannot see the uncommitted clear
        db.commit()
        for future in futures:
            future.result(timeout=30)
    _assert_ledgers(env, 100, 10, 15)


@pytest.mark.parametrize("kind", ["graphrag", "raptor"])
async def test_real_fake_document_queue_and_worker_leave_source_counters_intact(counters: dict[str, Any], monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    from api.db.services import document_service
    from common import settings
    from core.graphrag.general import index
    from core.svr import task_executor
    from core.utils.redis_conn import REDIS_CONN

    env, ids = counters, counters["ids"]
    task_id, queue = uuid4().hex, "legacy-counter-queue-" + uuid4().hex
    path = env["evidence"] / (task_id + ".queue.json")
    record = {"database": env["engine"].url.database, "task_id": task_id, "queue": queue, "kind": kind, "cleaned": False}
    _save(path, record)
    assert not REDIS_CONN.REDIS.exists(queue)
    monkeypatch.setattr(document_service, "get_uuid", lambda: task_id)
    monkeypatch.setattr(settings, "get_svr_queue_name", lambda priority: queue)
    with env["engine"].begin() as db:
        db.execute(sa.update(Knowledgebase).where(Knowledgebase.id == ids["kb"]).values(parser_config={kind: {"use_" + kind: True}}))
    before = _snapshot(env)
    try:
        with Session(env["engine"]) as db:
            assert queue_raptor_o_graphrag_tasks(db, {"id": ids["a"]}, kind, 0, "graph_raptor_x", [ids["a"], ids["b"]]) == task_id
            messages = REDIS_CONN.REDIS.xrange(queue)
            assert len(messages) == 1
            message = json.loads(messages[0][1]["message"])
            assert message["doc_id"] == "graph_raptor_x" and message["doc_ids"] == [ids["a"], ids["b"]]
            task = TaskService.get_task(db, task_id, message["doc_ids"])
            assert task is not None and task["kb_id"] == ids["kb"] and task["doc_id"] == "graph_raptor_x"
            task.update(task_type=message["task_type"], doc_ids=message["doc_ids"])
            progress: list[dict[str, Any]] = []
            graph = AsyncMock(return_value={})
            raptor = AsyncMock(return_value=([], 0, {}))
            monkeypatch.setattr(index, "run_graphrag_for_kb", graph)
            monkeypatch.setattr(task_executor, "run_raptor_for_kb", raptor)
            monkeypatch.setattr(task_executor, "has_canceled", lambda task_id: False)
            monkeypatch.setattr(task_executor, "get_model_config_by_type_and_name", lambda *args: {})
            monkeypatch.setattr(task_executor, "LLMBundle", lambda *args, **kwargs: SimpleNamespace(encode=lambda texts: ([[0.0]], 0)))
            monkeypatch.setattr(task_executor, "init_kb", AsyncMock())
            monkeypatch.setattr(task_executor, "set_progress", lambda *args, **kwargs: progress.append(kwargs))
            await task_executor.do_handle_task(db, task)
            (graph if kind == "graphrag" else raptor).assert_awaited_once()
            assert progress[-1]["prog"] == 1.0
        assert _snapshot(env) == before
        with env["engine"].connect() as db:
            assert db.scalar(sa.select(sa.func.count()).select_from(Document).where(Document.id == "graph_raptor_x")) == 0
        record.update(message=message, consumed=task, counters_unchanged=True)
    finally:
        REDIS_CONN.REDIS.delete(queue)
        assert not REDIS_CONN.REDIS.exists(queue)
        with Session(env["engine"]) as db:
            db.execute(sa.delete(Task).where(Task.id == task_id))
            db.commit()
        with env["engine"].connect() as db:
            assert db.scalar(sa.select(sa.func.count()).select_from(Task).where(Task.id == task_id)) == 0
        record.update(cleaned=True, queue_absent=True, task_absent=True)
        _save(path, record)
