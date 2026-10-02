"""Opt-in real Go listener, shared scratch PostgreSQL and actual index engines."""

import json
import os
import shutil
import subprocess
import time
from collections.abc import Iterator
from typing import Any
from uuid import uuid4

import infinity
import pytest
import requests
import sqlalchemy as sa
from infinity.common import ConflictType, NetworkAddress
from sqlalchemy.orm import Session

from api.db.db_models import Document, Tenant, User, UserTenant
from common import settings
from common.config_utils import CONFIGS
from tests.integration.document_status_rpc_proxy import StatusRPCProxy
from tests.integration.test_document_status import chunk as chunk
from tests.integration.test_document_status import index_rows as index_rows
from tests.integration.test_document_status import parse_api as parse_api
from tests.integration.test_document_status import runtime_upload_api as runtime_upload_api
from tests.integration.test_document_status import sql_rows as sql_rows
from tests.integration.test_document_status import status_api as status_api


@pytest.fixture(params=["infinity", "milvus", "elasticsearch"])
def go_status_api(status_api: dict[str, Any], tmp_path: Any, request: pytest.FixtureRequest) -> Iterator[dict[str, Any]]:
    env = status_api
    kind = request.param
    go = os.environ.get("MULTIRAG_TEST_GO") or shutil.which("go")
    assert go
    port = int(subprocess.check_output(["docker", "port", "multirag-a536-infinity", "23817"], text=True).strip().rsplit(":", 1)[1])
    conn = infinity.connect(NetworkAddress("127.0.0.1", port))
    db_name = "status_" + uuid4().hex
    database = conn.create_database(db_name, ConflictType.Error)
    table_name = env["collection"] + "_" + env["kb"]
    table = database.create_table(
        table_name,
        {
            "id": {"type": "varchar"},
            "doc_id": {"type": "varchar"},
            "mom_id": {"type": "varchar", "default": ""},
            "available_int": {"type": "integer"},
            "content": {"type": "varchar"},
            "created": {"type": "varchar"},
            "vector": {"type": "vector,4,float"},
        },
        ConflictType.Error,
    )
    table.insert(
        [
            {"id": uuid4().hex, "doc_id": doc, "available_int": 1, "content": "unchanged " + str(i), "created": "original source", "vector": [0.1, 0.2, 0.3, 0.4]}
            for i, doc in enumerate([env["doc"], env["doc"], env["second"]])
        ]
    )
    url = env["engine"].url
    milvus = CONFIGS["milvus"]
    proxy = StatusRPCProxy(port)
    cfg = {
        "SecretKey": settings.SECRET_KEY,
        "Database": {"Driver": "postgres", "Host": url.host, "Port": url.port, "Database": url.database, "Username": url.username, "Password": url.password, "Schema": "usr_ai"},
        "InfinityURI": f"127.0.0.1:{proxy.port}",
        "InfinityDB": db_name,
        "EngineType": kind,
        "Milvus": {"Hosts": milvus["hosts"], "Username": milvus.get("username", ""), "Password": milvus.get("password", ""), "DBName": milvus.get("db_name", "")},
    }
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(cfg))
    config_path.chmod(0o600)
    output_path = tmp_path / "go-live.log"
    with output_path.open("w") as output:
        process = subprocess.Popen(
            [go, "test", "./internal/handler", "-run", "^TestDocumentStatusLiveServer$", "-count=1", "-v"],
            env={**os.environ, "MULTIRAG_STATUS_LIVE_CONFIG": str(config_path)},
            stdout=output,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 120
            while not (tmp_path / "base").exists() and time.monotonic() < deadline:
                assert process.poll() is None, output_path.read_text()
                time.sleep(0.1)
            assert (tmp_path / "base").exists(), output_path.read_text()
            env.update(go_base=(tmp_path / "base").read_text(), go_engine=kind, infinity_table=table, rpc_proxy=proxy, infinity_port=port, infinity_db=db_name, infinity_table_name=table_name)
            yield env
        finally:
            (tmp_path / "stop").touch()
            try:
                result = process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=10)
                raise
            proxy.close()
            config_path.unlink()
            conn.drop_database(db_name, ConflictType.Error)
            assert db_name not in conn.list_databases().db_names
            conn.disconnect()
            assert result == 0, output_path.read_text()
            print(output_path.read_text())
            print("Owned Infinity scratch database and Go listener/config clients cleaned; same scratch SQL resolves usr_ai in 3 pooled connections")


