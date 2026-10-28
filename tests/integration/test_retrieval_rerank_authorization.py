"""Real SQL/auth/registered HTTP routes, with controlled provider and ranking IO."""

import asyncio
import hashlib
import json
import os
import socket
import subprocess
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
import requests
import sqlalchemy as sa
import uvicorn
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from api.db.db_models import APIToken, Document, Knowledgebase, Task, Tenant, TenantLLM, User, UserTenant, get_async_db, get_db
from api.db.joint_services import tenant_model_service as models
from api.db.services.tenant_llm_service import TenantLLMService
from common import resources
from tests.support.database import scratch_database
from tests.support.event_loop import UVICORN_LOOP
from tests.support.web_consumer import tsx_runner


@pytest.fixture
def rerank_api(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, postgres_service: None) -> Iterator[dict[str, Any]]:
    # This database-only contract owns its signer and controlled retrieval boundary.
    # It must also work after another test imported the cached application.
    monkeypatch.setitem(resources._state, "secret_key", "rerank-auth-contract-signer")
    from api.apps import app
    from api.apps.restful_apis import bot as session
    from api.apps.restful_apis import chunk as doc
    from api.apps.services import dataset_search_service
    from api.db import db_models

    evidence = Path(os.environ.get("RERANK_AUTH_EVIDENCE", str(tmp_path)))
    with scratch_database(evidence) as engine, ExitStack() as cleanup:
        owners = {key: uuid4().hex for key in ("caller", "shared", "foreign")}
        ids = {key: uuid4().hex for key in ("own", "shared", "foreign", "incompatible")}
        token, beta = uuid4().hex, uuid4().hex
        manifest: dict[str, Any] = {
            "database": engine.url.database,
            "pid": os.getpid(),
            "dataset_ids": ids,
            "owner_ids": owners,
            "containers": [],
            "buckets": [],
            "indexes": [],
            "redis_keys": [],
            "port": None,
        }
        manifest_path = evidence / f"{engine.url.database}.http.json"

        def save() -> None:
            manifest_path.write_text(json.dumps(manifest, indent=2))

        cleanup.callback(save)
        save()
        with Session(engine) as db:
            for owner in owners.values():
                db.add(User(id=owner, email=f"{owner}@rerank.test", nickname="Rerank auth", password="unused", access_token="active"))
                db.add(Tenant(id=owner, name="Rerank auth", llm_id="", embd_id="embedding", asr_id="", img2txt_id="", parser_ids="naive"))
                db.add(UserTenant(id=uuid4().hex, user_id=owner, tenant_id=owner, role="owner", invited_by=owner))
                db.add(TenantLLM(tenant_id=owner, llm_factory="OpenAI", llm_name="embedding", mdl_type="embedding", api_key="controlled-provider-key"))
            db.add(UserTenant(id=uuid4().hex, user_id=owners["caller"], tenant_id=owners["shared"], role="normal", invited_by=owners["shared"]))
            for name, identifier in ids.items():
                owner = owners[{"own": "caller", "incompatible": "shared"}.get(name, name)]
                db.add(Knowledgebase(id=identifier, tenant_id=owner, created_by=owner, name=name, permission="team", embd_id="other" if name == "incompatible" else "embedding", parser_config={}))
            db.add(APIToken(tenant_id=owners["caller"], token=token, beta=beta, name="rerank-auth"))
            registrations = {key: TenantLLM(tenant_id=owner, llm_factory="OpenAI", llm_name="rerank", mdl_type="rerank", api_key="controlled-provider-key") for key, owner in owners.items()}
            db.add_all(registrations.values())
            db.commit()
            model_ids = {key: row.id for key, row in registrations.items()}
        manifest["model_ids"] = model_ids
        save()

        async_engine = create_async_engine(engine.url, poolclass=NullPool)
        cleanup.callback(lambda: asyncio.run(async_engine.dispose()))
        sessions = async_sessionmaker(async_engine, expire_on_commit=False)

        async def async_db() -> AsyncIterator[AsyncSession]:
            async with sessions() as db:
                yield db

        def sync_db() -> Iterator[Session]:
            with Session(engine) as db:
                yield db

        monkeypatch.setitem(app.dependency_overrides, get_async_db, async_db)
        monkeypatch.setitem(app.dependency_overrides, get_db, sync_db)
        cleanup.callback(app.dependency_overrides.clear)
        monkeypatch.setattr(db_models, "SessionLocal", sa.orm.sessionmaker(engine, expire_on_commit=False))
        # Unchanged legacy embedding/name factories open short sessions. Point
        # those at scratch; the new rerank path must use its request session.
        from contextlib import contextmanager

        monkeypatch.setattr(models, "db_connection", contextmanager(sync_db))
        decoded = Mock(wraps=TenantLLMService.resolve_model_config)
        monkeypatch.setattr(TenantLLMService, "resolve_model_config", decoded)
        providers = Mock(return_value=SimpleNamespace())
        monkeypatch.setattr(TenantLLMService, "model_instance", providers)
        fetch = AsyncMock(return_value={"total": 0, "chunks": [], "doc_aggs": []})
        monkeypatch.setitem(resources._state, "retriever", SimpleNamespace(retrieval=fetch, retrieval_by_children=lambda chunks, *_: chunks))
        for module, name in ((doc, "label_question"), (session, "_label_question_with_conn"), (dataset_search_service, "label_question")):
            monkeypatch.setattr(module, name, lambda *_: {})
        translation, metadata, schedule = AsyncMock(), AsyncMock(), Mock()
        for module in (doc, session, dataset_search_service):
            monkeypatch.setattr(module, "cross_languages", translation)
            monkeypatch.setattr(module, "apply_meta_data_filter", metadata)
        from api.db.services import task_service

        monkeypatch.setattr(task_service, "queue_tasks", schedule)
        # Request-time SQL mutation monitoring, independent of provider spies.
        writes: list[str] = []

        def capture_write(conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool) -> None:
            verb = statement.lstrip().split()[0].upper()
            if verb in {"INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER", "TRUNCATE"}:
                writes.append(verb)

        for target in (engine, async_engine.sync_engine):
            sa.event.listen(target, "before_cursor_execute", capture_write)
        listener = socket.socket()
        cleanup.callback(listener.close)
        listener.bind(("127.0.0.1", 0))
        manifest["port"] = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="error", loop=UVICORN_LOOP))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)

        def stop() -> None:
            server.should_exit = True
            if thread.ident is not None:
                thread.join(timeout=15)
            listener.close()
            assert not thread.is_alive()
            with socket.socket() as client:
                assert client.connect_ex(("127.0.0.1", manifest["port"])) != 0
            manifest["listener_closed"] = True

        cleanup.callback(stop)
        save()
        thread.start()
        deadline = time.monotonic() + 30
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started
        yield {
            "base": f"http://127.0.0.1:{manifest['port']}",
            "token": token,
            "beta": beta,
            "ids": ids,
            "owners": owners,
            "model_ids": model_ids,
            "engine": engine,
            "decoded": decoded,
            "providers": providers,
            "fetch": fetch,
            "translation": translation,
            "metadata": metadata,
            "schedule": schedule,
            "writes": writes,
            "evidence": evidence,
        }
        manifest["writes"] = writes
        assert not writes


