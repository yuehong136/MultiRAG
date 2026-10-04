"""Real source rollback and authenticated native chunk PATCH winner regressions."""

import copy
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from uuid import uuid4

import numpy as np
import pytest
import requests
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.orm import Session

from api.db.db_models import Document, SourceRecoveryRecord, Task
from api.db.services import document_status_service as status_service
from api.db.services.document_source_recovery import SourceRecovery, SourceRecoveryConflict, load_source_recovery, prepare_source_recovery, source_recovery_key
from api.db.services.llm_service import LLMBundle
from common import settings
from core.utils.redis_conn import REDIS_CONN
from tests.support.document_parser_update import bootstrapped_engine as bootstrapped_engine
from tests.support.document_parser_update import image_http_api as image_http_api
from tests.support.document_parser_update import image_resources as image_resources
from tests.support.document_parser_update import parser_api as parser_api
from tests.support.document_parser_update import parser_database as parser_database
from tests.support.document_parser_update import patch, save, snapshot


def business_sql(stores: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in stores["sql"].items() if key != SourceRecoveryRecord.__tablename__}


@pytest.mark.parametrize("fault", ["flush", "commit"])
@pytest.mark.parametrize("image", ["same", "none"])
def test_source_recovery_preserves_authenticated_same_chunk_winner(parser_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, fault: str, image: str) -> None:
    env = parser_api
    headers = {"Authorization": "Bearer " + env["tokens"]["owner"]}
    response = requests.post(env["base"] + "/api/v1/documents/ingest", json={"doc_ids": [env["docs"]["b"]], "run": 1}, headers=headers, timeout=30)
    assert response.status_code == 200 and response.json()["code"] == 0
    with Session(env["engine"]) as db:
        task_id = db.scalar(sa.select(Task.id).where(Task.doc_id == env["docs"]["b"], Task.progress < 1, Task.progress >= 0))
    assert isinstance(task_id, str)
    original = copy.deepcopy(env["parser_record"]["source_rows"][3])
    if image == "none":
        assert settings.docStoreConn.update({"id": original["id"]}, {"img_id": ""}, env["collections"]["kb"], env["ids"]["kb"])
        original["img_id"] = ""
    row = copy.deepcopy(original)
    row.update(content_with_weight="Source attempt before ordinary SQL fault", q_768_vec=[0.2] * 768, vector=[0.2] * 768)
    key = source_recovery_key(env["docs"]["b"], task_id)
    env["parser_record"]["manifest"]["redis_keys"].append(key)
    save(env["parser_record_path"], env["parser_record"])

    before = snapshot(env)
    env["parser_record"]["source_before_attempt"] = before
    save(env["parser_record_path"], env["parser_record"])
    rolled_back, winner_done, inserted = threading.Event(), threading.Event(), threading.Event()
    source_thread: list[int] = []
    old_insert, old_flush, old_commit, old_rollback = settings.docStoreConn.insert, Session.flush, Session.commit, Session.rollback
    raised = False

    class Provider:
        def encode(self, texts: list[str]) -> tuple[np.ndarray[Any, Any], int]:
            with Session(env["engine"]) as probe:
                with pytest.raises(sa.exc.OperationalError) as held:
                    probe.scalar(sa.select(Document.id).where(Document.id == env["docs"]["b"]).with_for_update(nowait=True))
                assert held.value.orig.sqlstate == "55P03"
            return np.asarray([[0.7] * 768 for _ in texts]), 3

    class Embedding(LLMBundle):
        def __init__(self, db: Session) -> None:
            # Use actual LLMBundle.encode and its session-release behavior;
            # only external provider output and construction are controlled.
            self.db = db
            self.langfuse = None
            self.max_length = 8192
            self.model_config = {"llm_factory": "Builtin"}
            self.mdl = Provider()

    monkeypatch.setitem(vars(sys.modules["api.apps.restful_apis.chunk"]), "_embedding_model", lambda db, *args: Embedding(db))

    def insert(chunks: list[dict[str, Any]], *args: Any, **kwargs: Any) -> list[str]:
        result = old_insert(chunks, *args, **kwargs)
        if source_thread and threading.get_ident() == source_thread[0]:
            assert result == []
            inserted.set()
        return result

    def fail(db: Session) -> None:
        nonlocal raised
        if source_thread and threading.get_ident() == source_thread[0] and inserted.is_set() and not raised:
            raised = True
            raise RuntimeError("Controlled ordinary source SQL " + fault + " failure")

    def flush(db: Session, *args: Any, **kwargs: Any) -> None:
        if fault == "flush":
            fail(db)
        old_flush(db, *args, **kwargs)

    def commit(db: Session) -> None:
        if fault == "commit":
            fail(db)
        old_commit(db)

    def rollback(db: Session) -> None:
        old_rollback(db)
        if source_thread and threading.get_ident() == source_thread[0] and raised and not rolled_back.is_set():
            rolled_back.set()
            assert winner_done.wait(20)

    def source() -> str:
        source_thread.append(threading.get_ident())
        with pytest.raises(SourceRecoveryConflict, match="native rows") as caught:
            status_service.insert_source_chunks(env["engine"], [row], env["collections"]["kb"], env["ids"]["kb"], task_id)
        assert caught.value.outcome == "unknown"
        return "unknown"

    winner: dict[str, Any] = {}
    path = f"/api/v1/datasets/{env['ids']['kb']}/documents/{env['docs']['b']}/chunks/{row['id']}"
    with monkeypatch.context() as failures:
        failures.setattr(settings.docStoreConn, "insert", insert)
        failures.setattr(Session, "flush", flush)
        failures.setattr(Session, "commit", commit)
        failures.setattr(Session, "rollback", rollback)
        with ThreadPoolExecutor(max_workers=1) as pool:
            attempt = pool.submit(source)
            try:
                assert rolled_back.wait(20)
                response = requests.patch(env["base"] + path, json={"content": "Authenticated later native winner"}, headers=headers, timeout=20)
                assert response.status_code == 200 and response.json()["code"] == 0
                winner.update(snapshot(env))
                assert business_sql(winner) == business_sql(before) and winner["objects"] == before["objects"] and winner["queue"] == before["queue"]
                native = next(item for item in winner["index"]["kb"]["rows"] if item["id"] == row["id"])
                assert native["content_with_weight"] == "Authenticated later native winner" and native["img_id"] == original["img_id"]
                assert native["vector"] and native["q_768_vec"] and native["q_768_vec"] != row["q_768_vec"]
                env["parser_record"]["events"].append({"path": path, "status": response.status_code, "body": response.json(), "stores": winner, "fault": fault, "image": image})
                save(env["parser_record_path"], env["parser_record"])
            finally:
                winner_done.set()
            assert attempt.result(20) == "unknown"
    after = snapshot(env)
    assert business_sql(after) == business_sql(winner)
    for field in ["index", "objects", "queue", "image_reservations"]:
        assert after[field] == winner[field], field
    assert after["redis"][key]["value"] and after["redis_states"][key]["dump_hex"] and not after["redis_states"][key]["absent"]
    material = load_source_recovery(env["docs"]["b"], task_id, env["engine"])
    assert material is not None and material.data["nonce"] and material.data["task_id"] == task_id
    assert material.data["current_native"] == [item for item in winner["index"]["kb"]["rows"] if item["doc_id"] == env["docs"]["b"]]
    assert material.data["original_native"] != material.data["applied_native"] != material.data["current_native"]
    for name in ["original_native", "applied_native", "current_native"]:
        assert all(item["vector"] and item["q_768_vec"] for item in material.data[name])
    assert material.data["original_doc"] == material.data["current_doc"] and material.data["original_task"] == material.data["current_task"]
    assert json.loads(after["redis"][key]["value"])["payload"]["nonce"] == material.data["nonce"]
    sql_material = after["sql"][SourceRecoveryRecord.__tablename__]
    assert len(sql_material) == 1 and sql_material[0]["id"] == material.data["nonce"] and sql_material[0]["wire"] == material.wire
    with pytest.raises(SourceRecoveryConflict, match="before retry"):
        status_service.insert_source_chunks(env["engine"], [row], env["collections"]["kb"], env["ids"]["kb"], task_id)
    assert snapshot(env) == after
    env["parser_record"]["source_native_unknown"] = {"outcome": "unknown", "stores": after, "journal": material.data}
    save(env["parser_record_path"], env["parser_record"])
    # An explicit new document mode retires this generation and its journal.
    patch(env, {"chunk_method": "paper"}, key="b")
    assert not REDIS_CONN.REDIS.exists(key)
    final = snapshot(env)
    assert final["redis_states"][key]["absent"] and final["redis_states"][key]["ttl"] == -2
    assert not final["sql"][SourceRecoveryRecord.__tablename__]
    env["parser_record"]["source_native_retired"] = final
    save(env["parser_record_path"], env["parser_record"])


