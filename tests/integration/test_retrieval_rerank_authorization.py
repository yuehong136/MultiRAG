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
        monkeypatch.setattr(doc, "queue_tasks", schedule)
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