def snapshot(engine: sa.Engine) -> str:
    with Session(engine) as db:
        state = {model.__tablename__: [row.to_dict() for row in db.scalars(sa.select(model))] for model in (Knowledgebase, TenantLLM, Document, Task, APIToken)}
    return hashlib.sha256(json.dumps(state, sort_keys=True, default=str).encode()).hexdigest()


def post(env: dict[str, Any], route: str, datasets: list[str], model_id: int, *, authenticated: bool = True, extras: dict[str, Any] | None = None) -> requests.Response:
    body: dict[str, Any] = {"question": "q", "tenant_rerank_id": model_id}
    token = env["token"]
    if route == "embedded":
        path, body["kb_id"], token = "/searchbots/retrieval_test", datasets, env["beta"]
    else:
        path = {"sdk": "/retrieval", "collection": "/datasets/search", "single": f"/datasets/{datasets[0]}/search"}[route]
        body["dataset_ids"] = datasets
    response = requests.post(env["base"] + "/api/v1" + path, headers={"Authorization": "Bearer " + token} if authenticated else {}, json={**body, **(extras or {})}, timeout=30)
    assert response.status_code == (200 if authenticated else 401), response.text
    return response


def business_code(result: dict[str, Any]) -> int:
    # Embedded SDK keeps its existing retcode envelope; REST uses code.
    return result["code"] if "code" in result else result["retcode"]