def go_change(env: dict[str, Any], ids: list[str], status: Any, key: str | None = None, dataset: str | None = None) -> requests.Response:
    return requests.post(
        f"{env['go_base']}/api/v1/datasets/{dataset or env['kb']}/documents/batch-update-status",
        headers={"Authorization": f"Bearer {key or env['owner_key']}"},
        json={"doc_ids": ids, "status": status},
        timeout=30,
    )


def inf_rows(env: dict[str, Any], condition: str | None = None) -> dict[str, Any]:
    query = env["infinity_table"].output(["*"])
    if condition:
        query = query.filter(condition)
    data = query.to_result()[0]
    normalized = json.loads(json.dumps(data, default=lambda value: value.tolist() if hasattr(value, "tolist") else str(value)))
    order = sorted(range(len(normalized["id"])), key=lambda index: normalized["id"][index])
    return {field: [values[index] for index in order] for field, values in normalized.items()}


def test_go_actual_supported_and_unavailable_index_contract(go_status_api: dict[str, Any]) -> None:
    env = go_status_api
    before_sql = sql_rows(env)
    before_index, before_milvus = inf_rows(env), index_rows(env)
    response = go_change(env, [env["doc"], env["doc"], uuid4().hex, env["foreign_doc"], env["second"]], 0)
    assert response.status_code == 200 and response.json()["code"] == 500
    result = response.json()["data"]
    if env["go_engine"] == "infinity":
        assert result[env["doc"]] == result[env["second"]] == {"status": "0"}
        assert sql_rows(env)[env["doc"]]["status"] == "0"
        disabled = inf_rows(env)
        assert disabled["available_int"] == [0, 0, 0]
        disabled["available_int"] = before_index["available_int"]
        assert disabled == before_index
        assert go_change(env, [env["doc"]], "1").json()["code"] == 0
        assert sql_rows(env)[env["doc"]]["status"] == "1"
        # Force old index mismatch while SQL already enabled, then retry same status.
        env["infinity_table"].update(f"doc_id = '{env['doc']}'", {"available_int": 0})
        assert go_change(env, [env["doc"]], 1).json()["code"] == 0
        assert all(value == 1 for value in inf_rows(env, f"doc_id = '{env['doc']}'")["available_int"])
        assert go_change(env, [env["zero"]], 0).json()["code"] == 0
        assert sql_rows(env)[env["zero"]]["status"] == "0"
        assert go_change(env, [env["zero"]], 1).json()["code"] == 0
    else:
        assert "unavailable" in result[env["doc"]]["error"]
        assert sql_rows(env) == before_sql
    assert sql_rows(env)[env["foreign_doc"]] == before_sql[env["foreign_doc"]]
    assert index_rows(env) == before_milvus
    before_zero_sql, before_zero_index = sql_rows(env), inf_rows(env)
    # No index exists for this other dataset: all three engines can update a
    # truly zero-chunk document, verified through the independent Python SQL.
    assert go_change(env, [env["foreign_doc"]], 0, dataset=env["other_kb"]).json() == {"code": 0, "message": "success", "data": {env["foreign_doc"]: {"status": "0"}}}
    assert sql_rows(env)[env["foreign_doc"]]["status"] == "0"
    after_zero_sql = sql_rows(env)
    after_zero_sql[env["foreign_doc"]]["status"] = "1"
    assert after_zero_sql == before_zero_sql
    assert inf_rows(env) == before_zero_index
    assert index_rows(env) == before_milvus
    print(f"Actual Go {env['go_engine']} HTTP: truthful per-document storage result, SQL independent readback, preserved source/vector/create fields and cross-dataset isolation")