def test_source_intent_survives_first_redis_write_rejection(parser_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = parser_api
    headers = {"Authorization": "Bearer " + env["tokens"]["owner"]}
    response = requests.post(env["base"] + "/api/v1/documents/ingest", json={"doc_ids": [env["docs"]["b"]], "run": 1}, headers=headers, timeout=30)
    assert response.status_code == 200 and response.json()["code"] == 0
    with Session(env["engine"]) as db:
        task_id = db.scalar(sa.select(Task.id).where(Task.doc_id == env["docs"]["b"], Task.progress == 0))
    assert isinstance(task_id, str)
    row = copy.deepcopy(env["parser_record"]["source_rows"][3])
    row["content_with_weight"] = "Source must never reach native storage"
    key = source_recovery_key(env["docs"]["b"], task_id)
    env["parser_record"]["manifest"]["redis_keys"].append(key)
    before = snapshot(env)
    env["parser_record"]["source_before_attempt"] = before
    save(env["parser_record_path"], env["parser_record"])
    old_set = REDIS_CONN.REDIS.set
    rejected: list[str] = []

    def reject(name: str, value: Any, *args: Any, **kwargs: Any) -> Any:
        if name == key:
            rejected.append(name)
            raise ConnectionError("Controlled rejection before Redis accepted SET")
        return old_set(name, value, *args, **kwargs)

    with monkeypatch.context() as fault:
        fault.setattr(REDIS_CONN.REDIS, "set", reject)
        with pytest.raises(SourceRecoveryConflict, match="before native writes"):
            status_service.insert_source_chunks(env["engine"], [row], env["collections"]["kb"], env["ids"]["kb"], task_id)
    failed = snapshot(env)
    assert rejected == [key]
    assert business_sql(failed) == business_sql(before)
    for field in ["index", "objects", "queue", "image_reservations", "redis", "redis_states"]:
        assert failed[field] == before[field], field
    assert failed["redis_states"][key] == {"type": "none", "dump_hex": "", "ttl": -2, "absent": True}
    material = load_source_recovery(env["docs"]["b"], task_id, env["engine"])
    assert material is not None and material.data["phase"] == "intent" and material.data["nonce"]
    assert material.data["original_native"] == [item for item in before["index"]["kb"]["rows"] if item["doc_id"] == env["docs"]["b"]]
    assert material.data["chunks"] == [row] and material.data["applied_native"] is None
    sql_material = failed["sql"][SourceRecoveryRecord.__tablename__]
    assert len(sql_material) == 1 and sql_material[0]["id"] == material.data["nonce"] and sql_material[0]["wire"] == material.wire
    path = f"/api/v1/datasets/{env['ids']['kb']}/documents/{env['docs']['b']}/chunks"
    response = requests.patch(env["base"] + path, json={"chunk_ids": [row["id"]], "available_int": 0}, headers=headers, timeout=20)
    assert response.status_code == 200 and response.json()["code"] == 0
    winner = snapshot(env)
    with Session(env["engine"]) as db:
        task = db.get(Task, task_id)
        assert task is not None and task.doc_id == env["docs"]["b"] and task.progress == 0
    with pytest.raises(SourceRecoveryConflict, match="before retry"):
        status_service.insert_source_chunks(env["engine"], [row], env["collections"]["kb"], env["ids"]["kb"], task_id)
    assert snapshot(env) == winner
    env["parser_record"]["first_journal_rejected"] = {"outcome": "unknown", "stores": failed, "journal": material.data, "winner_stores": winner, "same_current_task_retry_blocked": True}
    env["parser_record"]["events"].append({"path": path, "status": response.status_code, "body": response.json(), "stores": winner})
    save(env["parser_record_path"], env["parser_record"])
    patch(env, {"chunk_method": "paper"}, key="b")
    final = snapshot(env)
    assert not final["sql"][SourceRecoveryRecord.__tablename__] and final["redis_states"][key]["absent"]
    env["parser_record"]["source_native_retired"] = final
    save(env["parser_record_path"], env["parser_record"])


def test_source_confirms_first_redis_write_with_lost_reply(parser_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = parser_api
    old_set = REDIS_CONN.REDIS.set
    lost: list[str] = []

    def accept_then_lose(name: str, value: Any, *args: Any, **kwargs: Any) -> Any:
        result = old_set(name, value, *args, **kwargs)
        if name.startswith("document-source-recovery:" + env["docs"]["b"] + ":") and not lost:
            assert result is True
            lost.append(name)
            captured = snapshot(env)
            material = json.loads(value)["payload"]
            assert material["phase"] == "intent" and material["nonce"] and captured["redis_states"][name]["dump_hex"]
            assert captured["redis"][name]["value"] == value and captured["redis"][name]["ttl"] == -1
            assert captured["index"] == env["parser_record"]["source_before_attempt"]["index"]
            sql = captured["sql"][SourceRecoveryRecord.__tablename__]
            assert len(sql) == 1 and sql[0]["id"] == material["nonce"] and sql[0]["wire"] == value
            env["parser_record"]["first_journal_accepted_lost_reply"] = {"key": name, "nonce": material["nonce"], "stores": captured}
            save(env["parser_record_path"], env["parser_record"])
            raise ConnectionError("Controlled reply loss after real Redis SET acceptance")
        return result

    monkeypatch.setattr(REDIS_CONN.REDIS, "set", accept_then_lose)
    test_source_recovery_preserves_authenticated_same_chunk_winner(env, monkeypatch, "flush", "same")
    assert len(lost) == 1
    assert env["parser_record"]["source_native_unknown"]["journal"]["nonce"] == env["parser_record"]["first_journal_accepted_lost_reply"]["nonce"]


@pytest.mark.parametrize("writer", ["rest_switch", "rest_switch_mixed", "legacy_set", "legacy_switch", "legacy_switch_mixed"])
def test_chunk_writers_reject_other_document_before_any_mutation(parser_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, writer: str) -> None:
    env = parser_api
    a, b = env["parser_record"]["source_rows"][1], env["parser_record"]["source_rows"][3]
    headers = {"Authorization": "Bearer " + env["tokens"]["owner"]}
    before = snapshot(env)

    def write(ids: list[str]) -> requests.Response:
        if writer.startswith("rest_switch"):
            return requests.patch(env["base"] + f"/api/v1/datasets/{env['ids']['kb']}/documents/{env['docs']['a']}/chunks", json={"chunk_ids": ids, "available_int": 1}, headers=headers, timeout=20)
        if writer == "legacy_set":
            return requests.post(
                env["base"] + "/v1/chunk/set", json={"doc_id": env["docs"]["a"], "chunk_id": ids[0], "content_with_weight": "Legitimate document A replacement"}, headers=headers, timeout=20
            )
        return requests.post(env["base"] + "/v1/chunk/switch", json={"doc_id": env["docs"]["a"], "chunk_ids": ids, "available_int": 1}, headers=headers, timeout=20)

    response = write([a["id"], b["id"]] if writer.endswith("mixed") else [b["id"]])
    code_field = "code" if writer.startswith("rest_switch") else "retcode"
    assert response.status_code == 200 and response.json()[code_field] != 0
    rejected = snapshot(env)
    assert rejected == before
    env["parser_record"]["writer_target_rejected"] = {"writer": writer, "body": response.json(), "before": before, "after": rejected, "whole_batch_no_effects": True}

    class Provider:
        def encode(self, texts: list[str]) -> tuple[np.ndarray[Any, Any], int]:
            with Session(env["engine"]) as probe:
                with pytest.raises(sa.exc.OperationalError) as held:
                    probe.scalar(sa.select(Document.id).where(Document.id == env["docs"]["a"]).with_for_update(nowait=True))
                assert held.value.orig.sqlstate == "55P03"
            return np.asarray([[0.8] * 768 for _ in texts]), 3

    class Embedding(LLMBundle):
        def __init__(self, db: Session, *args: Any) -> None:
            self.db, self.langfuse, self.max_length = db, None, 8192
            self.model_config, self.mdl = {"llm_factory": "Builtin"}, Provider()

    monkeypatch.setitem(vars(sys.modules["api.apps.chunk"]), "LLMBundle", Embedding)
    monkeypatch.setitem(vars(sys.modules["api.apps.chunk"]), "get_tenant_default_model_by_type", lambda *args: {"llm_factory": "Builtin"})
    monkeypatch.setitem(vars(sys.modules["api.apps.chunk"]), "get_model_config_by_type_and_name", lambda *args: {"llm_factory": "Builtin"})
    monkeypatch.setitem(vars(sys.modules["api.apps.chunk"]), "get_model_config_by_id", lambda *args: {"llm_factory": "Builtin"})
    legitimate = write([a["id"]])
    assert legitimate.status_code == 200 and legitimate.json()[code_field] == 0
    after = snapshot(env)
    assert after["sql"] == before["sql"] and after["objects"] == before["objects"] and after["queue"] == before["queue"]
    assert after["index"]["foreign"] == before["index"]["foreign"]
    current = {item["id"]: item for item in after["index"]["kb"]["rows"]}
    original = {item["id"]: item for item in before["index"]["kb"]["rows"]}
    assert current[b["id"]] == original[b["id"]]
    assert current[a["id"]] != original[a["id"]]
    if writer == "legacy_set":
        assert current[a["id"]]["content_with_weight"] == "Legitimate document A replacement" and current[a["id"]]["vector"] and current[a["id"]]["q_768_vec"]
    else:
        assert current[a["id"]]["available_int"] == 1
    env["parser_record"]["writer_legitimate_target"] = {"writer": writer, "status": legitimate.status_code, "body": legitimate.json(), "stores": after}
    save(env["parser_record_path"], env["parser_record"])


def test_source_compare_restore_window_excludes_wrong_document_writer(parser_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    from common.doc_store import document_history as history

    env = parser_api
    headers = {"Authorization": "Bearer " + env["tokens"]["owner"]}
    response = requests.post(env["base"] + "/api/v1/documents/ingest", json={"doc_ids": [env["docs"]["b"]], "run": 1}, headers=headers, timeout=30)
    assert response.status_code == 200 and response.json()["code"] == 0
    with Session(env["engine"]) as db:
        task_id = db.scalar(sa.select(Task.id).where(Task.doc_id == env["docs"]["b"], Task.progress == 0))
    assert isinstance(task_id, str)
    row = copy.deepcopy(env["parser_record"]["source_rows"][3])
    row.update(content_with_weight="Attempt before compare/restore window", vector=[0.6] * 768, q_768_vec=[0.6] * 768)
    key = source_recovery_key(env["docs"]["b"], task_id)
    env["parser_record"]["manifest"]["redis_keys"].append(key)
    before = snapshot(env)
    env["parser_record"]["source_before_attempt"] = before
    save(env["parser_record_path"], env["parser_record"])
    compared, release, writer_attempted = threading.Event(), threading.Event(), threading.Event()
    source_thread: list[int] = []
    source_pid: list[int] = []
    writer_pid: list[int] = []
    old_insert, old_flush, old_restore = settings.docStoreConn.insert, Session.flush, history.restore_document_history
    inserted, failed = False, False

    def insert(*args: Any, **kwargs: Any) -> Any:
        nonlocal inserted
        result = old_insert(*args, **kwargs)
        if source_thread and threading.get_ident() == source_thread[0]:
            inserted = True
        return result

    def flush(db: Session, *args: Any, **kwargs: Any) -> None:
        nonlocal failed
        if source_thread and threading.get_ident() == source_thread[0] and inserted and not failed:
            failed = True
            raise RuntimeError("Controlled source flush failure before comparison")
        old_flush(db, *args, **kwargs)

    def restore(*args: Any, **kwargs: Any) -> None:
        if source_thread and threading.get_ident() == source_thread[0]:
            compared.set()
            assert release.wait(20)
        old_restore(*args, **kwargs)

    def document_lock(connection: Any, cursor: Any, statement: str, parameters: Any, context: Any, many: bool) -> None:
        if "FOR UPDATE" not in statement or env["docs"]["b"] not in parameters.values():
            return
        pid = connection.connection.driver_connection.info.backend_pid
        if source_thread and threading.get_ident() == source_thread[0]:
            source_pid[:] = [pid]
        elif compared.is_set():
            writer_pid[:] = [pid]
            writer_attempted.set()

    def source() -> str:
        source_thread.append(threading.get_ident())
        with pytest.raises(RuntimeError, match="Controlled source flush failure before comparison"):
            status_service.insert_source_chunks(env["engine"], [row], env["collections"]["kb"], env["ids"]["kb"], task_id)
        return "restored"

    def write(document: str) -> requests.Response:
        return requests.patch(env["base"] + f"/api/v1/datasets/{env['ids']['kb']}/documents/{document}/chunks", json={"chunk_ids": [row["id"]], "available_int": 0}, headers=headers, timeout=25)

    sa.event.listen(env["engine"], "before_cursor_execute", document_lock)
    try:
        with monkeypatch.context() as fault:
            fault.setattr(settings.docStoreConn, "insert", insert)
            fault.setattr(Session, "flush", flush)
            fault.setattr(history, "restore_document_history", restore)
            with ThreadPoolExecutor(max_workers=2) as pool:
                attempt = pool.submit(source)
                try:
                    assert compared.wait(20)
                    middle = snapshot(env)
                    wrong = write(env["docs"]["a"])
                    assert wrong.status_code == 200 and wrong.json()["code"] != 0
                    assert snapshot(env) == middle
                    winner = pool.submit(write, env["docs"]["b"])
                    assert writer_attempted.wait(10) and writer_pid and source_pid
                    blockers: list[int] = []
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        with env["engine"].connect() as probe:
                            blockers = probe.scalar(sa.text("SELECT pg_blocking_pids(:pid)"), {"pid": writer_pid[0]})
                        if source_pid[0] in blockers:
                            break
                        threading.Event().wait(0.05)
                    assert source_pid[0] in blockers and not winner.done()
                    env["parser_record"]["source_compare_restore_window"] = {
                        "middle": middle,
                        "wrong_status": wrong.status_code,
                        "wrong_body": wrong.json(),
                        "source_pid": source_pid[0],
                        "writer_pid": writer_pid[0],
                        "blocking_pids": blockers,
                    }
                    save(env["parser_record_path"], env["parser_record"])
                finally:
                    release.set()
                assert attempt.result(20) == "restored"
                response = winner.result(20)
                assert response.status_code == 200 and response.json()["code"] == 0
    finally:
        sa.event.remove(env["engine"], "before_cursor_execute", document_lock)
    after = snapshot(env)
    expected = copy.deepcopy(before["index"])
    next(item for item in expected["kb"]["rows"] if item["id"] == row["id"])["available_int"] = 0
    assert after["index"] == expected
    assert after["sql"] == before["sql"] and after["objects"] == before["objects"] and after["queue"] == before["queue"]
    assert after["redis_states"][key]["absent"] and after["redis_states"][key]["ttl"] == -2
    env["parser_record"]["source_compare_restore_window"]["winner"] = {"status": response.status_code, "body": response.json(), "stores": after}
    save(env["parser_record_path"], env["parser_record"])


@pytest.mark.parametrize("schema", ["absent", "exact", "pair_unique_index", "partial_pair_unique_index", "incompatible", "protected", "extra_unique", "extra_unique_index"])
def test_source_recovery_migration_requires_exact_durable_schema(parser_database: sa.Engine, alembic_cfg: Config, schema: str, request: pytest.FixtureRequest, tmp_path: Path) -> None:
    table = SourceRecoveryRecord.__table__
    cfg = Config(alembic_cfg.config_file_name)
    cfg.set_main_option("script_location", alembic_cfg.get_main_option("script_location"))
    evidence = Path(os.environ.get("MULTIRAG_C810_API_EVIDENCE_DIR", str(tmp_path)))
    evidence.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = evidence / f"{parser_database.url.database}.source-schema-{schema}.json"
    record: dict[str, Any] = {"test_nodeid": request.node.nodeid, "database": str(parser_database.url.database), "pid": os.getpid(), "schema": schema}
    save(path, record)
    try:
        if schema == "protected":
            nonce, document_id, task_id = uuid4().hex, uuid4().hex, uuid4().hex
            with parser_database.begin() as db:
                db.execute(table.insert().values(id=nonce, document_id=document_id, task_id=task_id, wire="protected source recovery material"))
            with parser_database.connect() as db:
                record["before"] = {
                    "rows": [dict(row) for row in db.execute(sa.select(table).order_by(table.c.id)).mappings()],
                    "version": db.scalar(sa.text("SELECT version_num FROM usr_ai.alembic_version")),
                }
            save(path, record)
            with pytest.raises(RuntimeError, match="source recovery material exists"), parser_database.begin() as db:
                cfg.attributes["connection"] = db
                command.downgrade(cfg, "e1f3a5c7b9d0")
            with parser_database.connect() as db:
                assert db.scalar(sa.text("SELECT version_num FROM usr_ai.alembic_version")) == record["before"]["version"]
                assert db.execute(sa.select(table.c.id, table.c.wire).where(table.c.document_id == document_id)).one() == (nonce, "protected source recovery material")
                record["after"] = {
                    "rows": [dict(row) for row in db.execute(sa.select(table).order_by(table.c.id)).mappings()],
                    "version": db.scalar(sa.text("SELECT version_num FROM usr_ai.alembic_version")),
                }
                assert record["after"] == record["before"]
            record["refused_downgrade_material_preserved"] = True
            save(path, record)
            return
        with parser_database.begin() as db:
            cfg.attributes["connection"] = db
            command.downgrade(cfg, "e1f3a5c7b9d0")
            assert not sa.inspect(db).has_table(table.name, schema=table.schema)
            if schema != "absent":
                table.create(db)
            if schema == "exact":
                db.execute(sa.text("ALTER TABLE usr_ai.t_document_source_recovery DROP CONSTRAINT uq_document_source_recovery_task"))
                db.execute(sa.text("ALTER TABLE usr_ai.t_document_source_recovery ADD CONSTRAINT equivalent_pair UNIQUE (task_id, document_id)"))
            if schema in {"pair_unique_index", "partial_pair_unique_index"}:
                db.execute(sa.text("ALTER TABLE usr_ai.t_document_source_recovery DROP CONSTRAINT uq_document_source_recovery_task"))
                predicate = " WHERE task_id LIKE 'a%'" if schema == "partial_pair_unique_index" else ""
                db.execute(sa.text("CREATE UNIQUE INDEX equivalent_standalone_pair ON usr_ai.t_document_source_recovery (task_id, document_id)" + predicate))
                document_id, task_id = uuid4().hex, "b" + uuid4().hex[1:]
                db.execute(table.insert().values(id=uuid4().hex, document_id=document_id, task_id=task_id, wire="retained first task material"))
                db.execute(table.insert().values(id=uuid4().hex, document_id=document_id, task_id=task_id if predicate else uuid4().hex, wire="retained second task material"))
                record["partial_pair_allowed_duplicate"] = bool(predicate)
            if schema == "extra_unique":
                db.execute(sa.text("ALTER TABLE usr_ai.t_document_source_recovery ADD CONSTRAINT incompatible_doc UNIQUE (document_id)"))
            if schema == "extra_unique_index":
                db.execute(sa.text("CREATE UNIQUE INDEX incompatible_doc ON usr_ai.t_document_source_recovery (document_id)"))
            if schema == "incompatible":
                db.execute(sa.text("ALTER TABLE usr_ai.t_document_source_recovery ALTER COLUMN wire TYPE varchar(20)"))
        if schema in {"incompatible", "extra_unique", "extra_unique_index", "partial_pair_unique_index"}:
            if schema in {"extra_unique", "extra_unique_index"}:
                nonce, document_id, task_id = uuid4().hex, uuid4().hex, uuid4().hex
                with parser_database.begin() as db:
                    db.execute(table.insert().values(id=nonce, document_id=document_id, task_id=task_id, wire="retained material under incompatible uniqueness"))
            with parser_database.connect() as db:
                inspector = sa.inspect(db)
                before = {
                    "columns": [(column["name"], str(column["type"]), column["nullable"]) for column in inspector.get_columns(table.name, schema=table.schema)],
                    "unique": inspector.get_unique_constraints(table.name, schema=table.schema),
                    "indexes": inspector.get_indexes(table.name, schema=table.schema),
                    "rows": [dict(row) for row in db.execute(sa.select(table).order_by(table.c.id)).mappings()],
                    "version": db.scalar(sa.text("SELECT version_num FROM usr_ai.alembic_version")),
                }
            message = "incompatible column types" if schema == "incompatible" else "incompatible uniqueness"
            record["before"] = before
            save(path, record)
            with pytest.raises(RuntimeError, match=message), parser_database.begin() as db:
                cfg.attributes["connection"] = db
                command.upgrade(cfg, "a9c810f1d2e3")
            with parser_database.connect() as db:
                inspector = sa.inspect(db)
                after = {
                    "columns": [(column["name"], str(column["type"]), column["nullable"]) for column in inspector.get_columns(table.name, schema=table.schema)],
                    "unique": inspector.get_unique_constraints(table.name, schema=table.schema),
                    "indexes": inspector.get_indexes(table.name, schema=table.schema),
                    "rows": [dict(row) for row in db.execute(sa.select(table).order_by(table.c.id)).mappings()],
                    "version": db.scalar(sa.text("SELECT version_num FROM usr_ai.alembic_version")),
                }
                assert after == before and after["version"] == "e1f3a5c7b9d0"
            record.update(after=after, rejected=True, table_data_version_preserved=True)
            save(path, record)
        else:
            if schema == "pair_unique_index":
                with parser_database.connect() as db:
                    inspector = sa.inspect(db)
                    record["before"] = {
                        "columns": [(column["name"], str(column["type"]), column["nullable"]) for column in inspector.get_columns(table.name, schema=table.schema)],
                        "unique": inspector.get_unique_constraints(table.name, schema=table.schema),
                        "indexes": inspector.get_indexes(table.name, schema=table.schema),
                        "rows": [dict(row) for row in db.execute(sa.select(table).order_by(table.c.id)).mappings()],
                        "version": db.scalar(sa.text("SELECT version_num FROM usr_ai.alembic_version")),
                    }
                    assert not record["before"]["unique"] and len(record["before"]["rows"]) == 2
                    first, second = record["before"]["rows"]
                    assert first["document_id"] == second["document_id"] and first["task_id"] != second["task_id"]
                save(path, record)
            with parser_database.begin() as db:
                cfg.attributes["connection"] = db
                command.upgrade(cfg, "a9c810f1d2e3")
                assert db.scalar(sa.text("SELECT version_num FROM usr_ai.alembic_version")) == "a9c810f1d2e3"
                if schema == "pair_unique_index":
                    inspector = sa.inspect(db)
                    record["after"] = {
                        "columns": [(column["name"], str(column["type"]), column["nullable"]) for column in inspector.get_columns(table.name, schema=table.schema)],
                        "unique": inspector.get_unique_constraints(table.name, schema=table.schema),
                        "indexes": inspector.get_indexes(table.name, schema=table.schema),
                        "rows": [dict(row) for row in db.execute(sa.select(table).order_by(table.c.id)).mappings()],
                        "version": db.scalar(sa.text("SELECT version_num FROM usr_ai.alembic_version")),
                    }
                    assert {key: value for key, value in record["after"].items() if key != "version"} == {key: value for key, value in record["before"].items() if key != "version"}
                    record["table_data_preserved"] = True
                document_id, task_id, nonce = uuid4().hex, uuid4().hex, uuid4().hex
                db.execute(table.insert().values(id=nonce, document_id=document_id, task_id=task_id, wire="exact durable material"))
                with pytest.raises(sa.exc.IntegrityError), db.begin_nested():
                    db.execute(table.insert().values(id=uuid4().hex, document_id=document_id, task_id=task_id, wire="other material"))
                assert db.execute(sa.select(table.c.id, table.c.wire).where(table.c.document_id == document_id)).one() == (nonce, "exact durable material")
                record.update(accepted=True, exact_nonce=nonce, duplicate_pair_rejected=True, version=db.scalar(sa.text("SELECT version_num FROM usr_ai.alembic_version")))
                save(path, record)
                db.execute(table.delete().where(table.c.id == nonce))
    finally:
        with parser_database.begin() as db:
            table.drop(db, checkfirst=True)
            table.create(db)
            cfg.attributes["connection"] = db
            # Restore later migration tables too, rather than stamping a
            # revision whose schema the fixture has not actually recreated.
            command.upgrade(cfg, "head")
        record["cleanup"] = {"fixture_table_restored": True, "fixture_rows_absent": True}
        save(path, record)


@pytest.mark.parametrize("failure", ["later_winner", "completion_then_restore"])
def test_source_completed_material_keeps_replay_authority(parser_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    env = parser_api
    headers = {"Authorization": "Bearer " + env["tokens"]["owner"]}
    response = requests.post(env["base"] + "/api/v1/documents/ingest", json={"doc_ids": [env["docs"]["b"]], "run": 1}, headers=headers, timeout=30)
    assert response.status_code == 200 and response.json()["code"] == 0
    with Session(env["engine"]) as db:
        task_id = db.scalar(sa.select(Task.id).where(Task.doc_id == env["docs"]["b"], Task.progress == 0))
    assert isinstance(task_id, str)
    row = copy.deepcopy(env["parser_record"]["source_rows"][3])
    row.update(content_with_weight="Confirmed source content", vector=[0.6] * 768, q_768_vec=[0.6] * 768)
    key = source_recovery_key(env["docs"]["b"], task_id)
    env["parser_record"]["manifest"]["redis_keys"].append(key)
    before = snapshot(env)
    env["parser_record"]["source_before_attempt"] = before
    save(env["parser_record_path"], env["parser_record"])
    old_update = SourceRecovery.update

    def incomplete(material: SourceRecovery, **changes: Any) -> None:
        if changes.get("phase") == "ready":
            raise RuntimeError("Controlled final source completion reply failure")
        old_update(material, **changes)

    with monkeypatch.context() as fault:
        if failure == "completion_then_restore":
            fault.setattr(SourceRecovery, "update", incomplete)
            fault.setattr(settings.docStoreConn, "delete", lambda *args, **kwargs: False)
            with pytest.raises(RuntimeError):
                status_service.insert_source_chunks(env["engine"], [row], env["collections"]["kb"], env["ids"]["kb"], task_id)
        else:
            assert status_service.insert_source_chunks(env["engine"], [row], env["collections"]["kb"], env["ids"]["kb"], task_id) == []
    applied = snapshot(env)
    material = load_source_recovery(env["docs"]["b"], task_id, env["engine"])
    assert material is not None and material.data["phase"] == ("ready" if failure == "later_winner" else "complete")
    assert material.data["nonce"] and material.data["original_native"] and material.data["applied_native"]
    sql_material = applied["sql"][SourceRecoveryRecord.__tablename__]
    assert len(sql_material) == 1 and sql_material[0]["wire"] == material.wire and sql_material[0]["id"] == material.data["nonce"]
    assert applied["redis_states"][key]["absent"] and applied["redis_states"][key]["ttl"] == -2
    path = f"/api/v1/datasets/{env['ids']['kb']}/documents/{env['docs']['b']}/chunks"
    response = requests.patch(env["base"] + path, json={"chunk_ids": [row["id"]], "available_int": 0}, headers=headers, timeout=20)
    assert response.status_code == 200 and response.json()["code"] == 0
    winner = snapshot(env)
    with Session(env["engine"]) as db:
        assert db.get(Task, task_id).progress == 0
    with pytest.raises(SourceRecoveryConflict):
        status_service.insert_source_chunks(env["engine"], [row], env["collections"]["kb"], env["ids"]["kb"], task_id)
    after = snapshot(env)
    assert business_sql(after) == business_sql(winner)
    for field in ["index", "objects", "queue", "image_reservations"]:
        assert after[field] == winner[field], field
    retained = load_source_recovery(env["docs"]["b"], task_id, env["engine"])
    assert retained is not None and retained.data["nonce"] == material.data["nonce"]
    assert retained.data["phase"] == ("unknown" if failure == "later_winner" else "complete")
    assert after["sql"][SourceRecoveryRecord.__tablename__][0]["wire"] == retained.wire
    with pytest.raises(SourceRecoveryConflict, match="before retry"):
        status_service.insert_source_chunks(env["engine"], [row], env["collections"]["kb"], env["ids"]["kb"], task_id)
    assert snapshot(env) == after
    env["parser_record"]["completed_source_authority"] = {
        "failure": failure,
        "applied": applied,
        "winner": winner,
        "retained": after,
        "journal": retained.data,
        "same_current_task_retry_blocked": True,
    }
    env["parser_record"]["events"].append({"path": path, "status": response.status_code, "body": response.json(), "stores": winner})
    save(env["parser_record_path"], env["parser_record"])
    patch(env, {"chunk_method": "paper"}, key="b")
    assert not snapshot(env)["sql"][SourceRecoveryRecord.__tablename__] and not REDIS_CONN.REDIS.exists(key)


def test_source_materials_for_same_document_are_independent(parser_api: dict[str, Any]) -> None:
    from api.db.services.document_image_lock import retire_task_image_reservations
    from common.doc_store.document_history import document_history

    env = parser_api
    document_id, first_id, second_id = env["docs"]["b"], uuid4().hex, uuid4().hex
    with Session(env["engine"]) as db:
        doc = db.get(Document, document_id)
        doc.run, doc.progress = "1", 0
        db.add_all([Task(id=task_id, doc_id=document_id, progress=0, chunk_ids="") for task_id in [first_id, second_id]])
        db.commit()
        original_doc = status_service._source_row(db.get(Document, document_id))
        original_tasks = {task_id: status_service._source_row(db.get(Task, task_id)) for task_id in [first_id, second_id]}
    keys = [source_recovery_key(document_id, task_id) for task_id in [first_id, second_id]]
    env["parser_record"]["manifest"]["redis_keys"].extend(keys)
    before = snapshot(env)
    env["parser_record"]["source_before_attempt"] = before
    save(env["parser_record_path"], env["parser_record"])
    original_native = document_history(settings.docStoreConn, env["collections"]["kb"], env["ids"]["kb"], document_id)
    row = copy.deepcopy(original_native[0])
    row.update(content_with_weight="Incoming complete source payload", available_int=1)
    payload = {
        "dataset_id": env["ids"]["kb"],
        "index": env["collections"]["kb"],
        "original_doc": original_doc,
        "original_native": original_native,
        "chunks": [row],
        "applied_native": None,
        "applied_task": None,
        "applied_doc": None,
    }
    materials = [prepare_source_recovery(env["engine"], document_id, task_id, original_task=original_tasks[task_id], **copy.deepcopy(payload)) for task_id in [first_id, second_id]]
    first, second = materials
    assert first.data["nonce"] != second.data["nonce"]
    both = snapshot(env)
    assert business_sql(both) == business_sql(before)
    for field in ["index", "objects", "queue", "image_reservations"]:
        assert both[field] == before[field], field
    for material in materials:
        read = load_source_recovery(document_id, material.data["task_id"], env["engine"])
        assert read is not None and read.data == material.data and read.wire == material.wire
        assert both["redis"][material.key]["value"] == material.wire and both["redis"][material.key]["ttl"] == -1 and both["redis_states"][material.key]["dump_hex"]
        assert set(material.data["original_doc"]) == set(Document.__table__.columns.keys()) and set(material.data["original_task"]) == set(Task.__table__.columns.keys())
        assert material.data["original_native"] and all(item["vector"] and item["q_768_vec"] for item in material.data["original_native"])
    with env["engine"].begin() as db:
        assert db.scalar(sa.select(sa.func.count()).select_from(SourceRecoveryRecord)) == 2
        with pytest.raises(sa.exc.IntegrityError), db.begin_nested():
            db.execute(SourceRecoveryRecord.__table__.insert().values(id=uuid4().hex, document_id=document_id, task_id=first_id, wire="duplicate pair"))
    assert snapshot(env) == both
    env["parser_record"]["same_document_materials"] = {"both": both, "journals": [material.data for material in materials], "duplicate_pair_rejected_without_effects": True}
    save(env["parser_record_path"], env["parser_record"])
    with Session(env["engine"]) as db:
        db.execute(sa.delete(Task).where(Task.id == first_id))
        db.commit()
        retire_task_image_reservations(db, [first_id], document_id=document_id)
    assert load_source_recovery(document_id, first_id, env["engine"]) is None
    retained = load_source_recovery(document_id, second_id, env["engine"])
    after_first = snapshot(env)
    assert retained is not None and retained.wire == second.wire and retained.data == second.data
    assert after_first["redis"][second.key] == both["redis"][second.key] and after_first["redis_states"][second.key] == both["redis_states"][second.key]
    assert after_first["redis_states"][first.key]["absent"] and after_first["redis_states"][first.key]["ttl"] == -2
    assert len(after_first["sql"][SourceRecoveryRecord.__tablename__]) == 1 and after_first["sql"][SourceRecoveryRecord.__tablename__][0]["wire"] == second.wire
    env["parser_record"]["same_document_materials"]["first_retired"] = after_first
    save(env["parser_record_path"], env["parser_record"])
    with Session(env["engine"]) as db:
        db.execute(sa.delete(Task).where(Task.id == second_id))
        db.commit()
        retire_task_image_reservations(db, [second_id], document_id=document_id)
    final = snapshot(env)
    assert not final["sql"][SourceRecoveryRecord.__tablename__] and all(final["redis_states"][key]["absent"] and final["redis_states"][key]["ttl"] == -2 for key in keys)
    for field in ["index", "objects", "queue", "image_reservations"]:
        assert final[field] == before[field], field
    env["parser_record"]["same_document_materials"]["both_retired"] = final
    save(env["parser_record_path"], env["parser_record"])