def test_registered_http_scope_and_zero_denial_side_effects(rerank_api: dict[str, Any]) -> None:
    env = rerank_api
    ids, models_by_owner = env["ids"], env["model_ids"]
    before = snapshot(env["engine"])
    checks: list[dict[str, Any]] = []
    for route in ("sdk", "embedded", "collection", "single"):
        for selection, model, reason in (
            ([ids["own"]], models_by_owner["foreign"], "foreign model"),
            ([ids["own"]], max(models_by_owner.values()) + 10000, "missing model"),
            ([ids["own"]], 0, "zero model id"),
            ([ids["own"], ids["foreign"]], models_by_owner["caller"], "mixed unauthorized datasets"),
            ([ids["own"]], models_by_owner["caller"], "missing auth"),
        ):
            for name in ("decoded", "providers", "fetch", "translation", "metadata", "schedule"):
                env[name].reset_mock()
            result = post(env, route, selection, model, authenticated=reason != "missing auth", extras={"cross_languages": ["English"], "tenant_id": env["owners"]["foreign"]}).json()
            assert business_code(result) != 0, (route, reason, result)
            if reason in {"foreign model", "missing model", "zero model id"}:
                assert business_code(result) == 109, (route, reason, result)
            for name in ("decoded", "providers", "fetch", "translation", "metadata", "schedule"):
                env[name].assert_not_called()
            assert snapshot(env["engine"]) == before and not env["writes"]
            checks.append({"route": route, "case": reason, "code": business_code(result), "provider_calls": 0, "credential_resolutions": 0, "writes": 0, "scheduled": 0})
        # SDK ownership remains strict. Shared access is exercised by the two
        # REST variants and embedded route without broadening SDK permissions.
        allowed_cases = [(["own"], "caller")] if route == "sdk" else [(["shared"], "caller"), (["shared"], "shared"), (["own", "shared"], "shared")]
        for datasets, model_owner in allowed_cases:
            result = post(env, route, [ids[dataset] for dataset in datasets], models_by_owner[model_owner]).json()
            assert business_code(result) == 0, (route, datasets, model_owner, result)
            reranker = env["fetch"].call_args.kwargs["rerank_mdl"]
            assert reranker.tenant_id == env["owners"][model_owner]
            assert reranker.model_config["id"] == models_by_owner[model_owner]
            assert not env["writes"] and snapshot(env["engine"]) == before
            checks.append({"route": route, "case": f"allowed {datasets} dataset / {model_owner} model", "code": 0})
    for route in ("embedded", "collection", "single"):
        env["decoded"].reset_mock()
        env["providers"].reset_mock()
        result = post(env, route, [ids["own"], ids["incompatible"]], models_by_owner["caller"]).json()
        assert business_code(result) != 0, result
        env["decoded"].assert_not_called()
        env["providers"].assert_not_called()
    assert snapshot(env["engine"]) == before
    (env["evidence"] / "http-cases.json").write_text(json.dumps(checks, indent=2))


def test_sql_empty_scope_and_intersection_never_decode_foreign_credentials(rerank_api: dict[str, Any]) -> None:
    env = rerank_api
    with Session(env["engine"]) as db:
        for scope, tenant in ((set(), None), ({env["owners"]["caller"]}, None), ({env["owners"]["foreign"]}, env["owners"]["caller"])):
            env["decoded"].reset_mock()
            with pytest.raises(LookupError):
                models.get_model_config_by_id(db, env["model_ids"]["foreign"], allowed_tenant_ids=scope, tenant_id=tenant)
            env["decoded"].assert_not_called()
        config = models.get_model_config_by_id(db, env["model_ids"]["shared"], allowed_tenant_ids={env["owners"]["caller"], env["owners"]["shared"]})
        assert config["tenant_id"] == env["owners"]["shared"]


