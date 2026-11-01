"""Canonical parse/stop HTTP contracts with real SQL, Redis, Milvus and MinIO.

Parser/model output is not run. Failures are injected at the named native probe
or acknowledged delete boundary; all success, compensation and cleanup reads
use independent SQL/native clients.
"""

import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from uuid import uuid4

import pytest
import requests
import sqlalchemy as sa
from sqlalchemy.orm import Session

from api.db.db_models import Document, File, File2Document, Knowledgebase, Task, UserTenant
from api.db.services import document_ingest_service as ingest
from api.db.services.document_image_lock import task_image_reservation_key
from api.db.services.document_ingest_recovery import recovery_key
from api.db.services.document_source_recovery import source_recovery_key
from api.db.services.document_status_service import insert_source_chunks
from api.db.services.document_task_service import SupersededDocumentTask, increment_task_document
from common import settings
from common.doc_store.document_history import document_history
from core.utils.redis_conn import REDIS_CONN
from tests.support.document_parse_retirement import index_snapshot, object_snapshot, sql_snapshot
from tests.support.document_parse_retirement import parse_api as parse_api
from tests.support.runtime_upload import runtime_upload_api as runtime_upload_api


@pytest.fixture
def stop_api(parse_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    env = parse_api
    ids = {key: uuid4().hex for key in ["document", "sibling", "task", "sibling_task", "file", "sibling_file", "link", "sibling_link", "membership", "chunk", "sibling_chunk", "late_chunk"]}
    keys = {recovery_key(ids["document"]), recovery_key(ids["sibling"])}
    task_ids: set[str] = set()
    evidence_lock = threading.RLock()
    env["stop_ids"] = ids

    def reserve_task(identifier: str, document_id: str) -> None:
        with evidence_lock:
            if identifier in task_ids:
                return
            task_ids.add(identifier)
            new_keys = {identifier + "-cancel", task_image_reservation_key(identifier), source_recovery_key(document_id, identifier)}
            env["journal"].event("reserve-stop-task", task_id=identifier, document_id=document_id, redis_keys=sorted(new_keys))
            keys.update(new_keys)

    def register_tasks(db: Session, *_args: Any) -> None:
        for row in db.new:
            if isinstance(row, Task) and row.doc_id in {ids["document"], ids["sibling"]}:
                reserve_task(row.id, row.doc_id)

    def register_lock(connection: Any, cursor: Any, statement: str, parameters: Any, context: Any, many: bool) -> None:
        if "pg_advisory" in statement:
            env["journal"].event("reserve-stop-advisory-lock", database=env["engine"].url.database, parameters=parameters)

    env["journal"].event(
        "reserve-stop-case",
        database=env["engine"].url.database,
        dataset=env["kb"],
        collection=env["collection"],
        ids=ids,
        redis_keys=sorted(keys),
        objects=[f"{env['kb']}/{key}.txt" for key in ["document", "sibling"]],
    )
    for doc_key, task_key in [("document", "task"), ("sibling", "sibling_task")]:
        reserve_task(ids[task_key], ids[doc_key])
    sa.event.listen(Session, "before_flush", register_tasks)
    sa.event.listen(env["engine"], "before_cursor_execute", register_lock)
    try:
        with Session(env["engine"]) as db:
            for doc_key, task_key, file_key, link_key in [("document", "task", "file", "link"), ("sibling", "sibling_task", "sibling_file", "sibling_link")]:
                db.add(
                    Document(
                        id=ids[doc_key],
                        kb_id=env["kb"],
                        created_by=env["owners"][0],
                        parser_id="naive",
                        parser_config={},
                        name=doc_key + ".txt",
                        location=doc_key + ".txt",
                        type="doc",
                        run="1",
                        progress=0.5,
                        chunk_num=1,
                        token_num=7,
                    )
                )
                db.add(Task(id=ids[task_key], doc_id=ids[doc_key], from_page=0, to_page=1, progress=0.5, chunk_ids=ids["chunk" if doc_key == "document" else "sibling_chunk"]))
                db.add(File(id=ids[file_key], tenant_id=env["owners"][0], created_by=env["owners"][0], parent_id=env["kb"], name=doc_key + ".txt", location=doc_key + ".txt", type="doc"))
                db.add(File2Document(id=ids[link_key], file_id=ids[file_key], document_id=ids[doc_key]))
                env["storage_adapter"].put(env["kb"], doc_key + ".txt", ("Original " + doc_key).encode())
            kb = db.get(Knowledgebase, env["kb"])
            assert kb is not None
            kb.doc_num, kb.chunk_num, kb.token_num = 2, 2, 14
            db.commit()
        assert settings.docStoreConn.insert([_row(env, "document", "chunk"), _row(env, "sibling", "sibling_chunk")], env["collection"], env["kb"]) == []
        yield env
    finally:
        sa.event.remove(Session, "before_flush", register_tasks)
        sa.event.remove(env["engine"], "before_cursor_execute", register_lock)
        # Exact owned cleanup runs while the dependency's listener/index/bucket
        # still exist; its outer fixture then verifies those containers too.
        for doc_key in ["document", "sibling"]:
            if settings.docStoreConn.has_collection(env["collection"]):
                settings.docStoreConn.delete({"doc_id": ids[doc_key]}, env["collection"], env["kb"])
            assert document_history(settings.docStoreConn, env["collection"], env["kb"], ids[doc_key]) == []
            env["storage_adapter"].rm(env["kb"], doc_key + ".txt")
            assert not env["storage_adapter"].obj_exist(env["kb"], doc_key + ".txt")
        scoped = [
            (Task, Task.doc_id.in_([ids["document"], ids["sibling"]])),
            (File2Document, File2Document.id.in_([ids["link"], ids["sibling_link"]])),
            (File, File.id.in_([ids["file"], ids["sibling_file"]])),
            (Document, Document.id.in_([ids["document"], ids["sibling"]])),
            (UserTenant, UserTenant.id == ids["membership"]),
        ]
        with env["engine"].begin() as connection:
            for model, condition in scoped:
                connection.execute(sa.delete(model).where(condition))
        with env["engine"].connect() as reader:
            remaining = {model.__tablename__: reader.scalar(sa.select(sa.func.count()).select_from(model).where(condition)) for model, condition in scoped}
        assert not any(remaining.values()), remaining
        REDIS_CONN.REDIS.delete(*sorted(keys))
        assert not any(REDIS_CONN.REDIS.exists(key) for key in keys)
        env["journal"].event("stop-case-independent-cleanup-readback", sql_remaining=remaining, redis_keys_absent=sorted(keys), native_rows_absent=True, source_objects_absent=True)


def _row(env: dict[str, Any], document_key: str, chunk_key: str) -> dict[str, Any]:
    ids = env["stop_ids"]
    return {
        "pk": ids[chunk_key],
        "id": ids[chunk_key],
        "doc_id": ids[document_key],
        "kb_id": env["kb"],
        "content_with_weight": document_key,
        "docnm_kwd": document_key + ".txt",
        "available_int": 1,
        "vector": [0.2] * 768,
        "q_768_vec": [0.2] * 768,
    }


def _snapshot(env: dict[str, Any]) -> dict[str, Any]:
    return {
        "sql": sql_snapshot(env),
        "index": index_snapshot(env),
        "objects": object_snapshot(env),
        "queue": REDIS_CONN.REDIS.xrange(env["queue"]),
        "cancel": REDIS_CONN.REDIS.get(env["stop_ids"]["task"] + "-cancel"),
    }


def _request(env: dict[str, Any], operation: str = "stop", *, token: str | None = None) -> requests.Response:
    return requests.post(
        env["base"] + f"/api/v1/datasets/{env['kb']}/documents/{operation}",
        json={"document_ids": [env["stop_ids"]["document"]]},
        headers={"Authorization": "Bearer " + (token or env["jwt"])},
        timeout=30,
    )


def _assert_stopped(env: dict[str, Any]) -> None:
    ids = env["stop_ids"]
    with Session(env["engine"]) as reader:
        doc, sibling, kb = reader.get(Document, ids["document"]), reader.get(Document, ids["sibling"]), reader.get(Knowledgebase, env["kb"])
        assert doc is not None and sibling is not None and kb is not None
        assert (doc.run, doc.progress, doc.chunk_num, doc.token_num) == ("2", 0, 0, 0)
        assert (sibling.run, sibling.chunk_num, sibling.token_num) == ("1", 1, 7)
        assert (kb.chunk_num, kb.token_num, kb.doc_num) == (1, 7, 2)
        assert reader.get(Task, ids["task"]) is None
        assert reader.get(Task, ids["sibling_task"]) is not None
    assert all(row["doc_id"] != ids["document"] for row in index_snapshot(env))


def test_canonical_stop_cleans_partial_is_repeatable_and_fences_late_workers(stop_api: dict[str, Any]) -> None:
    env, ids = stop_api, stop_api["stop_ids"]
    before = _snapshot(env)
    assert _request(env, token="invalid").status_code == 401
    denied = _request(env, token=env["api_key"])
    assert denied.status_code == 200 and denied.json()["code"] != 0
    assert _snapshot(env) == before
    for method in ["POST", "DELETE"]:
        retired = requests.request(
            method, env["base"] + f"/api/v1/datasets/{env['kb']}/chunks", headers={"Authorization": "Bearer " + env["jwt"]}, json={"document_ids": [ids["document"]]}, timeout=30
        )
        assert retired.status_code == 404
        assert _snapshot(env) == before
    for _ in range(2):
        response = _request(env)
        assert response.status_code == 200 and response.json() == {"code": 0, "data": {"success_count": 1}}, response.text
        _assert_stopped(env)
    stopped = _snapshot(env)
    assert stopped["objects"] == before["objects"]
    assert stopped["cancel"] is not None
    with pytest.raises(SupersededDocumentTask):
        insert_source_chunks(env["engine"], [_row(env, "document", "late_chunk")], env["collection"], env["kb"], ids["task"])
    with pytest.raises(SupersededDocumentTask):
        increment_task_document(env["engine"], ids["task"], ids["document"], env["kb"], 9, 1, 0)
    assert _snapshot(env) == stopped
    env["journal"].event(
        "canonical-stop-independent-readback", partial_removed=True, counters_zero=True, sibling_unchanged=True, repeated_stop=True, retired_chunks_status=404, late_writes_rejected=True
    )


@pytest.mark.parametrize("operation", ["stop", "parse"])
def test_confirmed_missing_collection_clears_only_selected_ledger(stop_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, operation: str) -> None:
    env, ids = stop_api, stop_api["stop_ids"]
    store = settings.docStoreConn
    store.delete_idx(env["collection"], env["kb"])
    assert not store.has_collection(env["collection"])
    deleted: list[Any] = []
    native = store._get_connection()
    actual_delete = native.delete

    def forbidden_delete(*args: Any, **kwargs: Any) -> Any:
        deleted.append((args, kwargs))
        raise AssertionError("Confirmed missing collection must skip delete")

    with Session(env["engine"]) as db:
        db.add(UserTenant(id=ids["membership"], user_id=env["owners"][1], tenant_id=env["owners"][0], role="admin", invited_by=env["owners"][0]))
        db.commit()
    with monkeypatch.context() as scoped:
        scoped.setattr(native, "delete", forbidden_delete)
        response = _request(env, operation, token=env["api_key"])
    assert response.status_code == 200 and response.json()["code"] == 0, response.text
    assert deleted == []
    assert not store.has_collection(env["collection"])
    with Session(env["engine"]) as reader:
        doc, kb = reader.get(Document, ids["document"]), reader.get(Knowledgebase, env["kb"])
        assert doc is not None and kb is not None
        assert (doc.chunk_num, doc.token_num, kb.chunk_num, kb.token_num) == (0, 0, 1, 7)
        assert doc.run == ("2" if operation == "stop" else "1")
        tasks = list(reader.scalars(sa.select(Task).where(Task.doc_id == ids["document"])))
        assert len(tasks) == (0 if operation == "stop" else 1)
    assert bool(REDIS_CONN.REDIS.xrange(env["queue"])) is (operation == "parse")
    assert native.delete == actual_delete
    env["journal"].event(
        "missing-index-independent-readback", operation=operation, dataset_owner=env["owners"][0], caller=env["owners"][1], skipped_delete=True, document_counters=[0, 0], dataset_counters=[1, 7]
    )


@pytest.mark.parametrize("fault", ["native-probe", "delete-after-effect"])
def test_backend_failure_is_error_and_restores_storage_sql_and_cancel_flag(stop_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, fault: str) -> None:
    env, ids = stop_api, stop_api["stop_ids"]
    store = settings.docStoreConn
    before = _snapshot(env)
    with monkeypatch.context() as scoped:
        if fault == "native-probe":
            client = store._get_connection()

            def fail_probe(*args: Any, **kwargs: Any) -> bool:
                raise ConnectionError("controlled native collection probe failure")

            scoped.setattr(client, "has_collection", fail_probe)
        else:
            native = store._get_connection()
            actual = native.delete
            failed = False

            def fail_after_delete(*args: Any, **kwargs: Any) -> Any:
                nonlocal failed
                result = actual(*args, **kwargs)
                if not failed:
                    failed = True
                    raise ConnectionError("controlled delete reply loss after effect")
                return result

            scoped.setattr(native, "delete", fail_after_delete)
        response = _request(env)
    assert response.status_code == 200 and response.json()["code"] != 0, response.text
    assert response.json()["data"]["success_count"] == 0
    assert _snapshot(env) == before
    assert not REDIS_CONN.REDIS.exists(recovery_key(ids["document"]))
    env["journal"].event("backend-failure-independent-readback", fault=fault, response=response.json(), sql_native_objects_queue_flags_restored=True)


def test_existing_empty_collection_does_not_erase_an_unreconciled_ledger(stop_api: dict[str, Any]) -> None:
    env, ids = stop_api, stop_api["stop_ids"]
    store = settings.docStoreConn
    assert store.delete({"doc_id": ids["document"]}, env["collection"], env["kb"]) == 1
    before = _snapshot(env)
    response = _request(env)
    assert response.json()["code"] != 0 and "history is unavailable" in response.text
    assert _snapshot(env) == before
    env["journal"].event("existing-empty-index-independent-readback", unchanged=True, missing_not_inferred_from_empty=True)


def test_stop_serializes_with_inflight_source_write_and_retries_changed_generation(stop_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env, ids = stop_api, stop_api["stop_ids"]
    store = settings.docStoreConn
    entered, release, selected = threading.Event(), threading.Event(), threading.Event()
    actual_insert, actual_preflight = store.insert, ingest.preflight

    def held_insert(*args: Any, **kwargs: Any) -> list[str]:
        entered.set()
        assert release.wait(10)
        return actual_insert(*args, **kwargs)

    def observed_preflight(*args: Any, **kwargs: Any) -> Any:
        result = actual_preflight(*args, **kwargs)
        selected.set()
        return result

    with monkeypatch.context() as scoped, ThreadPoolExecutor(max_workers=2) as pool:
        scoped.setattr(store, "insert", held_insert)
        scoped.setattr(ingest, "preflight", observed_preflight)
        writer = pool.submit(insert_source_chunks, env["engine"], [_row(env, "document", "late_chunk")], env["collection"], env["kb"], ids["task"])
        try:
            assert entered.wait(10)
            stopper = pool.submit(_request, env)
            assert selected.wait(10)
            assert not stopper.done()
        finally:
            release.set()
        assert writer.result(timeout=20) == []
        first = stopper.result(timeout=20)
        assert first.json()["code"] != 0 and "work changed" in first.text
    response = _request(env)
    assert response.json()["code"] == 0, response.text
    _assert_stopped(env)
    env["journal"].event(
        "concurrent-stop-independent-readback",
        first_response=first.json(),
        retried_response=response.json(),
        inflight_write_serialized=True,
        changed_generation_rejected=True,
        final_counters_zero=True,
    )


def test_generic_ingest_cancel_preserves_partial_history_by_default(stop_api: dict[str, Any]) -> None:
    env, ids = stop_api, stop_api["stop_ids"]
    before = _snapshot(env)
    response = requests.post(env["base"] + "/api/v1/documents/ingest", headers={"Authorization": "Bearer " + env["jwt"]}, json={"doc_ids": [ids["document"]], "run": 2}, timeout=30)
    assert response.status_code == 200 and response.json()["code"] == 0, response.text
    after = _snapshot(env)
    assert after["index"] == before["index"] and after["objects"] == before["objects"] and after["queue"] == before["queue"]
    with Session(env["engine"]) as reader:
        doc, kb, task = reader.get(Document, ids["document"]), reader.get(Knowledgebase, env["kb"]), reader.get(Task, ids["task"])
        assert doc is not None and kb is not None and task is not None
        assert (doc.run, doc.chunk_num, doc.token_num, kb.chunk_num, kb.token_num) == ("2", 1, 7, 2, 14)
        assert task.progress == -1
    env["journal"].event("generic-ingest-cancel-independent-readback", default_history_preserved=True, document_counters=[1, 7], dataset_counters=[2, 14])