def test_go_actual_body_membership_auth(go_status_api: dict[str, Any]) -> None:
    env = go_status_api
    before = sql_rows(env)
    for state in [True, False, 0.0, 1.0, "2", None, []]:
        assert go_change(env, [env["doc"]], state).status_code == 422
    for ids in [[], [1], [None], [""], "a"]:
        assert go_change(env, ids, 0).status_code == 422
    for key in ["invalid-key", "a.b.c"]:
        assert go_change(env, [env["doc"]], 0, key).status_code == 401
    assert go_change(env, [env["doc"]], 0, dataset=uuid4().hex).json()["code"] == 109
    assert go_change(env, [env["doc"]], 0, env["api_key"]).json()["code"] == 109
    for role, active in [("normal", "1"), ("invite", "1"), ("admin", "0"), ("admin", "1")]:
        with Session(env["engine"]) as db:
            db.execute(sa.delete(UserTenant).where(UserTenant.tenant_id == env["owners"][0], UserTenant.user_id == env["owners"][1]))
            db.add(UserTenant(id=uuid4().hex, tenant_id=env["owners"][0], user_id=env["owners"][1], role=role, status=active, invited_by=env["owners"][0]))
            db.commit()
        response = go_change(env, [env["doc"]], 0, env["api_key"])
        expected = (0 if env["go_engine"] == "infinity" else 500) if role == "admin" and active == "1" else 109
        assert response.json()["code"] == expected
    if env["go_engine"] != "infinity":
        assert sql_rows(env) == before
    jwt_response = go_change(env, [env["zero"]], 1, env["jwt"])
    assert jwt_response.json()["code"] == (500 if env["go_engine"] == "milvus" else 0)

    with Session(env["engine"]) as db:
        db.execute(sa.update(User).where(User.id == env["owners"][0]).values(status="0"))
        db.commit()
    try:
        for token in [env["owner_key"], env["jwt"]]:
            assert go_change(env, [env["doc"]], 0, token).status_code == 401
    finally:
        with Session(env["engine"]) as db:
            db.execute(sa.update(User).where(User.id == env["owners"][0]).values(status="1"))
            db.commit()


@pytest.mark.parametrize("failure_phase", ["execute", "commit"])
def test_go_sql_failure_compensates_real_infinity_and_continues(go_status_api: dict[str, Any], failure_phase: str) -> None:
    env = go_status_api
    if env["go_engine"] != "infinity":
        assert go_change(env, [env["doc"]], 0).json()["code"] == 500
        return
    before_sql, before_index = sql_rows(env), inf_rows(env)
    name = "status_guard_" + uuid4().hex
    with env["engine"].begin() as db:
        db.execute(
            sa.text(
                f"CREATE FUNCTION usr_ai.{name}() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.id = '{env['doc']}' THEN RAISE EXCEPTION 'controlled SQL status failure'; END IF; RETURN NEW; END $$"
            )
        )
        trigger = (
            f"CREATE CONSTRAINT TRIGGER {name} AFTER UPDATE ON usr_ai.t_ai_documents DEFERRABLE INITIALLY DEFERRED"
            if failure_phase == "commit"
            else f"CREATE TRIGGER {name} BEFORE UPDATE OF status ON usr_ai.t_ai_documents"
        )
        db.execute(sa.text(f"{trigger} FOR EACH ROW EXECUTE FUNCTION usr_ai.{name}()"))
    try:
        response = go_change(env, [env["doc"], env["second"]], 0).json()
        assert response["code"] == 500 and response["data"][env["doc"]] == {"error": "Failed to update document status; retry to reconcile."}
        assert response["data"][env["second"]] == {"status": "0"}
        assert sql_rows(env)[env["doc"]] == before_sql[env["doc"]]
        current = inf_rows(env)
        for index, doc_id in enumerate(current["doc_id"]):
            assert current["available_int"][index] == (1 if doc_id == env["doc"] else 0)
        current["available_int"] = before_index["available_int"]
        assert current == before_index
    finally:
        with env["engine"].begin() as db:
            db.execute(sa.text(f"DROP TRIGGER {name} ON usr_ai.t_ai_documents"))
            db.execute(sa.text(f"DROP FUNCTION usr_ai.{name}()"))