@pytest.mark.external_consumer
def test_real_web_retrieval_consumer_observes_authorization(rerank_api: dict[str, Any]) -> None:
    env = rerank_api
    root = Path(os.environ["WEB_DATASET_CHECKOUT"])
    script = env["evidence"] / "web-rerank-consumer.mts"
    script.write_text(
        """import assert from 'node:assert/strict';
import {apiClient, APIError} from 'WEB_ROOT/src/api/client.ts';
import {knowledgeRestConfig} from 'WEB_ROOT/src/api/knowledge-config.ts';
import {knowledgeRetrievalAPI} from 'WEB_ROOT/src/api/knowledge-retrieval.ts';
knowledgeRestConfig.baseURL = process.env.RERANK_BASE + '/api';
apiClient.setAuthToken(process.env.RERANK_TOKEN!);
const args = {question:'q', kb_ids:[process.env.RERANK_DATASET!]};
for (const id of JSON.parse(process.env.RERANK_ALLOWED!)) {
 const result = await knowledgeRetrievalAPI.test({...args, tenant_rerank_id:id});
 assert.equal(result.total,0);
}
await assert.rejects(knowledgeRetrievalAPI.test({...args,tenant_rerank_id:Number(process.env.RERANK_FOREIGN)}),APIError);
console.log('Real Web retrieval consumer: own/shared models accepted; foreign model rejected');
""".replace("WEB_ROOT", str(root))
    )
    execution = subprocess.run(
        [str(tsx_runner(root)), "--tsconfig", str(root / "tsconfig.app.json"), str(script)],
        cwd=root,
        text=True,
        capture_output=True,
        timeout=90,
        env={
            **os.environ,
            "RERANK_BASE": env["base"],
            "RERANK_TOKEN": env["token"],
            "RERANK_DATASET": env["ids"]["shared"],
            "RERANK_ALLOWED": json.dumps([env["model_ids"]["caller"], env["model_ids"]["shared"]]),
            "RERANK_FOREIGN": str(env["model_ids"]["foreign"]),
        },
    )
    (env["evidence"] / "web-consumer.log").write_text(execution.stdout + execution.stderr)
    assert execution.returncode == 0, execution.stdout + execution.stderr


