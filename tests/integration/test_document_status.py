"""Actual status HTTP, PostgreSQL, Milvus and source write acceptance.

Only model/provider output and specifically named failure boundaries are
controlled. Authentication, routes, transactions, index writes and reads are real."""

import asyncio
import copy
import json
import os
import subprocess
import threading
from typing import Any
from uuid import uuid4

import pytest
import requests
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

from api.db.db_models import Document, Knowledgebase, UserTenant
from api.db.services import document_status_service as status_service
from common import settings
from common.doc_store.doc_store_base import OrderByExpr
from tests.support.document_parse_retirement import _ModelOutput, assert_retired_upload_has_no_work
from tests.support.document_parse_retirement import parse_api as parse_api
from tests.support.document_status import assert_retired_status_has_no_work, change, chunk, index_rows, sql_rows
from tests.support.document_status import status_api as status_api
from tests.support.runtime_upload import read_object
from tests.support.runtime_upload import runtime_upload_api as runtime_upload_api


def test_http_disable_enable_retry_partial_preserves_every_source_field(status_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = status_api
    before_sql, before_index = sql_rows(env), index_rows(env)
    assert change(env, [env["doc"], env["doc"]], 0).json() == {"code": 0, "message": "success", "data": {env["doc"]: {"status": "0"}}}
    assert sql_rows(env)[env["doc"]]["status"] == "0"
    rows = index_rows(env)
    for row, original in zip(rows, before_index, strict=True):
        if row["doc_id"] == env["doc"]:
            assert row["available_int"] == 0
            row["available_int"] = 1
        assert row == original
    current_sql = sql_rows(env)
    current_sql[env["doc"]]["status"] = "1"
    assert current_sql == before_sql
    assert read_object(env["storage"], env["bucket"], f"{env['kb']}/doc.txt") == b"Owned original source bytes."
    assert change(env, [env["doc"]], "1", key=env["jwt"]).json()["code"] == 0
    assert index_rows(env) == before_index
    # Create the old SQL/index mismatch deliberately and prove same-state repair.
    assert settings.docStoreConn.update({"doc_id": env["doc"]}, {"available_int": 0}, env["collection"], env["kb"])
    assert change(env, [env["doc"]], 1).json()["code"] == 0
    assert index_rows(env) == before_index
    missing = uuid4().hex
    response = change(env, [env["doc"], missing, env["foreign_doc"], env["second"]], "0").json()
    assert response["code"] == 500
    assert response["data"][env["doc"]] == response["data"][env["second"]] == {"status": "0"}
    assert response["data"][missing] == response["data"][env["foreign_doc"]] == {"error": "Document not found in this dataset."}
    assert sql_rows(env)[env["foreign_doc"]] == before_sql[env["foreign_doc"]]
    result = settings.docStoreConn.search(["id", "available_int"], [], {"doc_id": env["doc"], "available_int": 0}, [], OrderByExpr(), 0, 10, [env["collection"]], [env["kb"]])
    assert settings.docStoreConn.get_total(result) == 2
    result = settings.docStoreConn.search(["id"], [], {"doc_id": env["doc"], "available_int": 1}, [], OrderByExpr(), 0, 10, [env["collection"]], [env["kb"]])
    assert settings.docStoreConn.get_total(result) == 0
    assert_retired_status_has_no_work(env, monkeypatch)
    enabled = change(env, [env["doc"]], 1, key=env["jwt"])
    assert enabled.status_code == 200 and enabled.json() == {"code": 0, "message": "success", "data": {env["doc"]: {"status": "1"}}}
    assert sql_rows(env)[env["doc"]]["status"] == "1"
    smoke = subprocess.run(["make", "smoke"], env={**os.environ, "SMOKE_BASE_URL": env["base"]}, text=True, capture_output=True, timeout=60)
    env["record_path"].with_suffix(".smoke.log").write_text(smoke.stdout + smoke.stderr + f"\nexit={smoke.returncode}\n")
    assert smoke.returncode == 0, smoke.stdout + smoke.stderr
    print("Real HTTP/SQL/Milvus full payload+vectors+timestamps, sibling and object bytes preserved; same-state retry, partial map and available0/1 filters; same-listener smoke passed")


def test_patch_enabled_uses_status_service_and_repairs_same_state(status_api: dict[str, Any]) -> None:
    env = status_api
    before_sql, before_index = sql_rows(env), index_rows(env)
    url = f"{env['base']}/api/v1/datasets/{env['kb']}/documents/{env['doc']}"
    headers = {"Authorization": f"Bearer {env['owner_key']}"}
    for status in [0, 1]:
        response = requests.patch(url, headers=headers, json={"enabled": status}, timeout=30)
        assert response.status_code == 200 and response.json()["code"] == 0
        assert sql_rows(env)[env["doc"]]["status"] == str(status)
        assert all(row["available_int"] == status for row in index_rows(env, env["doc"]))
    assert sql_rows(env) == before_sql and index_rows(env) == before_index
    assert settings.docStoreConn.update({"doc_id": env["doc"]}, {"available_int": 0}, env["collection"], env["kb"])
    assert requests.patch(url, headers=headers, json={"enabled": 1}, timeout=30).json()["code"] == 0
    assert sql_rows(env) == before_sql and index_rows(env) == before_index


@pytest.mark.parametrize("role,active,allowed", [("admin", "1", True), ("owner", "1", True), ("normal", "1", False), ("invite", "1", False), ("admin", "0", False), (None, "1", False)])
def test_http_actual_membership(status_api: dict[str, Any], role: str | None, active: str, allowed: bool) -> None:
    env = status_api
    before = sql_rows(env)
    if role:
        with Session(env["engine"]) as db:
            db.add(UserTenant(id=uuid4().hex, tenant_id=env["owners"][0], user_id=env["owners"][1], role=role, status=active, invited_by=env["owners"][0]))
            db.commit()
    response = change(env, [env["doc"]], 0, key=env["api_key"])
    assert response.status_code == 200
    assert response.json()["code"] == (0 if allowed else 109)
    assert sql_rows(env)[env["doc"]]["status"] == ("0" if allowed else "1")
    if not allowed:
        assert sql_rows(env) == before


def test_http_body_auth_and_dataset_boundaries(status_api: dict[str, Any]) -> None:
    env = status_api
    before = sql_rows(env), index_rows(env)
    url = f"{env['base']}/api/v1/datasets/{env['kb']}/documents/batch-update-status"
    for body in [
        {},
        {"doc_ids": [], "status": 0},
        {"doc_ids": "a", "status": 0},
        {"doc_ids": [1], "status": 0},
        {"doc_ids": [" "], "status": 0},
        {"doc_ids": [env["doc"]], "status": True},
        {"doc_ids": [env["doc"]], "status": 0.0},
        {"doc_ids": [env["doc"]], "status": 1.0},
        {"doc_ids": [env["doc"]], "status": 0.5},
        {"doc_ids": [env["doc"]], "status": "2"},
        {"doc_ids": [env["doc"]], "status": 0, "tenant_id": env["owners"][0]},
    ]:
        assert requests.post(url, headers={"Authorization": f"Bearer {env['owner_key']}"}, json=body, timeout=30).status_code == 422
    for token in ["invalid-api-key", "a.b.c", ""]:
        response = requests.post(url, headers={"Authorization": f"Bearer {token}"}, json={"doc_ids": [env["doc"]], "status": 0}, timeout=30)
        assert response.status_code == 401
    assert change(env, [env["doc"]], 0, dataset=uuid4().hex).json()["code"] == 109
    with Session(env["engine"]) as db:
        db.execute(sa.update(Knowledgebase).where(Knowledgebase.id == env["kb"]).values(status="0"))
        db.commit()
    assert change(env, [env["doc"]], 0).json()["code"] == 109
    with Session(env["engine"]) as db:
        db.execute(sa.update(Knowledgebase).where(Knowledgebase.id == env["kb"]).values(status="1"))
        db.commit()
    assert (sql_rows(env), index_rows(env)) == before


@pytest.mark.parametrize("fault", ["false", "exception", "partial", "sql", "recovery"])
def test_real_store_sql_failure_and_compensation(status_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, fault: str) -> None:
    env = status_api
    before = sql_rows(env), index_rows(env)
    store = settings.docStoreConn
    actual = store.update
    attempts = 0

    def update(condition: dict[str, Any], values: dict[str, Any], *args: Any) -> bool:
        nonlocal attempts
        attempts += 1
        if condition["doc_id"] != env["doc"]:
            return actual(condition, values, *args)
        if fault == "recovery":
            actual(condition, values, *args)
            return False
        if attempts == 1 and fault in {"false", "exception", "partial"}:
            if fault == "partial":
                one = index_rows(env, env["doc"])[0]["pk"]
                assert actual({"pk": one}, values, *args)
            if fault == "exception":
                raise RuntimeError("3022 secret provider endpoint")
            return False
        return actual(condition, values, *args)

    def fail_sql(conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool) -> None:
        if statement.lstrip().startswith("UPDATE usr_ai.t_ai_documents"):
            raise RuntimeError("controlled SQL failure")

    monkeypatch.setattr(store, "update", update)
    if fault == "sql":
        sa.event.listen(env["engine"], "before_cursor_execute", fail_sql)
        # Async HTTP has its own engine. Inject at the SQL statement boundary.
        from sqlalchemy.ext.asyncio import AsyncSession

        execute = AsyncSession.execute

        async def failing_execute(self: AsyncSession, statement: Any, *args: Any, **kwargs: Any) -> Any:
            if isinstance(statement, sa.sql.dml.Update) and statement.table.name == Document.__tablename__:
                raise RuntimeError("controlled SQL failure")
            return await execute(self, statement, *args, **kwargs)

        monkeypatch.setattr(AsyncSession, "execute", failing_execute)
    try:
        response = change(env, [env["doc"], env["second"]], 0).json()
        assert response["code"] == 500 and "error" in response["data"][env["doc"]]
        assert "secret" not in json.dumps(response) and "3022" not in json.dumps(response)
        assert sql_rows(env)[env["doc"]] == before[0][env["doc"]]
        assert index_rows(env, env["doc"]) == [row for row in before[1] if row["doc_id"] == env["doc"]]
        if fault != "sql":
            assert response["data"][env["second"]] == {"status": "0"}
        if fault == "recovery":
            assert response["data"][env["doc"]]["error"] == status_service.COMPENSATION_ERROR
    finally:
        if fault == "sql":
            sa.event.remove(env["engine"], "before_cursor_execute", fail_sql)


@pytest.mark.parametrize("asynchronous", [True, False])
def test_recovery_failure_releases_per_document_transaction(status_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, asynchronous: bool) -> None:
    env = status_api
    before = sql_rows(env), index_rows(env)
    actual = settings.docStoreConn.update

    def unconfirmed(*args: Any, **kwargs: Any) -> bool:
        assert actual(*args, **kwargs)
        return False

    def independently_lock() -> None:
        with env["engine"].begin() as connection:
            assert connection.scalar(sa.select(Document.status).where(Document.id == env["doc"]).with_for_update(nowait=True)) == "1"

    monkeypatch.setattr(settings.docStoreConn, "update", unconfirmed)

    async def scenario() -> None:
        engine = create_async_engine(env["engine"].url)
        try:
            async with async_sessionmaker(engine)() as db:
                assert await status_service.change_document_status(db, env["doc"], "0", env["owners"][0], env["kb"]) == status_service.COMPENSATION_ERROR
                assert not db.in_transaction()
                independently_lock()
        finally:
            await engine.dispose()

    if asynchronous:
        asyncio.run(scenario())
    else:
        with Session(env["engine"]) as db:
            doc, kb = db.get(Document, env["doc"]), db.get(Knowledgebase, env["kb"])
            assert status_service.change_document_status_sync(db, doc, kb, "0") == status_service.COMPENSATION_ERROR
            assert not db.in_transaction()
            independently_lock()
    assert (sql_rows(env), index_rows(env)) == before


def test_zero_disabled_source_rest_legacy_worker_and_mixed_status(status_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    chunk_api = vars(sys.modules["api.apps.restful_apis.chunk"])
    chunk_app = vars(sys.modules["api.apps.chunk"])
    from core.svr import task_executor

    env = status_api
    assert change(env, [env["zero"]], 0).json()["code"] == 0
    model = _ModelOutput("status scratch", [])
    monkeypatch.setitem(chunk_api, "_embedding_model", lambda *args: model)
    monkeypatch.setitem(chunk_app, "LLMBundle", lambda *args: model)
    for name in ["get_model_config_by_id", "get_model_config_by_type_and_name", "get_tenant_default_model_by_type"]:
        monkeypatch.setitem(chunk_app, name, lambda *args: {})
    headers = {"Authorization": f"Bearer {env['owner_key']}"}
    response = requests.post(f"{env['base']}/api/v1/datasets/{env['kb']}/documents/{env['zero']}/chunks", headers=headers, json={"content": "REST source while disabled"}, timeout=30)
    assert response.status_code == 200 and response.json()["code"] == 0, response.text
    response = requests.post(
        env["base"] + "/v1/chunk/create", headers=headers, json={"doc_id": env["zero"], "content_with_weight": "Legacy source while disabled", "important_kwd": [], "question_kwd": []}, timeout=30
    )
    assert response.json()["retcode"] == 0, response.text
    rows = [chunk(env, env["zero"], "worker disabled"), chunk(env, env["second"], "worker enabled")]
    rows[0]["mom"] = "Hidden mother source"
    with Session(env["engine"]) as db:
        for task_id, row in zip([env["task"], env["second_task"]], rows, strict=True):
            assert asyncio.run(
                task_executor.insert_chunks(
                    db, task_id, env["owners"][0], env["kb"], [row], lambda *args, **kwargs: None, env["collection"], settings.docStoreConn._get_connection().describe_collection(env["collection"])
                )
            )
    assert all(row["available_int"] == 0 for row in index_rows(env, env["zero"]))
    assert all(row["available_int"] == 1 for row in index_rows(env, env["second"]))
    assert len(index_rows(env, env["zero"])) == 4
    assert change(env, [env["zero"]], 1).json()["code"] == 0
    assert sql_rows(env)[env["zero"]]["status"] == "1"
    enabled_rows = index_rows(env, env["zero"])
    assert sum(row["available_int"] == 0 for row in enabled_rows) == 1
    assert next(row for row in enabled_rows if row["available_int"] == 0)["content_with_weight"] == "Hidden mother source"
    print("Real disabled zero-chunk insertion through REST/legacy/worker, mixed-document grouping and hidden mother source; enable readback passed")


def test_source_read_failure_has_no_store_write(status_api: dict[str, Any]) -> None:
    env = status_api
    before = index_rows(env)
    with pytest.raises(ValueError, match="status unavailable"):
        status_service.insert_source_chunks(env["engine"], [chunk(env, uuid4().hex, "missing source")], env["collection"], env["kb"])
    assert index_rows(env) == before


def test_cancelled_status_drains_store_before_next_sql_winner(status_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = status_api
    entered, release = threading.Event(), threading.Event()
    actual = settings.docStoreConn.update
    first = True

    def blocked(*args: Any, **kwargs: Any) -> bool:
        nonlocal first
        if first:
            first = False
            entered.set()
            assert release.wait(10)
        return actual(*args, **kwargs)

    monkeypatch.setattr(settings.docStoreConn, "update", blocked)

    async def scenario() -> None:
        engine = create_async_engine(env["engine"].url)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with sessions() as first_db, sessions() as next_db:
                write = asyncio.create_task(status_service.change_document_status(first_db, env["doc"], "0", env["owners"][0], env["kb"]))
                assert await asyncio.to_thread(entered.wait, 10)
                write.cancel()
                later = asyncio.create_task(status_service.change_document_status(next_db, env["doc"], "1", env["owners"][0], env["kb"]))
                await asyncio.sleep(0.1)
                assert not write.done() and not later.done()
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await write
                assert await later is None
        finally:
            release.set()
            await engine.dispose()

    asyncio.run(scenario())
    assert sql_rows(env)[env["doc"]]["status"] == "1" and all(row["available_int"] == 1 for row in index_rows(env, env["doc"]))


def test_retired_upload_parse_has_no_producer_for_disabled_zero_document(status_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = status_api
    assert change(env, [env["zero"]], 0).json()["code"] == 0
    assert sql_rows(env)[env["zero"]]["chunk_num"] == 0 and sql_rows(env)[env["zero"]]["status"] == "0"
    assert_retired_upload_has_no_work(env, monkeypatch)
    assert index_rows(env, env["zero"]) == []


def test_source_special_products_and_read_then_delete(status_api: dict[str, Any]) -> None:
    env = status_api
    special = [chunk(env, "graph_raptor_x", "graph product"), chunk(env, uuid4().hex, "raptor product")]
    special[0]["knowledge_graph_kwd"] = "mind_map"
    special[1]["raptor_kwd"] = "summary"
    special[0]["available_int"] = special[1]["available_int"] = 0
    with status_service.source_document_availability(env["engine"], special, env["kb"]):
        assert [row["available_int"] for row in special] == [0, 0]
    with Session(env["engine"]) as db:
        db.execute(sa.delete(Document).where(Document.id == env["zero"]))
        db.commit()
    response = change(env, [env["zero"], env["second"]], 0).json()
    assert response["code"] == 500 and response["data"][env["zero"]] == {"error": "Document not found in this dataset."}
    assert response["data"][env["second"]] == {"status": "0"}


def test_source_insert_before_counter_status_race(status_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    import time
    from concurrent.futures import ThreadPoolExecutor

    env = status_api
    entered, release = threading.Event(), threading.Event()
    actual = settings.docStoreConn.insert

    def blocked(*args: Any, **kwargs: Any) -> list[str]:
        entered.set()
        assert release.wait(10)
        return actual(*args, **kwargs)

    monkeypatch.setattr(settings.docStoreConn, "insert", blocked)
    with ThreadPoolExecutor(max_workers=2) as executor:
        insert = executor.submit(status_service.insert_source_chunks, env["engine"], [chunk(env, env["zero"], "Race source before counter")], env["collection"], env["kb"])
        assert entered.wait(10)
        disable = executor.submit(change, env, [env["zero"]], 0)
        time.sleep(0.1)
        assert not disable.done()
        release.set()
        assert insert.result(timeout=10) == []
        assert disable.result(timeout=10).json()["code"] == 0
    assert sql_rows(env)[env["zero"]]["chunk_num"] == 0
    assert sql_rows(env)[env["zero"]]["status"] == "0"
    assert index_rows(env, env["zero"])[0]["available_int"] == 0
    assert change(env, [env["zero"]], 1).json()["code"] == 0
    assert index_rows(env, env["zero"])[0]["available_int"] == 1


def test_actual_missing_collection_is_nonzero_and_retry_reconciles(status_api: dict[str, Any]) -> None:
    env = status_api
    original_sql, original_index = sql_rows(env), index_rows(env)
    settings.docStoreConn.delete_idx(env["collection"], env["kb"])
    response = change(env, [env["doc"], env["zero"]], 0).json()
    assert response["code"] == 500 and "error" in response["data"][env["doc"]]
    assert response["data"][env["zero"]] == {"status": "0"}
    assert sql_rows(env)[env["doc"]] == original_sql[env["doc"]]
    assert settings.docStoreConn.insert(original_index, env["collection"], env["kb"]) == []
    assert change(env, [env["doc"]], 0).json()["code"] == 0
    assert all(row["available_int"] == 0 for row in index_rows(env, env["doc"]))


@pytest.mark.parametrize("winner", ["status", "delete"])
def test_python_commit_failure_uses_current_winner_or_reports_deleted_recovery(status_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, winner: str) -> None:
    from concurrent.futures import ThreadPoolExecutor

    from sqlalchemy.ext.asyncio import AsyncSession

    env = status_api
    before_sql, before_index = sql_rows(env), index_rows(env)
    name = "status_commit_" + uuid4().hex
    with env["engine"].begin() as connection:
        connection.execute(
            sa.text(
                f"CREATE FUNCTION usr_ai.{name}() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.id = '{env['doc']}' THEN RAISE EXCEPTION 'controlled commit failure'; END IF; RETURN NEW; END $$"
            )
        )
        connection.execute(sa.text(f"CREATE CONSTRAINT TRIGGER {name} AFTER UPDATE ON usr_ai.t_ai_documents DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION usr_ai.{name}()"))
    rolled_back, release = threading.Event(), threading.Event()
    first_session: AsyncSession | None = None
    paused = False
    commit, rollback = AsyncSession.commit, AsyncSession.rollback

    async def marked_commit(self: AsyncSession) -> None:
        nonlocal first_session
        if first_session is None:
            first_session = self
        await commit(self)

    async def recovery_gate(self: AsyncSession) -> None:
        nonlocal paused
        await rollback(self)
        if self is first_session and not paused:
            paused = True
            rolled_back.set()
            assert await asyncio.to_thread(release.wait, 15)

    monkeypatch.setattr(AsyncSession, "commit", marked_commit)
    monkeypatch.setattr(AsyncSession, "rollback", recovery_gate)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            failed = executor.submit(change, env, [env["doc"], env["second"]], 0)
            try:
                assert rolled_back.wait(10)
                # The real first transaction has failed COMMIT and rolled back;
                # remove only this owned guard so a new winner can really commit.
                with env["engine"].begin() as connection:
                    connection.execute(sa.text(f"DROP TRIGGER {name} ON usr_ai.t_ai_documents"))
                    if winner == "delete":
                        connection.execute(sa.delete(Document).where(Document.id == env["doc"]))
                if winner == "status":
                    assert change(env, [env["doc"]], 0).json()["code"] == 0
                    assert sql_rows(env)[env["doc"]]["status"] == "0"
                else:
                    assert env["doc"] not in sql_rows(env)
            finally:
                release.set()
            result = failed.result(timeout=15).json()
        assert result["code"] == 500
        assert result["data"][env["doc"]] == {"error": status_service.STATUS_ERROR if winner == "status" else status_service.COMPENSATION_ERROR}
        assert result["data"][env["second"]] == {"status": "0"}
        final_sql = sql_rows(env)
        expected_sql = copy.deepcopy(before_sql)
        if winner == "delete":
            expected_sql.pop(env["doc"])
        else:
            expected_sql[env["doc"]]["status"] = "0"
        expected_sql[env["second"]]["status"] = "0"
        assert final_sql == expected_sql
        expected_index = copy.deepcopy(before_index)
        for row in expected_index:
            if row["doc_id"] in {env["doc"], env["second"]}:
                row["available_int"] = 0
        assert index_rows(env) == expected_index
    finally:
        release.set()
        with env["engine"].begin() as connection:
            connection.execute(sa.text(f"DROP TRIGGER IF EXISTS {name} ON usr_ai.t_ai_documents"))
            connection.execute(sa.text(f"DROP FUNCTION usr_ai.{name}()"))
