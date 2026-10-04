"""Dataset metadata/ingestions through real HTTP, auth and isolated PostgreSQL."""

import asyncio
import copy
import json
import os
import socket
import subprocess
import threading
import time
from collections.abc import AsyncIterator, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import requests
import sqlalchemy as sa
import uvicorn
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from api.db.db_models import APIToken, Document, Knowledgebase, PipelineOperationLog, Tenant, User, UserTenant, get_async_db
from api.db.services.task_service import GRAPH_RAPTOR_FAKE_DOC_ID

FIELDS = [{"key": "author", "description": "Author", "enum": ["Alice"], "restrictDefinedValues": True}]
PARSER = {"chunk_token_num": 128, "delimiter": "\n", "raptor": {"use_raptor": True}, "metadata": []}


def sql_state(env: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Independent connection, complete rows, only this fixture's exact IDs."""
    result = {}
    with env["engine"].connect() as db:
        for model, ids in [(Knowledgebase, env["datasets"]), (Document, env["documents"]), (PipelineOperationLog, env["logs"])]:
            result[model.__tablename__] = {row["id"]: dict(row) for row in db.execute(sa.select(model.__table__).where(model.id.in_(ids))).mappings()}
    return result


@pytest.fixture
def management_api(bootstrapped_engine: sa.Engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[dict[str, Any]]:
    from api.apps import app, manager

    users = [uuid4().hex for _ in range(3)]
    datasets, documents, logs = ([uuid4().hex for _ in range(count)] for count in [3, 3, 5])
    token_names = ["d780-" + uuid4().hex[:15] for _ in users]
    tokens = ["d780-key-" + uuid4().hex for _ in users]
    with Session(bootstrapped_engine) as db:
        for user, name, token in zip(users, token_names, tokens, strict=True):
            db.add(User(id=user, email=f"{user}@d780.test", nickname="HTTP scratch", password="unused", access_token="active"))
            db.add(Tenant(id=user, name="HTTP scratch", llm_id="", embd_id="", asr_id="", img2txt_id="", parser_ids="naive"))
            db.add(UserTenant(id=uuid4().hex, tenant_id=user, user_id=user, role="owner", invited_by=user))
            db.add(APIToken(tenant_id=user, token=token, name=name))
        db.add(UserTenant(id=uuid4().hex, tenant_id=users[0], user_id=users[2], role="admin", invited_by=users[0]))
        for i, (dataset, doc) in enumerate(zip(datasets, documents, strict=True)):
            owner = users[0] if i < 2 else users[1]
            db.add(Knowledgebase(id=dataset, tenant_id=owner, created_by=owner, name="Metadata scratch", embd_id="scratch", parser_id="naive", parser_config=copy.deepcopy(PARSER)))
            db.add(Document(id=doc, kb_id=dataset, created_by=owner, name="source.txt", parser_id="naive", type="txt", parser_config=copy.deepcopy(PARSER)))
        for i, identifier in enumerate(logs):
            db.add(
                PipelineOperationLog(
                    id=identifier,
                    tenant_id=users[0],
                    kb_id=datasets[0] if i < 4 else datasets[1],
                    document_id=GRAPH_RAPTOR_FAKE_DOC_ID if i != 3 else documents[0],
                    parser_id="naive",
                    document_name="HTTP scratch",
                    document_suffix="txt",
                    document_type="txt",
                    source_from="local",
                    operation_status="success" if i % 2 == 0 else "failed",
                    task_type="GraphRAG",
                    create_date=datetime(2025, 1, i + 1),
                    create_time=1735689600000 + i * 86400000,
                )
            )
        db.flush()
        # BaseModel's insert hook sets current timestamps. Seed date boundaries
        # with explicit SQL after insertion rather than disabling that hook.
        for i, identifier in enumerate(logs):
            db.execute(sa.update(PipelineOperationLog.__table__).where(PipelineOperationLog.id == identifier).values(create_date=datetime(2025, 1, i + 1), create_time=1735689600000 + i * 86400000))
        db.commit()

    engine = create_async_engine(bootstrapped_engine.url, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def scratch_db() -> AsyncIterator[AsyncSession]:
        async with sessions() as db:
            yield db

    monkeypatch.setitem(app.dependency_overrides, get_async_db, scratch_db)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    env = {
        "engine": bootstrapped_engine,
        "base": f"http://127.0.0.1:{port}",
        "users": users,
        "datasets": datasets,
        "documents": documents,
        "logs": logs,
        "jwt": [manager.create_access_token(data={"sub": f"{user}@d780.test"}) for user in users],
        "keys": tokens,
    }
    record = {"database": bootstrapped_engine.url.database, "users": users, "datasets": datasets, "documents": documents, "logs": logs, "token_names": token_names, "port": port}
    evidence = Path(os.environ.get("MULTIRAG_D780_EVIDENCE_DIR", str(tmp_path)))
    evidence.mkdir(parents=True, exist_ok=True)
    record_path = evidence / (users[0] + ".json")
    env["record_path"] = record_path
    record_path.write_text(json.dumps(record))
    thread.start()
    try:
        deadline = time.monotonic() + 30
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started
        yield env
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        listener.close()
        asyncio.run(engine.dispose())
        assert not thread.is_alive()
        with bootstrapped_engine.begin() as db:
            scopes = [(PipelineOperationLog, logs), (Document, documents), (Knowledgebase, datasets), (UserTenant, users), (APIToken, token_names), (Tenant, users), (User, users)]
            for model, ids in scopes:
                column = model.user_id if model is UserTenant else model.name if model is APIToken else model.id
                db.execute(sa.delete(model).where(column.in_(ids)))
        # Independent readback after the deletion transaction committed.
        with bootstrapped_engine.connect() as db:
            remaining = {}
            for model, ids in scopes:
                column = model.user_id if model is UserTenant else model.name if model is APIToken else model.id
                remaining[model.__tablename__] = db.scalar(sa.select(sa.func.count()).select_from(model).where(column.in_(ids)))
            assert not any(remaining.values()), remaining
        with socket.socket() as client:
            assert client.connect_ex(("127.0.0.1", port)) != 0
        record.update(remaining=remaining, listener_closed=True)
        record_path.write_text(json.dumps(record))


def request_api(env: dict[str, Any], method: str, suffix: str, *, credential: str | None = None, payload: Any = None, params: Any = None) -> requests.Response:
    headers = {"Authorization": f"Bearer {credential}"} if credential else {}
    return requests.request(method, env["base"] + "/api/v1" + suffix, headers=headers, json=payload, params=params, timeout=30)


def metadata_path(env: dict[str, Any], document: bool = False) -> str:
    return f"/datasets/{env['datasets'][0]}" + (f"/documents/{env['documents'][0]}" if document else "") + "/metadata/config"


@pytest.mark.parametrize("route", ["dataset-get", "dataset-put", "document-put", "ingestions"])
@pytest.mark.parametrize("credential", [None, "bad-jwt", "bad-api-key"])
def test_management_real_auth_rejection(management_api: dict[str, Any], route: str, credential: str | None) -> None:
    env = management_api
    before = sql_state(env)
    actual = credential
    if credential == "bad-jwt":
        header, payload_segment, signature = env["jwt"][0].split(".")
        signature = ("A" if signature[0] != "A" else "B") + signature[1:]
        actual = ".".join([header, payload_segment, signature])
    suffix = f"/datasets/{env['datasets'][0]}/ingestions" if route == "ingestions" else metadata_path(env, route == "document-put")
    method = "PUT" if route.endswith("put") else "GET"
    payload = {"metadata": FIELDS} if route == "document-put" else {"fields": FIELDS}
    response = request_api(env, method, suffix, credential=actual, payload=payload if method == "PUT" else None)
    message = "`Authorization` can't be empty" if credential is None else "Authentication error: invalid credentials!"
    assert response.status_code == 401 and response.json() == {"retcode": 109, "retmsg": message, "data": False}
    assert sql_state(env) == before


@pytest.mark.parametrize("kind", ["jwt", "keys"])
def test_dataset_metadata_real_roundtrip_and_empty_fields(management_api: dict[str, Any], kind: str) -> None:
    env = management_api
    key, path = env[kind][0], metadata_path(env)
    before = sql_state(env)
    assert request_api(env, "GET", path, credential=key).json() == {"code": 0, "data": {"enabled": False, "fields": [], "metadata": [], "built_in_metadata": []}}
    for config in [{"enabled": True, "fields": FIELDS}, {"enabled": False, "fields": []}, {}]:
        expected = {"enabled": config.get("enabled", True), "fields": config.get("fields", [])}
        response = request_api(env, "PUT", path, credential=key, payload=config)
        assert response.status_code == 200 and response.json() == {"code": 0, "data": expected}
        assert request_api(env, "GET", path, credential=key).json() == {"code": 0, "data": {**expected, "metadata": expected["fields"], "built_in_metadata": []}}
        after = sql_state(env)
        target = after[Knowledgebase.__tablename__][env["datasets"][0]]
        assert target["parser_config"] == {**PARSER, "metadata": expected["fields"], "enable_metadata": expected["enabled"]}
        for dataset in env["datasets"][1:]:
            assert after[Knowledgebase.__tablename__][dataset] == before[Knowledgebase.__tablename__][dataset]
        assert after[Document.__tablename__] == before[Document.__tablename__] and after[PipelineOperationLog.__tablename__] == before[PipelineOperationLog.__tablename__]


@pytest.mark.parametrize("kind", ["jwt", "keys"])
def test_document_metadata_real_array_schema_empty_and_admin(management_api: dict[str, Any], kind: str) -> None:
    env = management_api
    before = sql_state(env)
    path = metadata_path(env, True)
    schema = {"type": "object", "properties": {"author": {"type": "string"}}}
    for value, actor in [(FIELDS, 0), (schema, 0), ([], 2)]:
        response = request_api(env, "PUT", path, credential=env[kind][actor], payload={"metadata": value})
        assert response.status_code == 200 and response.json()["code"] == 0
        assert response.json()["data"]["parser_config"] == {**PARSER, "metadata": value}
        after = sql_state(env)
        assert after[Document.__tablename__][env["documents"][0]]["parser_config"] == {**PARSER, "metadata": value}
        for doc in env["documents"][1:]:
            assert after[Document.__tablename__][doc] == before[Document.__tablename__][doc]
        assert after[Knowledgebase.__tablename__] == before[Knowledgebase.__tablename__] and after[PipelineOperationLog.__tablename__] == before[PipelineOperationLog.__tablename__]


def test_metadata_real_invalid_ownership_and_body(management_api: dict[str, Any]) -> None:
    env = management_api
    before = sql_state(env)
    key, dataset, doc = env["keys"][0], env["datasets"][0], env["documents"][0]
    for method, path, credential, payload, code in [
        ("GET", "/datasets/missing/metadata/config", key, None, 102),
        ("PUT", "/datasets/missing/metadata/config", key, {"fields": []}, 102),
        ("GET", metadata_path(env), env["jwt"][1], None, 102),
        ("PUT", metadata_path(env), env["keys"][1], {"fields": FIELDS}, 102),
        ("GET", metadata_path(env), env["keys"][2], None, 102),
        ("PUT", metadata_path(env), env["jwt"][2], {"fields": FIELDS}, 102),
        ("PUT", f"/datasets/missing/documents/{doc}/metadata/config", key, {"metadata": FIELDS}, 102),
        ("PUT", f"/datasets/{dataset}/documents/missing/metadata/config", key, {"metadata": FIELDS}, 102),
        ("PUT", f"/datasets/{dataset}/documents/{env['documents'][1]}/metadata/config", key, {"metadata": FIELDS}, 102),
        ("PUT", metadata_path(env, True), env["keys"][1], {"metadata": FIELDS}, 109),
    ]:
        response = request_api(env, method, path, credential=credential, payload=payload)
        body = response.json()
        assert response.status_code == 200 and body["code"] == code and isinstance(body["message"], str) and body["message"]
        assert "data" not in body and sql_state(env) == before
    for path, bodies in [(metadata_path(env), [None]), (metadata_path(env, True), [None, {}])]:
        for body in bodies:
            response = request_api(env, "PUT", path, credential=key, payload=body)
            assert response.status_code == 422
            errors = response.json()["detail"]
            assert errors[0]["type"] == "missing" and errors[0]["loc"] == (["body", "metadata"] if body == {} else ["body"])
            assert sql_state(env) == before


@pytest.mark.parametrize(
    "params,positions",
    [
        ({"desc": "false"}, [0, 1, 2]),
        ({"orderby": "create_date", "desc": "true", "page": 2, "page_size": 1}, [1]),
        ({"create_date_from": "2025-01-02", "create_date_to": "2025-01-03", "desc": "false"}, [1, 2]),
        ({"create_date_from": "2025-01-02", "create_date_to": "2025-01-02"}, [1]),
        ({"create_date_from": "2025-01-02", "desc": "false"}, [1, 2]),
        ({"create_date_to": "2025-01-02", "desc": "false"}, [0, 1]),
        ({"operation_status": ["success"]}, [2, 0]),
        ({"create_date_from": "2025-01-02T08:00:00+08:00", "create_date_to": "2025-01-02"}, [1]),
    ],
)
def test_ingestion_logs_real_filters_and_pagination(management_api: dict[str, Any], params: dict[str, Any], positions: list[int]) -> None:
    env = management_api
    before = sql_state(env)
    response = request_api(env, "GET", f"/datasets/{env['datasets'][0]}/ingestions", credential=env["keys"][0], params=params)
    assert response.status_code == 200 and response.json()["code"] == 0
    data = response.json()["data"]
    assert data["total"] == (3 if "page" in params else len(positions))
    assert [row["id"] for row in data["logs"]] == [env["logs"][i] for i in positions]
    assert all(row["kb_id"] == env["datasets"][0] for row in data["logs"])
    assert sql_state(env) == before


def test_ingestion_logs_real_errors_scope_and_smoke(management_api: dict[str, Any]) -> None:
    env = management_api
    before = sql_state(env)
    path, key = f"/datasets/{env['datasets'][0]}/ingestions", env["jwt"][0]
    for start, end in [("2025-02-01", "2025-01-01"), ("2025-01-02T01:00:00Z", "2025-01-02")]:
        response = request_api(env, "GET", path, credential=key, params={"create_date_from": start, "create_date_to": end})
        assert response.status_code == 200 and response.json() == {"code": 102, "message": "create_date_from must not be later than create_date_to"}
    for suffix, credential in [("/datasets/missing/ingestions", key), (path, env["keys"][1])]:
        response = request_api(env, "GET", suffix, credential=credential)
        assert response.status_code == 200 and response.json() == {"code": 102, "message": "No authorization."}
    for params, field in [({"create_date_from": "not-a-date"}, "create_date_from"), ({"page": -1}, "page")]:
        response = request_api(env, "GET", path, credential=key, params=params)
        assert response.status_code == 422 and response.json()["detail"][0]["loc"] == ["query", field]
    response = request_api(env, "GET", path + "/" + env["logs"][-1], credential=key)
    assert response.status_code == 200 and response.json() == {"code": 102, "message": "Log not found"}
    response = request_api(env, "GET", "/datasets//ingestions", credential=key)
    assert response.status_code == 404 and response.json()["code"] == 404
    assert sql_state(env) == before
    smoke = subprocess.run(["make", "smoke"], env={**os.environ, "SMOKE_BASE_URL": env["base"]}, capture_output=True, text=True, timeout=60)
    env["record_path"].with_suffix(".smoke.log").write_text(smoke.stdout + smoke.stderr + f"\nexit={smoke.returncode}\n")
    assert smoke.returncode == 0, smoke.stdout + smoke.stderr
    print("Actual metadata/ingestion HTTP auth and SQL contracts; same listener make smoke passed; exact owned IDs/readback cleanup recorded")


@pytest.mark.parametrize("kind,positions", [("file", [3]), ("dataset", [0, 1, 2])])
def test_ingestion_split_search_fields_and_permissions(management_api: dict[str, Any], kind: str, positions: list[int]) -> None:
    import sqlalchemy as sa

    env = management_api
    path = f"/datasets/{env['datasets'][0]}/ingestions"
    # Change only owned scratch rows; mixed case, Unicode and literal SQL wildcards.
    with env["engine"].begin() as db:
        db.execute(sa.update(PipelineOperationLog).where(PipelineOperationLog.id.in_(env["logs"])).values(document_name="Report_100%报告"))
    before = sql_state(env)
    params = {"log_type": kind, "keywords": "REPORT_100%报告", "desc": "false"}
    for credential in [env["keys"][0], env["jwt"][0]]:
        response = request_api(env, "GET", path, credential=credential, params=params)
        assert response.status_code == 200 and response.json()["code"] == 0
        result = response.json()["data"]
        assert result["total"] == len(positions)
        assert [row["id"] for row in result["logs"]] == [env["logs"][i] for i in positions]
        for row in result["logs"]:
            assert ("document_name" in row) is (kind == "file")
            if kind == "file":
                assert row["document_name"] == "Report_100%报告" and row["document_id"] == env["documents"][0]
                assert row["document_suffix"] == "txt" and row["document_type"] == "txt"
                assert {"pipeline_id", "pipeline_title", "dsl", "parser_id", "source_from"} <= row.keys()
        missing = request_api(env, "GET", path, credential=credential, params={**params, "keywords": "REPORT_100_报告"}).json()
        assert missing["data"] == {"total": 0, "logs": []}
        page = request_api(env, "GET", path, credential=credential, params={**params, "page": 1, "page_size": 1}).json()["data"]
        assert page["total"] == len(positions) and len(page["logs"]) == 1
    denied = request_api(env, "GET", path, credential=env["keys"][1], params=params).json()
    assert denied == {"code": 102, "message": "No authorization."}
    if kind == "file":
        for extra, count in [
            ({"types": ["pdf", "txt"], "suffix": ["txt"]}, 1),
            ({"types": ["pdf"]}, 0),
            ({"suffix": ["pdf"]}, 0),
            ({"create_date_from": "2025-01-04T08:00:00+08:00", "create_date_to": "2025-01-04"}, 1),
            ({"create_date_to": "2025-01-03"}, 0),
            ({"operation_status": ["success"]}, 0),
        ]:
            result = request_api(env, "GET", path, credential=env["keys"][0], params={**params, **extra}).json()
            assert result["code"] == 0 and result["data"]["total"] == count
    assert sql_state(env) == before


@pytest.mark.external_consumer
def test_ingestion_actual_web_consumer(management_api: dict[str, Any]) -> None:
    import json
    from pathlib import Path

    env = management_api
    root = Path(os.environ.get("WEB_DATASET_CHECKOUT", str(Path(__file__).resolve().parents[3] / "web")))
    runner = root / "node_modules/.bin/tsx"
    script = root / "scripts/verify-ingestion-logs.ts"
    assert runner.exists() and script.exists(), "Web checkout required for consumer acceptance"
    before = sql_state(env)
    result = subprocess.run(
        [str(runner), "--tsconfig", str(root / "tsconfig.app.json"), str(script)],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=90,
        env={**os.environ, "INGESTION_BASE": env["base"], "INGESTION_TOKEN": env["jwt"][0], "INGESTION_DATASET": env["datasets"][0], "INGESTION_LOG_IDS": json.dumps(env["logs"])},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ingestion Web acceptance passed" in result.stdout
    assert sql_state(env) == before