def test_mixed_retrieval_auth_sql_and_retired_operations(bootstrapped_engine: sa.Engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Real token/user/membership SQL; controlled provider IO; independent readback."""
    from datetime import timedelta

    from fastapi.testclient import TestClient

    from api.apps import app, manager
    from api.apps.restful_apis import chunk as route
    from api.db.services import task_service
    from tests.support.resource_journal import ResourceJournal

    engine = bootstrapped_engine
    journal = ResourceJournal("mixed-retrieval-auth")
    roles = ["owner", "foreign", "inactive", "tenant_disabled", "missing_member", "member_disabled", "invited"]
    ids = {role: uuid4().hex for role in roles}
    tokens = {role: "auth-" + uuid4().hex for role in roles}
    duplicate, beta = "duplicate-" + uuid4().hex, "beta-" + uuid4().hex
    datasets = {role: uuid4().hex for role in ["owner", "foreign", "inactive"]}
    memberships = {role: uuid4().hex for role in roles if role != "missing_member"}
    emails = {role: ids[role] + "@mixed-auth.test" for role in roles}
    jwt = manager.create_access_token(data={"sub": emails["owner"]})
    inactive_jwt = manager.create_access_token(data={"sub": emails["inactive"]})
    expired = manager.create_access_token(data={"sub": emails["owner"]}, expires=timedelta(seconds=-1))
    invalid_jwt = "invalid.jwt.signature"
    evidence = Path(os.environ.get("F_AUTH_EVIDENCE", str(tmp_path))) / "mixed-auth.json"
    journal.event("reserve-sql-rows", database=engine.url.database, users=ids, memberships=memberships, datasets=datasets, api_token_tenants=list(ids.values()), evidence=str(evidence))
    report: dict[str, Any] = {"database": engine.url.database, "checks": [], "cleanup": False}

    def save() -> None:
        evidence.write_text(json.dumps(report, indent=2))
        evidence.chmod(0o600)

    def readback() -> str:
        with Session(engine) as db:
            rows = {
                model.__tablename__: [dict(row) for row in db.execute(sa.select(model.__table__).order_by(*model.__table__.primary_key.columns)).mappings()]
                for model in [User, Tenant, UserTenant, APIToken, Knowledgebase, Document, Task]
            }
        return hashlib.sha256(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()

    writes: list[str] = []

    def capture_write(conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool) -> None:
        if statement.lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER", "TRUNCATE"}:
            writes.append(statement.lstrip().split()[0].upper())

    def request_db() -> Iterator[Session]:
        with Session(engine) as db:
            yield db

    fetch = AsyncMock(return_value={"total": 0, "chunks": [], "doc_aggs": []})
    provider = Mock(return_value=SimpleNamespace())
    storage, queue = Mock(), Mock()
    monkeypatch.setitem(app.dependency_overrides, get_db, request_db)
    monkeypatch.setitem(resources._state, "retriever", SimpleNamespace(retrieval=fetch, retrieval_by_children=lambda chunks, *_: chunks))
    monkeypatch.setitem(resources._state, "storage", storage)
    monkeypatch.setattr(task_service, "queue_tasks", queue)
    monkeypatch.setattr(route, "LLMBundle", provider)
    monkeypatch.setattr(route, "get_model_config_by_type_and_name", lambda *_a, **_k: {})
    monkeypatch.setattr(route, "label_question", lambda *_a: {})
    client: TestClient | None = None
    try:
        with Session(engine) as db:
            for role, identifier in ids.items():
                db.add(User(id=identifier, email=emails[role], nickname="Mixed auth", password="unused", access_token="active", is_active=role != "inactive"))
                db.add(Tenant(id=identifier, name="Mixed auth", llm_id="", embd_id="", asr_id="", img2txt_id="", parser_ids="naive", status="0" if role == "tenant_disabled" else "1"))
                if role in memberships:
                    db.add(
                        UserTenant(
                            id=memberships[role],
                            user_id=identifier,
                            tenant_id=identifier,
                            role="invite" if role == "invited" else "owner",
                            invited_by=identifier,
                            status="0" if role == "member_disabled" else "1",
                        )
                    )
                db.add(APIToken(tenant_id=identifier, token=tokens[role], beta=beta if role == "owner" else None, name="mixed-auth"))
            for role, identifier in datasets.items():
                db.add(
                    Knowledgebase(
                        id=identifier,
                        tenant_id=ids["owner"] if role == "inactive" else ids[role],
                        created_by=ids["owner"] if role == "inactive" else ids[role],
                        name="Mixed auth " + role,
                        permission="me",
                        embd_id="embedding",
                        parser_config={},
                        status="0" if role == "inactive" else "1",
                    )
                )
            for owner, value in [(ids["owner"], duplicate), (ids["foreign"], duplicate), (ids["owner"], invalid_jwt), (ids["owner"], expired), (ids["owner"], inactive_jwt)]:
                db.add(APIToken(tenant_id=owner, token=value, name="mixed-auth"))
            db.commit()
        before = readback()
        sa.event.listen(engine, "before_cursor_execute", capture_write)
        client = TestClient(app)
        denied: list[tuple[str, str | None]] = [
            ("missing", None),
            ("invalid", "Bearer unknown"),
            ("wrong-scheme", "Basic " + tokens["owner"]),
            ("extra-part", "Bearer " + tokens["owner"] + " extra"),
            ("duplicate", "Bearer " + duplicate),
            ("beta", "Bearer " + beta),
            ("invalid-jwt-no-fallback", "Bearer " + invalid_jwt),
            ("expired-jwt-no-fallback", "Bearer " + expired),
            ("inactive-jwt-no-fallback", "Bearer " + inactive_jwt),
            *[(role, "Bearer " + tokens[role]) for role in roles if role not in {"owner", "foreign"}],
        ]
        for label, authorization in denied:
            response = client.post("/api/v1/retrieval", json={"question": "q", "dataset_ids": [datasets["owner"]]}, headers={} if authorization is None else {"Authorization": authorization})
            assert response.status_code == 401 and response.json()["code"] == 401, (label, response.text)
            assert readback() == before and not writes
            provider.assert_not_called()
            fetch.assert_not_called()
            assert not storage.mock_calls
            queue.assert_not_called()
            report["checks"].append({"case": label, "status": response.status_code, "code": response.json()["code"], "sql_unchanged": True, "provider_calls": 0})
        response = client.post(
            "/api/v1/retrieval", json={"question": "q", "dataset_ids": [datasets["owner"]]}, headers=[("Authorization", "Bearer " + tokens["owner"]), ("Authorization", "Bearer " + tokens["foreign"])]
        )
        assert response.status_code == 401 and response.json()["code"] == 401
        assert readback() == before and not writes
        provider.assert_not_called()
        fetch.assert_not_called()
        report["checks"].append({"case": "duplicate-header", "status": 401, "code": 401, "sql_unchanged": True})
        response = client.post("/api/v1/retrieval", json={"question": "q", "dataset_ids": [datasets["foreign"]]}, headers={"Authorization": "Bearer " + tokens["owner"]})
        assert response.status_code == 200 and response.json()["code"] == 102
        provider.assert_not_called()
        fetch.assert_not_called()
        report["checks"].append({"case": "foreign-dataset", "status": 200, "code": 102, "sql_unchanged": readback() == before})
        response = client.post("/api/v1/retrieval", json={"question": "q", "dataset_ids": [datasets["inactive"]]}, headers={"Authorization": "Bearer " + tokens["owner"]})
        assert response.status_code == 200 and response.json()["code"] == 102
        provider.assert_not_called()
        fetch.assert_not_called()
        report["checks"].append({"case": "inactive-dataset", "status": 200, "code": 102, "sql_unchanged": readback() == before})
        for label, credential in [("API", tokens["owner"]), ("JWT", jwt)]:
            response = client.post("/api/v1/retrieval", json={"question": "q", "dataset_ids": [datasets["owner"]]}, headers={"Authorization": "Bearer " + credential})
            assert response.status_code == 200 and response.json()["code"] == 0, response.text
            assert fetch.call_args.kwargs["kb_ids"] == [datasets["owner"]]
            report["checks"].append({"case": label, "status": 200, "code": 0, "controlled_retrieval": True})
        # Run the active MCP consumer through its real payload and HTTP adapter.
        # Dataset inventory/metadata are controlled ancillary boundaries here.
        import importlib.util

        import anyio
        import httpx

        spec = importlib.util.spec_from_file_location("multirag_f12_mcp", Path(__file__).resolve().parents[2] / "mcp/server/server.py")
        assert spec is not None and spec.loader is not None
        mcp_server = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mcp_server)

        async def mcp_retrieval() -> dict[str, Any]:
            connector = mcp_server.MultiRAGConnector("http://testserver")
            connector._client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app))
            monkeypatch.setattr(connector, "list_datasets_structured", AsyncMock(return_value=[{"id": datasets["owner"], "name": "Mixed auth owner", "embedding_model": "embedding"}]))
            monkeypatch.setattr(connector, "_get_document_metadata_cache", AsyncMock(return_value=({}, {})))
            try:
                return await connector.retrieval(tokens["owner"], [datasets["owner"]], question="MCP witness")
            finally:
                await connector.close()

        result = anyio.run(mcp_retrieval)
        assert result["chunks"] == [] and result["pagination"]["total_chunks"] == 0
        assert fetch.call_args.args[0] == "MCP witness" and fetch.call_args.kwargs["kb_ids"] == [datasets["owner"]]
        report["checks"].append({"case": "actual-MCP-consumer", "controlled_dataset_metadata": True, "real_HTTP_and_auth": True})
        for method in ["POST", "DELETE"]:
            response = client.request(method, "/api/v1/datasets/unused/chunks", json={"document_ids": ["unused"]})
            assert response.status_code == 404
        response = client.request("DELETE", "/api/v1/agents/unused/sessions", json={"ids": ["unused"]})
        assert response.status_code == 405
        assert not writes and readback() == before
        report.update(request_sql_unchanged=True, request_writes=writes, controlled_provider_calls=provider.call_count, controlled_retrieval_calls=fetch.call_count)
    finally:
        if client is not None:
            client.close()
        if sa.event.contains(engine, "before_cursor_execute", capture_write):
            sa.event.remove(engine, "before_cursor_execute", capture_write)
        with Session(engine) as db:
            for model, condition in [
                (APIToken, APIToken.tenant_id.in_(list(ids.values()))),
                (Knowledgebase, Knowledgebase.id.in_(list(datasets.values()))),
                (UserTenant, UserTenant.id.in_(list(memberships.values()))),
                (Tenant, Tenant.id.in_(list(ids.values()))),
                (User, User.id.in_(list(ids.values()))),
            ]:
                db.execute(sa.delete(model).where(condition))
            db.commit()
        with Session(engine) as db:
            remaining = {
                model.__tablename__: db.scalar(sa.select(sa.func.count()).select_from(model).where(condition))
                for model, condition in [
                    (APIToken, APIToken.tenant_id.in_(list(ids.values()))),
                    (Knowledgebase, Knowledgebase.id.in_(list(datasets.values()))),
                    (UserTenant, UserTenant.id.in_(list(memberships.values()))),
                    (Tenant, Tenant.id.in_(list(ids.values()))),
                    (User, User.id.in_(list(ids.values()))),
                ]
            }
            assert not any(remaining.values()), remaining
        report["cleanup_remaining"] = remaining
        journal.event("sql-cleanup-readback", database=engine.url.database, owned_rows_absent=True)
        report["cleanup"] = True
        save()