def test_go_query_after_delete_cannot_report_success(go_status_api: dict[str, Any]) -> None:
    from concurrent.futures import ThreadPoolExecutor

    env = go_status_api
    before_index = inf_rows(env)
    with env["engine"].begin() as db:
        db.execute(sa.delete(Document).where(Document.id == env["zero"]))
    response = go_change(env, [env["zero"], env["doc"]], 0).json()
    assert response["code"] == 500 and response["data"][env["zero"]] == {"error": "Document not found in this dataset."}
    # Also hold a real row lock, queue a read, then delete before its read wins.
    with ThreadPoolExecutor(max_workers=1) as executor, env["engine"].connect() as db:
        tx = db.begin()
        db.execute(sa.select(Document.id).where(Document.id == env["second"]).with_for_update())
        later = executor.submit(go_change, env, [env["second"]], 0)
        time.sleep(0.2)
        assert not later.done()
        db.execute(sa.delete(Document).where(Document.id == env["second"]))
        tx.commit()
        result = later.result(timeout=10).json()
        assert result["code"] == 500 and result["data"][env["second"]] == {"error": "Document not found in this dataset."}
    if env["go_engine"] != "infinity":
        assert inf_rows(env) == before_index


def test_go_concurrent_status_writes_end_at_sql_winner(go_status_api: dict[str, Any]) -> None:
    from concurrent.futures import ThreadPoolExecutor

    env = go_status_api
    before = sql_rows(env)
    with ThreadPoolExecutor(max_workers=2) as pool:
        disable = pool.submit(go_change, env, [env["doc"]], 0)
        enable = pool.submit(go_change, env, [env["doc"]], 1)
        outcomes = [disable.result(timeout=30).json(), enable.result(timeout=30).json()]
    if env["go_engine"] == "infinity":
        assert [body["code"] for body in outcomes] == [0, 0]
        winner = int(sql_rows(env)[env["doc"]]["status"])
        assert all(value == winner for value in inf_rows(env, f"doc_id='{env['doc']}'")["available_int"])
    else:
        assert [body["code"] for body in outcomes] == [500, 500]
        assert sql_rows(env) == before


@pytest.mark.parametrize("go_status_api", ["infinity"], indirect=True)
def test_go_current_principal_and_python_logout(go_status_api: dict[str, Any]) -> None:
    env = go_status_api
    owner = env["owners"][0]
    before_sql, before_index = sql_rows(env), inf_rows(env)
    response = requests.post(env["base"] + "/api/v1/auth/logout", headers={"Authorization": "Bearer " + env["jwt"]}, timeout=30)
    assert response.status_code == 200 and response.json().get("code", response.json().get("retcode")) == 0
    for base in [env["base"], env["go_base"]]:
        response = requests.post(
            f"{base}/api/v1/datasets/{env['kb']}/documents/batch-update-status", headers={"Authorization": "Bearer " + env["jwt"]}, json={"doc_ids": [env["doc"]], "status": 0}, timeout=30
        )
        assert response.status_code == 401
    assert sql_rows(env) == before_sql and inf_rows(env) == before_index
    # API keys deliberately retain their current Python contract after JWT logout.
    assert go_change(env, [env["doc"]], 0).json()["code"] == 0
    assert go_change(env, [env["doc"]], 1).json()["code"] == 0
    assert inf_rows(env) == before_index
    with env["engine"].begin() as db:
        db.execute(sa.update(User).where(User.id == owner).values(access_token="active"))
    cases = [
        (User, "is_authenticated", False, True),
        (User, "is_anonymous", True, False),
        (User, "is_active", False, True),
        (User, "status", "0", "1"),
        (Tenant, "status", "0", "1"),
        (UserTenant, "status", "0", "1"),
        (UserTenant, "role", "normal", "owner"),
    ]
    for model, field, invalid, valid in cases:
        condition = (model.user_id == owner) & (model.tenant_id == owner) if model is UserTenant else model.id == owner
        with env["engine"].begin() as db:
            db.execute(sa.update(model).where(condition).values({field: invalid}))
        try:
            for token in [env["jwt"], env["owner_key"]]:
                assert go_change(env, [env["doc"]], 0, token).status_code == 401
            assert sql_rows(env) == before_sql and inf_rows(env) == before_index
        finally:
            with env["engine"].begin() as db:
                db.execute(sa.update(model).where(condition).values({field: valid}))
    assert go_change(env, [env["doc"]], 1, env["jwt"]).json()["code"] == 0

    for model in [Tenant, UserTenant]:
        condition = (model.user_id == owner) & (model.tenant_id == owner) if model is UserTenant else model.id == owner
        with env["engine"].begin() as db:
            saved = dict(db.execute(sa.select(model.__table__).where(condition)).mappings().one())
            db.execute(sa.delete(model).where(condition))
        try:
            for token in [env["jwt"], env["owner_key"]]:
                assert go_change(env, [env["doc"]], 0, token).status_code == 401
            assert sql_rows(env) == before_sql and inf_rows(env) == before_index
        finally:
            with env["engine"].begin() as db:
                db.execute(sa.insert(model).values(saved))


@pytest.mark.parametrize("go_status_api", ["infinity"], indirect=True)
def test_go_different_documents_private_rpc_and_cancelled_http(go_status_api: dict[str, Any]) -> None:
    import socket
    from concurrent.futures import ThreadPoolExecutor
    from urllib.parse import urlparse

    env = go_status_api
    proxy = env["rpc_proxy"]
    before_sql, before_index = sql_rows(env), inf_rows(env)
    proxy.arm = True
    addr = urlparse(env["go_base"])
    body = json.dumps({"doc_ids": [env["doc"]], "status": 0}).encode()
    path = f"/api/v1/datasets/{env['kb']}/documents/batch-update-status"
    with socket.create_connection((addr.hostname, addr.port), timeout=10) as client, ThreadPoolExecutor(max_workers=2) as executor:
        client.sendall(f"POST {path} HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer {env['owner_key']}\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body)
        assert proxy.entered.wait(10)
        blocked_connection = next(identifier for identifier, method in reversed(proxy.events) if method == "Update")
        try:
            later = executor.submit(go_change, env, [env["doc"]], 1)
            other = executor.submit(go_change, env, [env["second"]], 0)
            assert other.result(timeout=10).json()["code"] == 0
            assert not later.done()
            assert sql_rows(env)[env["doc"]] == before_sql[env["doc"]]
            assert any(identifier != blocked_connection and method == "Update" for identifier, method in proxy.events)
            client.shutdown(socket.SHUT_RDWR)
            time.sleep(0.1)
            assert not later.done() and sql_rows(env)[env["doc"]] == before_sql[env["doc"]]
        finally:
            proxy.release.set()
        assert later.result(timeout=10).json()["code"] == 0
    rows = inf_rows(env)
    for i, doc in enumerate(rows["doc_id"]):
        assert rows["available_int"][i] == (1 if doc == env["doc"] else 0)
    rows["available_int"] = before_index["available_int"]
    assert rows == before_index
    assert sql_rows(env)[env["doc"]] == before_sql[env["doc"]]
    private_connections = {identifier for identifier, method in proxy.events if method == "Update"}
    assert proxy.events[0][0] not in private_connections
    deadline = time.monotonic() + 5
    while proxy.active & private_connections and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not proxy.active & private_connections
    print(
        "Distinct actual Thrift TCP connections; delayed real Update held SQL lock after HTTP socket close, different document progressed, subsequent winner retained; private connections drained/closed"
    )


@pytest.mark.parametrize("go_status_api", ["infinity"], indirect=True)
@pytest.mark.parametrize("stack", ["python", "go"])
def test_real_infinity_mothers_retry_and_commit_recovery(go_status_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, stack: str) -> None:
    import logging

    from core.utils.infinity_conn import InfinityConnection
    from tests.integration.test_document_status import change

    env = go_status_api
    table = env["infinity_table"]
    parent = uuid4().hex
    table.update(f"doc_id = '{env['doc']}'", {"mom_id": parent})
    table.insert([{"id": parent, "doc_id": env["doc"], "mom_id": parent, "available_int": 0, "content": "hidden mother", "created": "original mother", "vector": [0.1, 0.2, 0.3, 0.4]}])
    opened, closed = [], []

    class OwnedPool:
        def get_conn(self) -> Any:
            client = infinity.connect(NetworkAddress("127.0.0.1", env["infinity_port"]))
            opened.append(client)
            return client

        def release_conn(self, client: Any) -> None:
            client.disconnect()
            closed.append(client)

    if stack == "python":
        cls = next(cell.cell_contents for cell in InfinityConnection.__closure__ if isinstance(cell.cell_contents, type))
        store = object.__new__(cls)
        store.connPool, store.dbName, store.logger = OwnedPool(), env["infinity_db"], logging.getLogger(__name__)
        monkeypatch.setattr(settings, "docStoreConn", store)
    request_status = change if stack == "python" else go_change
    before_sql, before_index = sql_rows(env), inf_rows(env)
    assert request_status(env, [env["doc"]], 0).json()["code"] == 0
    assert all(value == 0 for value in inf_rows(env, f"doc_id = '{env['doc']}'")["available_int"])
    assert request_status(env, [env["doc"]], 1).json()["code"] == 0
    assert inf_rows(env) == before_index and sql_rows(env) == before_sql
    table.update(f"id = '{parent}'", {"available_int": 1})
    assert request_status(env, [env["doc"]], 1).json()["code"] == 0
    assert inf_rows(env) == before_index
    name = "mother_guard_" + uuid4().hex
    with env["engine"].begin() as db:
        db.execute(
            sa.text(
                f"CREATE FUNCTION usr_ai.{name}() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.id = '{env['doc']}' THEN RAISE EXCEPTION 'controlled mother commit failure'; END IF; RETURN NEW; END $$"
            )
        )
        db.execute(sa.text(f"CREATE CONSTRAINT TRIGGER {name} AFTER UPDATE ON usr_ai.t_ai_documents DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION usr_ai.{name}()"))
    try:
        response = request_status(env, [env["doc"]], 0).json()
        assert response["code"] == 500 and "error" in response["data"][env["doc"]]
        assert sql_rows(env) == before_sql and inf_rows(env) == before_index
        visible = inf_rows(env, f"doc_id = '{env['doc']}' AND available_int = 1")
        assert len(visible["id"]) == 2 and parent not in visible["id"]
    finally:
        with env["engine"].begin() as db:
            db.execute(sa.text(f"DROP TRIGGER {name} ON usr_ai.t_ai_documents"))
            db.execute(sa.text(f"DROP FUNCTION usr_ai.{name}()"))
    assert opened == closed
    print(f"Actual {stack} Infinity mothers remain0 across enable/same-state/SQL-commit recovery; visible1 excludes mother; complete source/vector/created fields and SQL preserved")
