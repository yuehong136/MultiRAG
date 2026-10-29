"""Owned PostgreSQL and HTTP evidence for provider-instance model lifecycles."""

import importlib.util
import json
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

from api.db.db_models import Tenant, TenantLLM, TenantModel, TenantModelInstance, TenantModelProvider
from api.db.joint_services.tenant_model_service import get_model_config_by_id, get_model_config_by_type_and_name, get_tenant_default_model_by_type
from api.db.model_backfill import backfill
from api.db.model_contract import ConnectionCandidate, ModelRegistration
from api.db.services import provider_verification
from common import resources, settings
from tests.support.provider_instances import REAL_VERIFY_MODEL, tenant
from tests.support.provider_instances import http as http
from tests.support.provider_instances import owned_engine as owned_engine
from tests.support.resource_journal import ResourceJournal

pytestmark = pytest.mark.integration


def _exercise_discovery(http: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch, tmp_path: Path, web_checkout: Path | None = None) -> None:
    """A discovered declaration is not an ID until verified atomic SQL creation."""
    import hashlib
    import socket
    import threading
    from contextlib import ExitStack
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from api.db.services import model_provider_service
    from tests.support.provider_instances import REAL_VERIFY_MODEL

    client, owner, engine, _module = http
    monkeypatch.setattr(settings, "FACTORY_LLM_INFOS", [{"name": "Ollama", "llm": []}, {"name": "MinerU", "llm": []}])
    journal = ResourceJournal("discovery-real-http-atomic-sql")

    async def owned_probe(provider: str, key: str, base: str, declaration: ModelRegistration) -> dict[str, Any]:
        from litellm.litellm_core_utils.logging_worker import GLOBAL_LOGGING_WORKER

        assert GLOBAL_LOGGING_WORKER._worker_task is None
        journal.event("reserve-sdk-logging-worker", model=declaration.model_name, purpose="owned isolated provider probe")
        try:
            return await REAL_VERIFY_MODEL(provider, key, base, declaration)
        finally:
            await GLOBAL_LOGGING_WORKER.stop()
            await GLOBAL_LOGGING_WORKER.clear_queue()
            assert GLOBAL_LOGGING_WORKER._worker_task is None and not GLOBAL_LOGGING_WORKER._running_tasks
            journal.event("sdk-logging-worker-cleanup", stopped=True, drained=True)

    monkeypatch.setattr(provider_verification, "verify_model", owned_probe)
    state = {"bad_show": False, "bad_chat": False}
    seen: list[tuple[str, str]] = []

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *_args: Any) -> None:
            return

        def reply(self, value: dict[str, Any]) -> None:
            content = json.dumps(value).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def do_GET(self) -> None:
            assert self.path == "/api/tags"
            seen.append((self.path, self.headers.get("Authorization", "")))
            self.reply({"models": [{"name": "first"}, {"name": "second"}]})

        def do_POST(self) -> None:
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append((self.path, self.headers.get("Authorization", "")))
            if self.path == "/api/show":
                self.reply(
                    {"model_info": {"fixture.context_length": 0}, "details": {"family": "fixture"}, "capabilities": None if state["bad_show"] else ["completion", "vision", "tools", "thinking"]}
                )
            else:
                assert self.path == "/api/chat" and payload["model"] == "first"
                self.reply({"model": "first", "message": {"role": "assistant", "content": "" if state["bad_chat"] else "OK"}, "done": True, "prompt_eval_count": 0, "eval_count": 0})

    def counts() -> list[int]:
        with Session(engine) as reader:
            return [reader.scalar(select(func.count()).select_from(model)) for model in (TenantModelProvider, TenantModelInstance, TenantModel, TenantLLM)]

    with ExitStack() as cleanup:
        address = ("127.0.0.1", 30000 + int(uuid4().hex[:4], 16) % 20000)
        journal.event("reserve-listener", address=list(address), input_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
        server = ThreadingHTTPServer(address, Provider, bind_and_activate=False)
        cleanup.callback(server.server_close)
        server.server_bind()
        server.server_activate()
        thread = threading.Thread(target=server.serve_forever, name="discovery-provider-fixture")
        thread.start()

        def stop_listener() -> None:
            server.shutdown()
            thread.join(5)
            server.server_close()
            assert not thread.is_alive()
            with socket.socket() as reader_socket:
                assert reader_socket.connect_ex(address) != 0
            journal.event("listener-independent-readback", address=list(address), absent=True, thread_absent=True)

        cleanup.callback(stop_listener)
        body = {"api_key": "fixture-discovery-key", "base_url": f"http://127.0.0.1:{address[1]}", "region": ""}
        from api.utils.api_utils import async_current_user

        authenticated = client.app.dependency_overrides.pop(async_current_user)
        try:
            denied = client.post("/api/v1/providers/Ollama/models/discovery", json=body, headers={"Authorization": "Bearer invalid-fixture-token"})
            assert denied.status_code == 401 and not seen
        finally:
            client.app.dependency_overrides[async_current_user] = authenticated
        response = client.post("/api/v1/providers/Ollama/models/discovery?page_size=1", json=body)
        assert response.status_code == 200, response.text
        assert response.headers["Cache-Control"] == "no-store"
        data = response.json()["data"]
        assert data["total"] == 2 and len(data["items"]) == 1
        item = data["items"][0]
        assert "id" not in item and "status" not in item
        assert item["max_tokens"] == 0 and item["model_types"] == ["chat", "image2text"] and item["features"] == ["is_tools", "thinking"]
        items = client.post("/api/v1/providers/Ollama/models/discovery", json=body).json()["data"]["items"]
        candidate = {**body, "model_ids": [], "custom_models": [{key: item[key] for key in ("model_name", "model_types", "max_tokens", "is_tools")} for item in items]}
        assert counts() == [0, 0, 0, 0]
        verified = client.post("/api/v1/providers/Ollama/connection", json=candidate)
        assert verified.status_code == 200 and verified.json()["data"]["verified"] is True, verified.text
        assert counts() == [0, 0, 0, 0]
        state["bad_show"] = True
        failed = client.post("/api/v1/providers/Ollama/models/discovery", json=body)
        assert failed.status_code == 502 and "fixture-discovery-key" not in failed.text
        state["bad_show"] = False
        assert client.post("/api/v1/providers/Ollama/models/discovery?api_key=forbidden", json=body).status_code == 422
        state["bad_chat"] = True
        assert client.post("/api/v1/providers/Ollama/instances", json={**candidate, "instance_name": "failed"}).status_code == 400
        assert counts() == [0, 0, 0, 0]
        state["bad_chat"] = False
        original = model_provider_service.add_model

        async def fail_second(*args: Any, **kwargs: Any) -> dict[str, Any]:
            if args[4].model_name == "second":
                raise ValueError("Controlled second model failure")
            return await original(*args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(model_provider_service, "add_model", fail_second)
            assert client.post("/api/v1/providers/Ollama/instances", json={**candidate, "instance_name": "rolled-back"}).status_code == 400
        assert counts() == [0, 0, 0, 0]
        created = client.post("/api/v1/providers/Ollama/instances", json={**candidate, "instance_name": "selected"})
        assert created.status_code == 200, created.text
        rows = client.get("/api/v1/models").json()["data"]["items"]
        assert len(rows) == 2 and all(type(row["id"]) is int and row["id"] > 0 and row["max_tokens"] == 0 for row in rows)
        with Session(engine) as reader:
            assert reader.scalars(select(TenantModel.model_types).order_by(TenantModel.model_name)).all() == [["chat", "image2text"], ["chat", "image2text"]]
        assert counts() == [1, 1, 2, 2]
        before = len(seen)
        placeholder = client.post("/api/v1/providers/MinerU/models/discovery", json={}).json()["data"]["items"]
        assert placeholder[0]["model_types"] == ["ocr"] and placeholder[0]["max_tokens"] == 0 and "id" not in placeholder[0]
        selected_placeholder = {key: placeholder[0][key] for key in ("model_name", "model_types", "max_tokens", "is_tools")}
        assert model_provider_service.candidate_models("MinerU", ConnectionCandidate(api_key="", model_ids=[], custom_models=[selected_placeholder]))[0].model_name == "MinerU"
        assert len(seen) == before
        assert client.delete("/api/v1/providers/Ollama").status_code == 200
        assert counts() == [0, 0, 0, 0]
        if web_checkout is not None:
            from tests.support.model_discovery_web import run_web_discovery

            run_web_discovery(client, owner, engine, body["base_url"], journal, web_checkout, tmp_path)
        assert seen and all(header == "Bearer fixture-discovery-key" for _path, header in seen)
        journal.event("sql-independent-readback", tenant=owner, counts=counts(), failures_zero_writes=True, numeric_model_ids=[row["id"] for row in rows])


def test_discovery_http_candidates_verify_and_create_atomically(http: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _exercise_discovery(http, monkeypatch, tmp_path)


@pytest.mark.external_consumer
def test_discovery_pinned_web_consumer_and_independent_sql(http: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import os

    checkout = Path(os.environ["WEB_COMPLETION_CHECKOUT"])
    assert (checkout / "scripts/verify-provider-discovery-routes.ts").is_file()
    _exercise_discovery(http, monkeypatch, tmp_path, checkout)


def create(client: TestClient, name: str = "first", *, models: list[str] | None = None) -> dict[str, Any]:
    provider = client.put("/api/v1/providers", json={"provider_name": "OpenAI"})
    assert provider.status_code in {200, 409}, provider.text
    body = {"instance_name": name, "api_key": "fixture-shared-key", "base_url": f"https://{name}.invalid/v1", "region": ""}
    if models is not None:
        body["model_ids"] = models
    if models == []:
        body["custom_model"] = {"model_name": "verified-fixture-model", "model_types": ["chat"], "max_tokens": 0}
    response = client.post("/api/v1/providers/OpenAI/instances", json=body)
    assert response.status_code == 200, response.text
    if models == []:
        items = client.get(f"/api/v1/providers/OpenAI/instances/{name}/models").json()["data"]["items"]
        assert len(items) == 1
        assert client.delete(f"/api/v1/providers/OpenAI/instances/{name}/models/{items[0]['id']}").status_code == 200
    return response.json()["data"]


@pytest.mark.parametrize("suffix", ["", "@Builtin"])
def test_builtin_default_is_readonly_without_selectable_placeholder(http: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch, suffix: str) -> None:
    client, owner, engine, _module = http
    name = "org/embedding"
    monkeypatch.setenv("COMPOSE_PROFILES", "milvus,tei-cpu")
    monkeypatch.setenv("TEI_MODEL", name)
    monkeypatch.setattr(settings, "FACTORY_LLM_INFOS", [*settings.FACTORY_LLM_INFOS, {"name": "Builtin", "llm": []}])
    monkeypatch.setattr(settings, "EMBEDDING_CFG", {"api_key": "", "base_url": "http://tei.invalid", "max_tokens": 0})
    with Session(engine) as db:
        db.get(Tenant, owner).embd_id = name + suffix
        db.commit()
    response = client.get("/api/v1/models/default")
    assert response.status_code == 200
    embedding = next(row for row in response.json()["data"]["models"] if row["model_type"] == "embedding")
    assert embedding == {"model_type": "embedding", "model_id": None, "reference": name + suffix, "model_provider": "Builtin", "model_instance": "default", "model_name": name}
    assert client.get("/api/v1/models").json()["data"]["items"] == []
    assert client.patch("/api/v1/models/default", json={"model_type": "embedding", "model_id": name}).status_code == 422
    with Session(engine) as reader:
        config = get_tenant_default_model_by_type(reader, owner, "embedding")
        assert config["id"] is None and config["llm_name"] == name and config["llm_factory"] == "Builtin" and config["max_tokens"] == 0
        assert reader.get(Tenant, owner).tenant_embd_id is None
        assert reader.get(Tenant, owner).embd_id == name + suffix
        assert all(reader.scalar(select(func.count()).select_from(model)) == 0 for model in (TenantModelProvider, TenantModelInstance, TenantModel, TenantLLM))


def test_search_stream_uses_numeric_default_and_rejects_foreign_override(http: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from api.db.services import dialog_service

    client, owner, engine, _module = http
    create(client)
    default_id = client.get("/api/v1/models").json()["data"]["items"][0]["id"]
    assert client.patch("/api/v1/models/default", json={"model_type": "chat", "model_id": default_id}).status_code == 200
    selected = client.post("/api/v1/providers/OpenAI/instances/first/models", json={"model_name": "selected-chat", "model_types": ["chat"], "max_tokens": 0})
    assert selected.status_code == 200
    selected_id = selected.json()["data"]["id"]
    foreign_owner = uuid4().hex
    with Session(engine) as db:
        db.add(tenant(foreign_owner))
        db.flush()
        foreign = TenantLLM(tenant_id=foreign_owner, llm_factory="OpenAI", llm_name="private", mdl_type="chat", api_key="foreign-key", max_tokens=0)
        db.add(foreign)
        db.commit()
        foreign_id = foreign.id

    from unittest.mock import AsyncMock

    kb = SimpleNamespace(id="selected", name="Dataset", tenant_id="dataset-owner", parser_id="naive", tenant_embd_id=None, embd_id="embedding")
    monkeypatch.setattr(dialog_service.KnowledgebaseService, "accessible", lambda *_a, **_k: True)
    monkeypatch.setattr(dialog_service.KnowledgebaseService, "get_by_ids", lambda *_: [kb])
    monkeypatch.setattr(dialog_service.KnowledgebaseService, "ensure_same_embedding_model", lambda _: None)
    monkeypatch.setattr(dialog_service, "_resolve_model_config", lambda *_: {"mdl_type": "embedding"})
    configurations: list[dict[str, Any]] = []

    async def chunks(*_args: Any, **_kwargs: Any) -> AsyncIterator[str]:
        yield "<think>reason</think>Visible answer"

    def bundle(_db: Any, tenant_id: str, config: dict[str, Any]) -> Any:
        if config["mdl_type"] == "embedding":
            return object()
        assert tenant_id == owner
        configurations.append(config)
        return SimpleNamespace(max_length=4096, async_chat_streamly_delta=chunks)

    monkeypatch.setattr(dialog_service, "detached_bundle", bundle)
    retrieve = AsyncMock(return_value={"chunks": [], "doc_aggs": []})
    monkeypatch.setitem(settings.__dict__, "retriever", SimpleNamespace(retrieval=retrieve, insert_citations=lambda answer, *_a, **_k: (answer, [])))
    monkeypatch.setattr(dialog_service, "label_question", lambda *_: {})
    monkeypatch.setattr(dialog_service, "enrich_reference_metadata_async", AsyncMock())

    async def exercise() -> None:
        async_engine = create_async_engine(engine.url)
        try:
            for config, expected_id in [({}, default_id), ({"chat_id": None}, default_id), ({"chat_id": ""}, default_id), ({"chat_id": str(selected_id)}, selected_id)]:
                async with AsyncSession(async_engine) as db:
                    emitted = [item async for item in dialog_service.async_ask(db, "Question", ["selected"], owner, search_config={"doc_ids": [], **config})]
                assert emitted[-1]["final"] is True and emitted[-1]["answer"].endswith("Visible answer")
                assert configurations[-1]["id"] == expected_id and configurations[-1]["tenant_id"] == owner
                assert retrieve.call_args.kwargs["doc_ids"] == []
            assert configurations[-1]["max_tokens"] == 0
            before = len(configurations), retrieve.await_count
            async with AsyncSession(async_engine) as db:
                with pytest.raises(LookupError):
                    await anext(dialog_service.async_ask(db, "Question", ["selected"], owner, search_config={"chat_id": str(foreign_id)}))
            assert (len(configurations), retrieve.await_count) == before
        finally:
            await async_engine.dispose()

    asyncio.run(exercise())
    from api.apps.restful_apis import search_api
    from api.db.db_models import Search, User
    from api.utils.api_utils import async_current_tenant_id

    client.app.include_router(search_api.router, prefix="/api/v1")
    client.app.dependency_overrides[async_current_tenant_id] = lambda: owner
    search_id = uuid4().hex
    saved_config = {"kb_ids": ["selected"], "doc_ids": [], "similarity_threshold": 0, "vector_similarity_weight": 0}
    with Session(engine) as db:
        db.add(User(id=owner, nickname="Search owner", email=f"{owner}@fixture.invalid", password="unused", status="1"))
        db.add(Search(id=search_id, tenant_id=owner, created_by=owner, name="Saved default", search_config=saved_config, status="1"))
        db.commit()
    response = client.post(f"/api/v1/searches/{search_id}/completions", json={"question": "Question", "kb_ids": ["ignored"]})
    frames = [json.loads(part[5:]) for part in response.text.split("\n\n") if part.startswith("data:")]
    assert response.status_code == 200 and all(frame["code"] == 0 for frame in frames)
    assert any(isinstance(frame["data"], dict) and frame["data"].get("final") for frame in frames)
    assert configurations[-1]["id"] == default_id
    assert retrieve.call_args.kwargs["doc_ids"] == []
    assert retrieve.call_args.kwargs["kb_ids"] == ["selected"]
    assert retrieve.call_args.kwargs["similarity_threshold"] == retrieve.call_args.kwargs["vector_similarity_weight"] == 0
    before = len(configurations), retrieve.await_count
    with Session(engine) as db:
        db.get(Tenant, owner).tenant_llm_id = foreign_id
        db.commit()
    rejected = client.post(f"/api/v1/searches/{search_id}/completions", json={"question": "Question"})
    assert any(json.loads(part[5:])["code"] == 500 for part in rejected.text.split("\n\n") if part.startswith("data:"))
    assert (len(configurations), retrieve.await_count) == before
    with Session(engine) as db:
        db.get(Tenant, owner).tenant_llm_id = default_id
        db.commit()
    with Session(engine) as reader:
        assert reader.get(Tenant, owner).tenant_llm_id == default_id
        assert reader.get(TenantLLM, foreign_id).tenant_id == foreign_owner
        assert reader.scalar(select(func.count()).select_from(TenantModel)) == 2
        assert reader.get(Search, search_id).search_config == saved_config


def test_instances_preserve_credentials_capabilities_defaults_and_secret_projection(http: tuple[Any, ...]) -> None:
    client, owner, engine, _module = http
    create(client, "first")
    create(client, "second")
    items = client.get("/api/v1/models").json()["data"]["items"]
    assert len(items) == 2 and len({item["id"] for item in items}) == 2
    assert {tuple(item["model_types"]) for item in items} == {("chat", "image2text")}
    instances = client.get("/api/v1/providers/OpenAI/instances").json()["data"]
    assert instances["total"] == 2 and "fixture-shared-key" not in json.dumps(instances)
    for item in items:
        with Session(engine) as db:
            config = get_model_config_by_type_and_name(db, owner, "image2text", str(item["id"]))
            assert config["llm_name"] == "catalog-chat"
            assert config["api_base"] == f"https://{item['instance_name']}.invalid/v1"
            assert config["api_key"] == "fixture-shared-key" and config["mdl_type"] == "image2text"
            assert get_model_config_by_id(db, item["id"], tenant_id=owner, model_type="chat")["is_tools"] is True
            with pytest.raises(LookupError, match="capability"):
                get_model_config_by_id(db, item["id"], tenant_id=owner)
            with pytest.raises(LookupError):
                get_model_config_by_id(db, item["id"], allowed_tenant_ids=set(), model_type="chat")
            with pytest.raises(LookupError):
                get_model_config_by_type_and_name(db, uuid4().hex, "chat", str(item["id"]))
    chosen = items[1]
    assert client.patch("/api/v1/models/default", json={"model_type": "image2text", "model_id": chosen["id"]}).status_code == 200
    with Session(engine) as db:
        assert get_tenant_default_model_by_type(db, owner, "image2text")["api_base"] == "https://second.invalid/v1"
    response = client.get("/api/v1/models/default").json()["data"]["models"]
    assert next(item for item in response if item["model_type"] == "image2text")["model_id"] == chosen["id"]
    assert client.delete("/api/v1/providers/OpenAI").status_code == 409
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(TenantModelInstance)) == 2
    assert client.patch("/api/v1/models/default", json={"model_type": "image2text", "model_id": None}).status_code == 200
    assert client.delete("/api/v1/providers/OpenAI").status_code == 200
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(TenantModelProvider)) == 0
        assert db.scalar(select(func.count()).select_from(TenantModelInstance)) == 0
        assert db.scalar(select(func.count()).select_from(TenantLLM)) == 0


def test_blank_patch_status_and_rejections_have_independent_readback(http: tuple[Any, ...]) -> None:
    client, owner, engine, _module = http
    create(client, models=[])
    assert client.get("/api/v1/models").json()["data"]["items"] == []
    registered = client.post("/api/v1/providers/OpenAI/instances/first/models", json={"model_name": "zero", "model_types": ["ocr", "image2text"], "max_tokens": 0, "is_tools": False})
    assert registered.status_code == 200, registered.text
    identifier = registered.json()["data"]["id"]
    assert client.patch("/api/v1/providers/OpenAI/instances/first", json={"api_key": "", "region": ""}).status_code == 200
    assert client.patch("/api/v1/providers/OpenAI/instances/first", json={"clear_api_key": True}).status_code == 400
    assert client.patch("/api/v1/models/default", json={"model_type": "embedding", "model_id": identifier}).status_code == 400
    for query in ["page=0", "page=1&page=2", "page_size=101", "page_size=1.0", "page_size=nan", "active=1", "model_type=unknown"]:
        assert client.get("/api/v1/models?" + query).status_code == 422
    assert client.patch("/api/v1/providers/OpenAI/instances/first", json={"status": "inactive"}).status_code == 200
    assert client.get("/api/v1/models?active=true").json()["data"]["items"] == []
    with Session(engine) as db:
        instance = db.scalar(select(TenantModelInstance))
        assert instance.api_key == "fixture-shared-key" and instance.region == "" and instance.status == "inactive"
        row = db.get(TenantLLM, identifier)
        assert row.max_tokens == 0
        assert db.get(Tenant, owner).tenant_embd_id is None
        with pytest.raises(LookupError, match="disabled"):
            get_model_config_by_type_and_name(db, owner, "ocr", str(identifier))


def test_atomic_create_failure_and_empty_provider_deletion(http: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _owner, engine, module = http
    assert client.put("/api/v1/providers", json={"provider_name": "OpenAI"}).status_code == 200
    assert client.delete("/api/v1/providers/OpenAI").status_code == 200
    assert client.put("/api/v1/providers", json={"provider_name": "OpenAI"}).status_code == 200

    async def fail(*args: Any, **kwargs: Any) -> None:
        raise SQLAlchemyError("synthetic storage failure containing fixture-key")

    monkeypatch.setattr(module.service, "add_model", fail)
    response = client.post("/api/v1/providers/OpenAI/instances", json={"instance_name": "failed", "api_key": "fixture-key"})
    assert response.status_code == 500 and "fixture-key" not in response.text
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(TenantModelProvider)) == 1
        assert db.scalar(select(func.count()).select_from(TenantModelInstance)) == 0
        assert db.scalar(select(func.count()).select_from(TenantLLM)) == 0


def test_backfill_preserves_legacy_ids_divergent_envelopes_and_zero(owned_engine: Engine) -> None:
    owner = uuid4().hex
    credentials = ['{"api_key":"same","is_tools":false,"provider_order":["A"]}', '{"api_key":"other","is_tools":true,"provider_order":["B"]}']
    with Session(owned_engine) as db:
        db.add(tenant(owner))
        for index, credential in enumerate(credentials):
            db.add(
                TenantLLM(
                    tenant_id=owner,
                    llm_factory="OpenAI",
                    llm_name=f"legacy-{index}",
                    mdl_type="chat",
                    api_key=credential,
                    api_base=f"https://legacy-{index}.invalid",
                    max_tokens=0,
                    status="0" if index else "1",
                )
            )
        db.commit()
        before = [(row.id, row.llm_name, row.api_key, row.api_base, row.max_tokens, row.status) for row in db.scalars(select(TenantLLM).order_by(TenantLLM.id))]
    with owned_engine.begin() as connection:
        backfill(connection)
        backfill(connection)
    with Session(owned_engine) as db:
        after = [(row.id, row.llm_name, row.api_key, row.api_base, row.max_tokens, row.status) for row in db.scalars(select(TenantLLM).order_by(TenantLLM.id))]
        assert before == after
        assert db.scalar(select(func.count()).select_from(TenantModel)) == 2
        assert db.scalar(select(func.count()).select_from(TenantModelInstance)) == 2
        config = get_model_config_by_type_and_name(db, owner, "chat", "legacy-0@OpenAI")
        assert config["id"] == before[0][0] and config["max_tokens"] == 0 and config["is_tools"] is False
        assert json.loads(config["api_key_payload"])["provider_order"] == ["A"]
        with pytest.raises(LookupError, match="disabled"):
            get_model_config_by_type_and_name(db, owner, "chat", "legacy-1@OpenAI")


def test_upgrade_is_additive_idempotent_and_no_destructive_downgrade(owned_engine: Engine) -> None:
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    path = Path(__file__).resolve().parents[2] / "configs/alembic/versions/b8d0f2a4c6e8_add_model_provider_instances.py"
    spec = importlib.util.spec_from_file_location("provider_migration_test", path)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    owner = uuid4().hex
    with Session(owned_engine) as db:
        db.add(tenant(owner))
        db.add(TenantLLM(tenant_id=owner, llm_factory="OpenAI", llm_name="legacy", mdl_type="embedding", api_key="fixture", api_base="", max_tokens=0))
        db.commit()
        identity = db.scalar(select(TenantLLM.id))
    with owned_engine.begin() as connection:
        for table in (TenantModel.__table__, TenantModelInstance.__table__, TenantModelProvider.__table__):
            table.drop(connection)
        connection.execute(text("ALTER TABLE usr_ai.t_ai_tenants DROP COLUMN ocr_id, DROP COLUMN tenant_ocr_id"))
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            migration.upgrade()
            with pytest.raises(RuntimeError, match="export"):
                migration.downgrade()
    with Session(owned_engine) as db:
        assert db.get(TenantLLM, identity).max_tokens == 0
        assert db.scalar(select(TenantModel.id)) == identity
        assert db.get(Tenant, owner).ocr_id is None


def test_long_base_url_strict_paths_and_validation_never_echo_secrets(http: tuple[Any, ...]) -> None:
    client, _owner, engine, _module = http
    create(client, models=[])
    registered = client.post("/api/v1/providers/OpenAI/instances/first/models", json={"model_name": "long-url", "model_types": ["chat"], "max_tokens": 0})
    identifier = registered.json()["data"]["id"]
    long_url = "https://long.invalid/" + "path" * 90
    response = client.patch("/api/v1/providers/OpenAI/instances/first", json={"base_url": long_url})
    assert response.status_code == 200, response.text
    for invalid in ["0", "01", "+1", "1.0", "-1", "9223372036854775808"]:
        for method in (client.patch, client.delete):
            kwargs = {"json": {"status": "inactive"}} if method == client.patch else {}
            assert method(f"/api/v1/providers/OpenAI/instances/first/models/{invalid}", **kwargs).status_code == 400
    secret = "secret-must-never-echo"
    invalid = client.post("/api/v1/providers/OpenAI/instances", json={"instance_name": "bad", "api_key": {"secret": secret}})
    assert invalid.status_code == 422 and secret not in invalid.text
    with Session(engine) as db:
        assert db.get(TenantLLM, identifier).api_base == long_url
        assert db.get(TenantLLM, identifier).status == "1"
        assert db.scalar(select(func.count()).select_from(TenantModelInstance)) == 1


def test_reference_checks_use_model_fields_and_owner_scope(http: tuple[Any, ...]) -> None:
    from api.db.db_models import Search, UserCanvas

    client, owner, engine, _module = http
    create(client)
    identifier = client.get("/api/v1/models").json()["data"]["items"][0]["id"]
    other = uuid4().hex
    with Session(engine) as db:
        db.add(tenant(other))
        db.add(Search(id=uuid4().hex, tenant_id=other, name="catalog-chat@OpenAI", created_by=other, description="catalog-chat@OpenAI", search_config={"chat_id": "catalog-chat@OpenAI"}))
        canvas = UserCanvas(id=uuid4().hex, user_id=owner, title="Agent", dsl={"components": {"llm": {"params": {"llm_id": str(identifier)}}}})
        db.add(canvas)
        db.commit()
        canvas_id = canvas.id
    assert client.delete("/api/v1/providers/OpenAI").status_code == 409
    with Session(engine) as db:
        assert db.get(TenantLLM, identifier) is not None
        canvas = db.get(UserCanvas, canvas_id)
        canvas.dsl = {"components": {"parser": {"params": {"layout_recognize": str(identifier)}}}}
        db.commit()
    assert client.delete("/api/v1/providers/OpenAI").status_code == 409
    with Session(engine) as db:
        canvas = db.get(UserCanvas, canvas_id)
        canvas.dsl = {}
        db.commit()
    with Session(engine) as db:
        search = Search(id=uuid4().hex, tenant_id=owner, name="Model search", created_by=owner, search_config={"chat_id": str(identifier)})
        db.add(search)
        db.commit()
        search_id = search.id
    assert client.delete("/api/v1/providers/OpenAI").status_code == 409
    with Session(engine) as db:
        search = db.get(Search, search_id)
        search.search_config = {"chat_id": "catalog-chat@OpenAI"}
        db.commit()
    assert client.delete("/api/v1/providers/OpenAI").status_code == 409
    with Session(engine) as db:
        db.delete(db.get(Search, search_id))
        db.commit()
    assert client.delete("/api/v1/providers/OpenAI").status_code == 200
    with Session(engine) as db:
        assert db.get(TenantLLM, identifier) is None
        assert db.scalar(select(func.count()).select_from(Search).where(Search.tenant_id == other)) == 1


def test_foreign_owner_denials_have_no_writes(http: tuple[Any, ...]) -> None:
    from api.utils.api_utils import async_current_user

    client, _owner, engine, _module = http
    create(client)
    identifier = client.get("/api/v1/models").json()["data"]["items"][0]["id"]
    other = uuid4().hex
    with Session(engine) as db:
        db.add(tenant(other))
        db.commit()
    client.app.dependency_overrides[async_current_user] = lambda: SimpleNamespace(id=other)
    assert client.get("/api/v1/models").json()["data"]["items"] == []
    assert client.get("/api/v1/providers/OpenAI/instances").status_code == 404
    assert client.patch("/api/v1/providers/OpenAI/instances/first", json={"api_key": "forbidden"}).status_code == 404
    assert client.delete("/api/v1/providers/OpenAI").status_code == 404
    assert client.patch("/api/v1/models/default", json={"model_type": "chat", "model_id": identifier}).status_code == 404
    with Session(engine) as db:
        assert db.scalar(select(TenantModelInstance.api_key)) == "fixture-shared-key"
        assert db.scalar(select(func.count()).select_from(TenantLLM)) == 1
        assert db.get(Tenant, other).tenant_llm_id is None


def test_numeric_name_collision_is_rejected(http: tuple[Any, ...]) -> None:
    client, owner, engine, _module = http
    create(client)
    identifier = client.get("/api/v1/models").json()["data"]["items"][0]["id"]
    with Session(engine) as db:
        db.add(TenantLLM(tenant_id=owner, llm_factory="OpenAI", llm_name=str(identifier), mdl_type="chat", api_key="legacy-other-key", api_base="https://other.invalid"))
        db.commit()
    with Session(engine) as db:
        with pytest.raises(LookupError, match="ambiguous"):
            get_model_config_by_type_and_name(db, owner, "chat", str(identifier))
        assert get_model_config_by_id(db, identifier, tenant_id=owner, model_type="chat")["api_key"] == "fixture-shared-key"


def test_logout_persists_invalidation_and_commit_failure_rolls_back(owned_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from fastapi import HTTPException

    from api.db.db_models import APIToken, User

    monkeypatch.setitem(resources._state, "secret_key", "test-only-logout-key")
    from datetime import UTC, datetime

    from api.apps import load_user, manager
    from api.apps.restful_apis.user_api import log_out
    from api.identity.principal import AuthenticatedActor, AuthenticationContext, AuthenticationSource, IdentityAssurance, TenantMembershipEvidence, build_principal_from_authenticated_actor

    owner = uuid4().hex
    email = f"{owner}@fixture.invalid"
    with Session(owned_engine) as db:
        db.add(User(id=owner, nickname="Logout", email=email, access_token="old-access", password="unused"))
        db.add(tenant(owner))
        db.add(APIToken(tenant_id=owner, token="fixture-independent-api-key", name="separate"))
        db.commit()
    principal = build_principal_from_authenticated_actor(
        actor=AuthenticatedActor(platform_user_id=owner, display_name="Logout"),
        membership=TenantMembershipEvidence(platform_user_id=owner, tenant_id=owner),
        authentication=AuthenticationContext(source=AuthenticationSource.WEB_SESSION, assurance=IdentityAssurance.AUTHENTICATED, validated_at=datetime.now(UTC)),
    )
    jwt = manager.create_access_token(data={"sub": email})

    async def invoke(fail: bool = False) -> None:
        async_engine = create_async_engine(owned_engine.url)
        async with AsyncSession(async_engine, expire_on_commit=False) as db:
            if fail:

                async def fail_commit() -> None:
                    raise SQLAlchemyError("fixture storage error")

                monkeypatch.setattr(db, "commit", fail_commit)
            try:
                await log_out(db, principal)
            finally:
                await db.close()
                await async_engine.dispose()

    with pytest.raises(HTTPException, match="500"):
        asyncio.run(invoke(True))
    with Session(owned_engine) as db:
        assert db.get(User, owner).access_token == "old-access"
        assert load_user(email, db) is not None
    asyncio.run(invoke())
    with Session(owned_engine) as db:
        assert db.get(User, owner).access_token.startswith("INVALID_")
        assert load_user(manager._get_payload(jwt)["sub"], db) is None
        assert db.scalar(select(APIToken.token).where(APIToken.tenant_id == owner)) == "fixture-independent-api-key"


@pytest.mark.parametrize("name", ["0", "1.0", "+1", "01"])
def test_exact_legacy_numeric_looking_aliases_survive(owned_engine: Engine, name: str) -> None:
    owner = uuid4().hex
    with Session(owned_engine) as db:
        db.add(tenant(owner))
        db.add(TenantLLM(tenant_id=owner, llm_factory="OpenAI", llm_name=name, mdl_type="chat", api_key="fixture", api_base=""))
        db.commit()
    with Session(owned_engine) as db:
        assert get_model_config_by_type_and_name(db, owner, "chat", name)["llm_name"] == name


def test_legacy_management_guard_denies_linked_records_before_probing(http: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from fastapi import HTTPException

    from api.apps.llm_app import AddLLMRequest, add_llm

    client, owner, engine, _module = http
    create(client)
    with Session(engine) as db:
        with pytest.raises(HTTPException, match="409"):
            asyncio.run(add_llm(AddLLMRequest(llm_factory="OpenAI", llm_name="catalog-chat", mdl_type="chat", api_key="must-not-probe"), db, SimpleNamespace(id=owner)))
    with Session(engine) as db:
        assert db.scalar(select(TenantModelInstance.api_key)) == "fixture-shared-key"
        assert db.scalar(select(func.count()).select_from(TenantLLM)) == 1


def test_real_model_http_and_worker_rebind_keep_instance_identity(http: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio
    import socket
    import threading
    from contextlib import ExitStack
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from api.db.joint_services.tenant_model_service import rebind_model_config
    from api.db.services import tenant_llm_service
    from api.db.services.llm_service import LLMBundle

    client, owner, engine, _module = http
    journal = ResourceJournal("model-http-worker-rebind")
    requests_seen: list[tuple[str, str, str]] = []

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            return

        def do_POST(self) -> None:
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests_seen.append((self.path, self.headers["Authorization"], payload["model"]))
            content = json.dumps(
                {
                    "id": "chatcmpl-fixture",
                    "object": "chat.completion",
                    "created": 1,
                    "model": payload["model"],
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "fixture answer"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

    with ExitStack() as cleanup:
        port = 30000 + int(uuid4().hex[:4], 16) % 20000
        address = ("127.0.0.1", port)
        journal.event("reserve-listener", address=list(address))
        server = ThreadingHTTPServer(address, Provider, bind_and_activate=False)
        cleanup.callback(server.server_close)
        server.server_bind()
        server.server_activate()
        thread = threading.Thread(target=server.serve_forever, name="provider-fixture")
        thread.start()
        cleanup.callback(thread.join, 5)
        cleanup.callback(server.shutdown)
        for name in ("first", "second"):
            create(client, name, models=[])
            assert client.patch(f"/api/v1/providers/OpenAI/instances/{name}", json={"api_key": "fixture-" + name, "base_url": f"http://127.0.0.1:{port}/{name}/v1"}).status_code == 200
            assert client.post(f"/api/v1/providers/OpenAI/instances/{name}/models", json={"model_name": "same-raw-name", "model_types": ["chat"], "max_tokens": 8192}).status_code == 200
        identifiers = [item["id"] for item in client.get("/api/v1/models").json()["data"]["items"]]

        async def infer() -> None:
            import aiohttp
            import httpx
            import litellm
            from litellm.caching.llm_caching_handler import LLMClientCache
            from litellm.litellm_core_utils.logging_worker import GLOBAL_LOGGING_WORKER

            assert GLOBAL_LOGGING_WORKER._worker_task is None
            owned_cache = LLMClientCache()
            monkeypatch.setattr(litellm, "in_memory_llm_clients_cache", owned_cache)
            async_engine = create_async_engine(engine.url)
            event.listen(async_engine.sync_engine, "before_cursor_execute", engine._provider_reserve)
            monkeypatch.setattr(tenant_llm_service, "async_session_factory", async_sessionmaker(async_engine, expire_on_commit=False))
            try:
                for identifier in identifiers:
                    with Session(engine) as db:
                        config = get_model_config_by_id(db, identifier, tenant_id=owner, model_type="chat")
                        original = LLMBundle(db, owner, config, max_retries=1)
                        original.db = None
                        rebuilt = rebind_model_config(db, original, "chat")
                        assert rebuilt["id"] == identifier and rebuilt["api_base"] == config["api_base"]
                        bundle = LLMBundle(db, owner, rebuilt, max_retries=1)
                        bundle.db = None
                    try:
                        assert await bundle.async_chat("", [{"role": "user", "content": "hello"}], {"temperature": 0}) == "fixture answer"
                    finally:
                        await bundle.aclose()
                        await original.aclose()
            finally:
                await GLOBAL_LOGGING_WORKER.stop()
                await GLOBAL_LOGGING_WORKER.clear_queue()
                await litellm.close_litellm_async_clients()
                seen: set[int] = set()
                sessions: list[Any] = []

                def collect(value: Any) -> None:
                    if id(value) in seen:
                        return
                    seen.add(id(value))
                    if isinstance(value, (aiohttp.ClientSession, httpx.AsyncClient)):
                        sessions.append(value)
                    fields = vars(value) if hasattr(value, "__dict__") else {}
                    for key in ("client", "_client", "client_session", "_transport", "transport"):
                        if key in fields:
                            collect(fields[key])

                for handler in owned_cache.cache_dict.values():
                    collect(handler)
                collect(getattr(litellm, "base_llm_aiohttp_handler", None))
                # This test owns the isolated SDK cache. The installed SDK's
                # cleanup hook misses some wrapped transports, so close the
                # exact remaining sessions and independently read their state.
                for value in sessions:
                    if isinstance(value, aiohttp.ClientSession):
                        if not value.closed:
                            await value.close()
                    elif not value.is_closed:
                        await value.aclose()
                assert all(value.closed if isinstance(value, aiohttp.ClientSession) else value.is_closed for value in sessions)
                assert GLOBAL_LOGGING_WORKER._worker_task is None and not GLOBAL_LOGGING_WORKER._running_tasks
                assert GLOBAL_LOGGING_WORKER._queue is None or GLOBAL_LOGGING_WORKER._queue.empty()
                journal.event("sdk-clients-cleaned", sessions=len(sessions), closed=True, logging_tasks_stopped=True)
                await async_engine.dispose()

        asyncio.run(infer())
        assert requests_seen == [("/first/v1/chat/completions", "Bearer fixture-first", "same-raw-name"), ("/second/v1/chat/completions", "Bearer fixture-second", "same-raw-name")]
        with Session(engine) as db:
            assert [db.get(TenantLLM, identifier).used_tokens for identifier in identifiers] == [3, 3]
    with socket.socket() as probe:
        assert probe.connect_ex(address) != 0
    assert not thread.is_alive()
    journal.event("listener-cleaned", address=list(address), absent=True, thread_stopped=True)


def test_readonly_legacy_projection_includes_disabled_models_without_opaque_keys(http: tuple[Any, ...]) -> None:
    from api.apps.llm_app import my_llms

    client, owner, engine, _module = http
    create(client)
    with Session(engine) as db:
        row = db.scalar(select(TenantLLM))
        row.status = "0"
        db.add(TenantLLM(tenant_id=owner, llm_factory="OpenAI", llm_name="legacy", mdl_type="chat", api_key="fixture-legacy", api_base="", max_tokens=0, status="0"))
        db.commit()
    with Session(engine) as db:
        for details in (False, True):
            data = json.loads(my_llms(details, db, SimpleNamespace(id=owner)).body)
            assert data["retcode"] == 0
            assert {item["name"] for item in data["data"]["OpenAI"]["llm"]} == {"catalog-chat@first", "legacy"}
            assert {item["status"] for item in data["data"]["OpenAI"]["llm"]} == {"0"}
            assert "model:" not in json.dumps(data) and "fixture-legacy" not in json.dumps(data) and "fixture-shared-key" not in json.dumps(data)
            if details:
                assert next(item for item in data["data"]["OpenAI"]["llm"] if item["name"] == "legacy")["max_tokens"] == 0


def test_default_ocr_and_parser_numeric_reference_preserve_model_tool_override(http: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch) -> None:
    from api.db.joint_services.tenant_model_service import resolve_parser_reference

    client, owner, engine, _module = http
    monkeypatch.setattr(settings, "FACTORY_LLM_INFOS", [{"name": "PaddleOCR", "llm": []}])
    assert client.put("/api/v1/providers", json={"provider_name": "PaddleOCR"}).status_code == 200
    key = json.dumps({"api_key": "fixture-ocr", "is_tools": True, "PADDLEOCR_URL": "https://ocr.invalid", "PADDLEOCR_API_TOKEN": "fixture-token"})
    response = client.post(
        "/api/v1/providers/PaddleOCR/instances",
        json={"instance_name": "local", "api_key": key, "model_ids": [], "custom_model": {"model_name": "ocr-model", "model_types": ["ocr"], "max_tokens": 0, "is_tools": False}},
    )
    assert response.status_code == 200, response.text
    model = client.get("/api/v1/providers/PaddleOCR/instances/local/models").json()["data"]["items"][0]
    assert client.patch("/api/v1/models/default", json={"model_type": "ocr", "model_id": model["id"]}).status_code == 200
    with Session(engine) as db:
        config = get_tenant_default_model_by_type(db, owner, "ocr")
        assert config["mdl_type"] == "ocr" and config["max_tokens"] == 0 and config["is_tools"] is False
        assert json.loads(config["api_key_payload"])["PADDLEOCR_API_TOKEN"] == "fixture-token"
        assert resolve_parser_reference(db, owner, str(model["id"])) == ("PaddleOCR", str(model["id"]))
    assert client.delete("/api/v1/providers/PaddleOCR").status_code == 409


def test_workflow_embedding_consumer_selects_owner_and_capability(http: tuple[Any, ...]) -> None:
    from workflow_v2.component.knowledge_base_search_component import KnowledgeBaseSearchComponent

    client, owner, engine, _module = http
    create(client, models=[])
    response = client.post(
        "/api/v1/providers/OpenAI/instances/first/models",
        json={
            "model_name": "shared-embedding",
            "model_types": ["rerank", "embedding"],
            "max_tokens": 0,
            "is_tools": False,
        },
    )
    assert response.status_code == 200, response.text
    identifier = response.json()["data"]["id"]
    with Session(engine) as db:
        component = object.__new__(KnowledgeBaseSearchComponent)
        component.db = db
        kb = SimpleNamespace(tenant_id=owner, tenant_embd_id=identifier, embd_id="ignored-legacy-name")
        bundle = component._build_embedding_model([kb])
        assert bundle.llm_type == "embedding" and bundle.llm_name == "shared-embedding"
        assert bundle.tenant_id == owner
        foreign = SimpleNamespace(tenant_id=uuid4().hex, tenant_embd_id=identifier, embd_id="ignored")
        with pytest.raises(LookupError, match="not found"):
            component._build_embedding_model([foreign])


def test_async_construction_detaches_before_callback_returns(http: tuple[Any, ...]) -> None:
    import asyncio

    from api.db.joint_services.tenant_model_service import detached_bundle

    client, owner, engine, _module = http
    create(client)
    identifier = client.get("/api/v1/models").json()["data"]["items"][0]["id"]

    async def check() -> None:
        async_engine = create_async_engine(engine.url)
        try:
            async with AsyncSession(async_engine) as db:

                def construct(session: Session) -> Any:
                    config = get_model_config_by_id(session, identifier, tenant_id=owner, model_type="chat")
                    bundle = detached_bundle(session, owner, config)
                    assert bundle.db is None
                    return bundle

                bundle = await db.run_sync(construct)
                assert bundle.db is None
                assert await db.scalar(select(Tenant.id).where(Tenant.id == owner)) == owner
        finally:
            await async_engine.dispose()

    asyncio.run(check())


def test_backfill_parameter_binding_quotes_injection_and_idempotency(owned_engine: Engine) -> None:
    owner = "tenant-'quoted"
    factory = "provider'); DROP TABLE usr_ai.t_ai_tenant_models; --"
    credential = "private-'credential"
    with Session(owned_engine) as db:
        db.add(tenant(owner))
        db.add(TenantLLM(tenant_id=owner, llm_factory=factory, llm_name="model-'quoted", mdl_type="chat", api_key=credential, api_base="https://fixture.invalid/'path", max_tokens=0))
        db.commit()
    with owned_engine.begin() as connection:
        backfill(connection, owner)
        backfill(connection, owner)
    with Session(owned_engine) as db:
        assert db.scalar(select(func.count()).select_from(TenantModel)) == 1
        provider = db.scalars(select(TenantModelProvider)).one()
        assert provider.tenant_id == owner and provider.provider_name == factory
        instance = db.scalars(select(TenantModelInstance)).one()
        assert instance.api_key == credential and instance.base_url == "https://fixture.invalid/'path"
        assert db.scalars(select(TenantModel)).one().model_name == "model-'quoted"


def test_backfill_failure_rolls_back_provider_instance_and_model_writes(owned_engine: Engine) -> None:
    owner = uuid4().hex
    with Session(owned_engine) as db:
        db.add(tenant(owner))
        db.add(TenantLLM(tenant_id=owner, llm_factory="OpenAI", llm_name="valid-first", mdl_type="chat", api_key="private-fixture"))
        db.add(TenantLLM(tenant_id=owner, llm_factory="OpenAI", llm_name="invalid-capability", mdl_type="invalid", api_key="private-fixture"))
        db.commit()
    with pytest.raises(RuntimeError, match="explicit repair"), owned_engine.begin() as connection:
        backfill(connection, owner)
    # A new session observes the failed transaction independently.
    with Session(owned_engine) as db:
        assert db.scalar(select(func.count()).select_from(TenantLLM)) == 2
        assert db.scalar(select(func.count()).select_from(TenantModelProvider)) == 0
        assert db.scalar(select(func.count()).select_from(TenantModelInstance)) == 0
        assert db.scalar(select(func.count()).select_from(TenantModel)) == 0


@pytest.mark.parametrize("provider_name,base_path", [("OpenAI", "/v1"), ("BaiduYiyan", "/v2"), ("Tongyi-Qianwen", "/compatible-mode/v1"), ("VLLM", "/mount"), ("VLLM", "/mount/v1/")])
def test_provider_connection_and_atomic_create_use_real_adapter_and_zero_write_failures(
    http: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, provider_name: str, base_path: str
) -> None:
    import socket
    import threading
    from contextlib import ExitStack
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    client, owner, engine, _module = http
    monkeypatch.setattr(settings, "FACTORY_LLM_INFOS", [{"name": provider_name, "llm": []}])
    monkeypatch.setattr(provider_verification, "verify_model", REAL_VERIFY_MODEL)
    journal = ResourceJournal("provider-connection-atomic-http")
    requests_seen: list[str] = []
    state = {"failure": False, "malformed": False}

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            return None

        def do_POST(self) -> None:
            assert self.path == (base_path.rstrip("/") + ("/v1" if provider_name == "VLLM" and not base_path.rstrip("/").endswith("/v1") else "") + "/chat/completions")
            assert self.headers["Authorization"] == "Bearer probe-SECRET-key"
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests_seen.append(payload["model"])
            if state["failure"]:
                status, body = 401, {"error": {"message": "probe-SECRET-key", "type": "authentication_error"}}
            elif state["malformed"]:
                status, body = 200, {"choices": []}
            else:
                status, body = (
                    200,
                    {
                        "id": "chatcmpl-probe",
                        "object": "chat.completion",
                        "created": 1,
                        "model": payload["model"],
                        "choices": [{"index": 0, "message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                    },
                )
            encoded = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    def counts() -> tuple[int, ...]:
        with Session(engine) as db:
            return tuple(db.scalar(select(func.count()).select_from(model)) for model in (TenantModelProvider, TenantModelInstance, TenantModel, TenantLLM))

    with ExitStack() as cleanup:
        address = ("127.0.0.1", 30000 + int(uuid4().hex[:4], 16) % 20000)
        journal.event("reserve-listener", address=list(address))
        server = ThreadingHTTPServer(address, Provider, bind_and_activate=False)
        cleanup.callback(server.server_close)
        server.server_bind()
        server.server_activate()
        thread = threading.Thread(target=server.serve_forever, name="owned-provider-probe")
        thread.start()
        cleanup.callback(thread.join, 5)
        cleanup.callback(server.shutdown)
        candidate = {
            "api_key": "probe-SECRET-key",
            "base_url": f"http://127.0.0.1:{address[1]}{base_path}",
            "region": "",
            "model_ids": [],
            "custom_model": {"model_name": "custom-zero", "model_types": ["chat", "image2text"], "max_tokens": 0, "is_tools": False},
        }
        endpoint = f"/api/v1/providers/{provider_name}"
        assert counts() == (0, 0, 0, 0)
        verified = client.post(endpoint + "/connection", json=candidate)
        assert verified.status_code == 200, verified.text
        assert verified.json()["data"] == {"verified": True, "model_name": "custom-zero", "model_type": "chat"}
        assert counts() == (0, 0, 0, 0)
        state["failure"] = True
        rejected = client.post(endpoint + "/instances", json={"instance_name": "first", **candidate})
        assert rejected.status_code == 400 and "probe-SECRET-key" not in rejected.text
        assert counts() == (0, 0, 0, 0)
        state["failure"], state["malformed"] = False, True
        assert client.post(endpoint + "/instances", json={"instance_name": "first", **candidate}).status_code == 400
        assert counts() == (0, 0, 0, 0)
        state["malformed"] = False
        for name in ("first", "second"):
            saved = client.post(endpoint + "/instances", json={"instance_name": name, **candidate})
            assert saved.status_code == 200, saved.text
            assert "probe-SECRET-key" not in saved.text
        assert counts() == (1, 2, 2, 2)
        assert requests_seen == ["custom-zero"] * 5  # Verify + two failed creates + two actual revalidations.
        before = len(requests_seen)
        assert client.post(endpoint + "/instances", json={"instance_name": "first", **candidate}).status_code == 409
        assert client.post(endpoint + "/instances", json={"instance_name": "empty", **candidate, "custom_model": None}).status_code == 400
        assert len(requests_seen) == before and counts() == (1, 2, 2, 2)
        with Session(engine) as db:
            rows = db.scalars(select(TenantModelInstance)).all()
            assert all(row.api_key == "probe-SECRET-key" for row in rows)
            assert all(row.max_tokens == 0 for row in db.scalars(select(TenantLLM)))
            assert all(row.model_types == ["chat", "image2text"] for row in db.scalars(select(TenantModel)))
        assert "probe-SECRET-key" not in caplog.text
        assert client.delete(endpoint).status_code == 200
        assert counts() == (0, 0, 0, 0)
        journal.event("sql-independent-readback", tenant=owner, counts=list(counts()), provider_calls=len(requests_seen))
    with socket.socket() as reader:
        assert reader.connect_ex(address) != 0
    assert not thread.is_alive()
    journal.event("listener-independent-readback", address=list(address), absent=True, thread_absent=True)


def test_region_catalog_effective_status_order_and_legacy_key_readback(http: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch) -> None:
    from api.db.joint_services.tenant_model_service import resolve_model_type

    client, owner, engine, _module = http
    factories = [
        {"name": "SILICONFLOW", "rank": "950", "llm": [{"llm_name": "CN_only", "mdl_type": "embedding", "max_tokens": 0}]},
        {"name": "siliconflow_intl", "llm": [{"llm_name": "intl_only", "mdl_type": "chat", "max_tokens": 0}]},
        {"name": "LocalProvider", "llm": []},
        {"name": "OpenAI", "rank": "999", "llm": [{"llm_name": "chat", "mdl_type": "chat", "max_tokens": 0}]},
    ]
    monkeypatch.setattr(settings, "FACTORY_LLM_INFOS", factories)
    candidate = {"api_key": "shared", "region": "intl", "model_ids": ["intl_only"]}
    for name in ("z_last", "a_first"):
        response = client.post("/api/v1/providers/SILICONFLOW/instances", json={"instance_name": name, **candidate})
        assert response.status_code == 200, response.text
    assert client.post("/api/v1/providers/OpenAI/instances", json={"instance_name": "other", "api_key": "shared"}).status_code == 200
    assert client.put("/api/v1/providers", json={"provider_name": "LocalProvider"}).status_code == 200
    providers = client.get("/api/v1/providers").json()["data"]["items"]
    assert [row["name"] for row in providers] == ["OpenAI", "SILICONFLOW", "LocalProvider"]
    assert "siliconflow_intl" not in {row["name"] for row in client.get("/api/v1/providers?available=true").json()["data"]["items"]}
    supported = client.get("/api/v1/providers/SILICONFLOW/instances/a_first/models?supported=true").json()["data"]["items"]
    assert [row["model_name"] for row in supported] == ["intl_only"]
    assert client.get("/api/v1/providers/SILICONFLOW/models?region=intl").json()["data"]["items"][0]["model_name"] == "intl_only"
    items = client.get("/api/v1/models").json()["data"]["items"]
    assert [(row["provider_name"], row["instance_name"]) for row in items] == [("OpenAI", "other"), ("SILICONFLOW", "a_first"), ("SILICONFLOW", "z_last")]
    identifier = items[1]["id"]
    assert client.patch("/api/v1/providers/SILICONFLOW/instances/a_first", json={"status": "inactive"}).status_code == 200
    with Session(engine) as db:
        assert db.get(TenantLLM, identifier).status == "1"
        with pytest.raises(LookupError):
            resolve_model_type(db, owner, str(identifier))
        with pytest.raises(LookupError):
            resolve_model_type(db, uuid4().hex, str(items[0]["id"]))
        other = db.scalar(select(TenantModelInstance).where(TenantModelInstance.instance_name == "z_last"))
        assert other is not None
        other.api_key = json.dumps({"api_key": "historical-key", "source_fid": "siliconflow_intl"})
        other.region = ""
        db.commit()
    with Session(engine) as db:
        config = get_model_config_by_id(db, items[2]["id"], tenant_id=owner, model_type="chat")
        assert config["api_key"] == "historical-key" and config["region"] == "intl" and config["api_base"] == "https://api.siliconflow.com/v1"
    item = next(row for row in client.get("/api/v1/models").json()["data"]["items"] if row["id"] == identifier)
    assert item["status"] == "active" and item["effective_status"] == "inactive"


def test_provider_inventory_projects_owned_capabilities_without_credentials(http: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch) -> None:
    client, owner, engine, module = http
    create(client)
    registered = client.post("/api/v1/providers/OpenAI/instances/first/models", json={"model_name": "custom-ocr", "model_types": ["ocr"], "max_tokens": 0})
    assert registered.status_code == 200
    assert client.patch("/api/v1/providers/OpenAI/instances/first", json={"status": "inactive"}).status_code == 200
    foreign = uuid4().hex
    with Session(engine) as db:
        db.add(tenant(foreign))
        provider = TenantModelProvider(id=uuid4().hex, tenant_id=foreign, provider_name="OpenAI", status="active")
        db.add(provider)
        db.flush()
        instance = TenantModelInstance(id=uuid4().hex, provider_id=provider.id, instance_name="foreign", api_key="private-foreign-key", base_url="", region="", status="active")
        db.add(instance)
        row = TenantLLM(tenant_id=foreign, llm_factory="OpenAI", llm_name="foreign-only", mdl_type="rerank", api_key="private-foreign-key", max_tokens=0, status="1")
        db.add(row)
        db.flush()
        db.add(TenantModel(id=row.id, instance_id=instance.id, model_name="foreign-only", model_types=["rerank"], is_tools=False))
        db.commit()
    # A provider can disappear from the static catalog while its registered
    # tenant inventory remains visible, including disabled custom capabilities.
    monkeypatch.setattr(settings, "FACTORY_LLM_INFOS", [])
    statements: list[str] = []

    async def projected(db: AsyncSession, tenant_id: str) -> dict[str, set[str]]:
        def capture(_conn: Any, _cursor: Any, statement: str, _params: Any, _context: Any, _many: bool) -> None:
            statements.append(statement)

        connection = await db.connection()
        event.listen(connection.sync_connection, "before_cursor_execute", capture)
        try:
            return await original(db, tenant_id)
        finally:
            event.remove(connection.sync_connection, "before_cursor_execute", capture)

    original = module.service.provider_capabilities_for_owner
    monkeypatch.setattr(module.service, "provider_capabilities_for_owner", projected)
    result = client.get("/api/v1/providers")
    assert result.status_code == 200, result.text
    assert result.json()["data"]["items"][0]["model_types"] == ["chat", "image2text", "ocr"]
    assert len(statements) == 1
    projection = statements[0].split("FROM", 1)[0].lower()
    assert "provider_name" in projection and "model_types" in projection
    assert "api_key" not in projection and "base_url" not in projection
    with Session(engine) as db:
        assert db.scalar(select(TenantModelInstance).where(TenantModelInstance.instance_name == "first")).status == "inactive"
        assert db.scalar(select(func.count()).select_from(TenantModelProvider).where(TenantModelProvider.tenant_id != owner)) == 1
        assert db.scalar(select(TenantLLM).where(TenantLLM.tenant_id == foreign)).api_key == "private-foreign-key"


def test_backfill_tools_wrappers_mixed_imports_preserve_ids_and_defaults(owned_engine: Engine) -> None:
    """Dedup new credentials without moving any already linked model."""
    from api.db.model_backfill import backfill_tenant_defaults

    owner, other = uuid4().hex, uuid4().hex
    with Session(owned_engine) as db:
        db.add_all([tenant(owner), tenant(other)])
        first = TenantLLM(tenant_id=owner, llm_factory="OpenAI", llm_name="original", mdl_type="chat", api_key='{"api_key":"fixture","is_tools":true}', api_base="https://one.invalid/v1", max_tokens=0)
        db.add(first)
        db.commit()
        original_id = first.id
    with owned_engine.begin() as connection:
        backfill(connection)
    with Session(owned_engine) as db:
        original_instance = db.get(TenantModel, original_id).instance_id
        db.get(Tenant, owner).tenant_llm_id = original_id
        inputs = [
            (owner, "OpenAI", "raw", "fixture", "https://one.invalid/v1"),
            (owner, "OpenAI", "false", '{"is_tools":false,"api_key":"fixture"}', "https://one.invalid/v1"),
            (owner, "OpenAI", "base", "fixture", "https://two.invalid/v1"),
            (other, "OpenAI", "tenant", "fixture", "https://one.invalid/v1"),
            (owner, "OpenRouter", "factory", "fixture", "https://one.invalid/v1"),
            (owner, "OpenAI", "order-a", '{"api_key":"fixture","provider_order":["A"],"is_tools":true}', "https://one.invalid/v1"),
            (owner, "OpenAI", "order-a-false", '{"provider_order":["A"],"is_tools":false,"api_key":"fixture"}', "https://one.invalid/v1"),
            (owner, "OpenAI", "order-b", '{"api_key":"fixture","provider_order":["B"]}', "https://one.invalid/v1"),
            (owner, "OpenAI", "region", '{"api_key":"fixture","region":"intl"}', "https://one.invalid/v1"),
            (owner, "OpenAI", "auth", '{"api_key":"fixture","auth_mode":"role"}', "https://one.invalid/v1"),
        ]
        for tenant_id, factory, name, key, base in inputs:
            db.add(TenantLLM(tenant_id=tenant_id, llm_factory=factory, llm_name=name, mdl_type="chat", api_key=key, api_base=base, max_tokens=0))
        db.commit()
        before = [(row.id, row.tenant_id, row.llm_name, row.api_key, row.api_base) for row in db.scalars(select(TenantLLM).order_by(TenantLLM.id))]
    with owned_engine.begin() as connection:
        backfill(connection)
        backfill(connection)
    with Session(owned_engine) as reader:
        assert [(row.id, row.tenant_id, row.llm_name, row.api_key, row.api_base) for row in reader.scalars(select(TenantLLM).order_by(TenantLLM.id))] == before
        models = {row.model_name: row for row in reader.scalars(select(TenantModel))}
        assert len(models) == 11 and {row.id for row in models.values()} == {row[0] for row in before}
        assert {models[name].instance_id for name in ("original", "raw", "false")} == {original_instance}
        assert models["original"].is_tools is True and models["false"].is_tools is False
        assert get_model_config_by_id(reader, models["false"].id, tenant_id=owner, model_type="chat")["is_tools"] is False
        assert models["order-a"].instance_id == models["order-a-false"].instance_id
        assert models["order-a"].is_tools is True and models["order-a-false"].is_tools is False
        assert len({models[name].instance_id for name in ("original", "base", "tenant", "factory", "order-a", "order-b", "region", "auth")}) == 8
        assert reader.get(Tenant, owner).tenant_llm_id == original_id
        assert reader.get(TenantModelInstance, original_instance).api_key == '{"api_key":"fixture","is_tools":true}'
        assert backfill_tenant_defaults(reader) == 0
        reader.commit()
    with Session(owned_engine) as reader:
        assert reader.get(Tenant, owner).tenant_llm_id == original_id
        assert reader.scalar(select(func.count()).select_from(TenantModelInstance)) == 8


@pytest.mark.external_consumer
@pytest.mark.parametrize("case", ["cn", "intl", "explicit", "mineru", "mixed"])
def test_after298_fixed_web_regions_and_ocr(http: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch, tmp_path: Path, case: str) -> None:
    """Real Web API client → JWT → Python → controlled HTTP → independent SQL."""
    import errno
    import io
    import os
    import socket
    import tempfile
    import threading
    import zipfile
    from contextlib import ExitStack
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    import httpx

    from tests.support.model_discovery_web import run_web_discovery

    client, owner, engine, _module = http
    checkout = Path(os.environ["WEB_COMPLETION_CHECKOUT"])
    provider_name = "MinerU" if case == "mineru" else "Tongyi-Qianwen"
    monkeypatch.setattr(
        settings, "FACTORY_LLM_INFOS", [{"name": provider_name, "llm": [] if case == "mineru" else [{"llm_name": "controlled-qwen", "mdl_type": "chat", "max_tokens": 0, "is_tools": False}]}]
    )
    journal = ResourceJournal("after298-web-" + case)
    calls: list[dict[str, Any]] = []

    from api.db.services import model_provider_service

    original_verify_candidate = model_provider_service.verify_candidate

    def snapshot_counts() -> list[int]:
        with Session(engine) as reader:
            return [reader.scalar(select(func.count()).select_from(model)) for model in (TenantModelProvider, TenantModelInstance, TenantModel, TenantLLM)]

    async def measured_verify(db: AsyncSession, tenant_id: str, provider: str, request: ConnectionCandidate) -> dict[str, Any]:
        before = snapshot_counts()
        try:
            return await original_verify_candidate(db, tenant_id, provider, request)
        finally:
            after = snapshot_counts()
            assert before == after
            journal.event("verify-independent-sql-readback", before=before, after=after, unchanged=True)

    monkeypatch.setattr(model_provider_service, "verify_candidate", measured_verify)

    async def owned_probe(provider: str, key: str, base: str, model: ModelRegistration) -> dict[str, Any]:
        from litellm.litellm_core_utils.logging_worker import GLOBAL_LOGGING_WORKER

        journal.event("reserve-sdk-logging-worker", model=model.model_name)
        try:
            return await REAL_VERIFY_MODEL(provider, key, base, model)
        finally:
            await GLOBAL_LOGGING_WORKER.stop()
            await GLOBAL_LOGGING_WORKER.clear_queue()
            assert GLOBAL_LOGGING_WORKER._worker_task is None and not GLOBAL_LOGGING_WORKER._running_tasks
            journal.event("sdk-logging-worker-cleanup", stopped=True, drained=True)

    monkeypatch.setattr(provider_verification, "verify_model", owned_probe)
    original_mkdtemp = tempfile.mkdtemp

    def reserve_parser_temp(suffix: str | None = None, prefix: str | None = None, dir: str | None = None) -> str:
        # Name is fixed before mkdir, rather than recording a random path later.
        path = Path(dir or tempfile.gettempdir()) / ((prefix or "tmp") + uuid4().hex + (suffix or ""))
        journal.event("reserve-parser-directory", path=str(path))
        path.mkdir(mode=0o700)
        return str(path)

    monkeypatch.setattr(tempfile, "mkdtemp", reserve_parser_temp)

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *_args: Any) -> None:
            return

        def reply(self, status: int, content: bytes, mime: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def do_HEAD(self) -> None:
            assert self.path == "/openapi.json"
            calls.append({"path": self.path, "method": "HEAD"})
            self.send_response(200)
            self.end_headers()

        def do_GET(self) -> None:
            assert self.path == "/openapi.json"
            calls.append({"path": self.path})
            self.reply(200, b"{}", "application/json")

        def do_POST(self) -> None:
            payload = self.rfile.read(int(self.headers["Content-Length"]))
            if self.path == "/file_parse":
                assert b"%PDF" in payload and b"connection.pdf" in payload
                calls.append({"path": self.path, "pdf": True})
                data = io.BytesIO()
                with zipfile.ZipFile(data, "w") as archive:
                    archive.writestr("connection/connection_content_list.json", json.dumps([{"type": "text", "text": "Connection test", "page_idx": 0, "bbox": [0, 0, 100, 40]}]))
                self.reply(200, data.getvalue(), "application/zip")
                return
            body = json.loads(payload)
            assert self.path.endswith("/chat/completions") and self.headers["Authorization"] == "Bearer fixture-after298"
            calls.append({"path": self.path, "model": body["model"]})
            if body["model"] == "controlled-fail":
                self.reply(401, b'{"error":{"message":"controlled rejection","type":"authentication_error"}}', "application/json")
            else:
                self.reply(
                    200,
                    json.dumps(
                        {
                            "id": "controlled",
                            "object": "chat.completion",
                            "created": 1,
                            "model": body["model"],
                            "choices": [{"index": 0, "message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}],
                            "usage": {"total_tokens": 0, "prompt_tokens": 0, "completion_tokens": 0},
                        }
                    ).encode(),
                    "application/json",
                )

    with ExitStack() as cleanup:
        for attempt in range(32):
            address = ("127.0.0.1", 30000 + int(uuid4().hex[:4], 16) % 20000)
            journal.event("reserve-listener", address=list(address), attempt=attempt + 1)
            server = ThreadingHTTPServer(address, Provider, bind_and_activate=False)
            cleanup.callback(server.server_close)
            try:
                server.server_bind()
            except OSError as error:
                server.server_close()
                journal.event("listener-bind-failed", address=list(address), attempt=attempt + 1, errno=error.errno, socket_closed=server.socket.fileno() == -1)
                if error.errno != errno.EADDRINUSE or attempt == 31:
                    raise
            else:
                break
        server.server_activate()
        thread = threading.Thread(target=server.serve_forever, name="after298-provider-" + case)
        journal.event("reserve-thread", name=thread.name, listener=list(address))
        thread.start()

        def stop() -> None:
            server.shutdown()
            thread.join(5)
            server.server_close()
            with socket.socket() as reader:
                absent = reader.connect_ex(address) != 0
            assert absent and not thread.is_alive()
            journal.event("provider-listener-readback", absent=True, thread_absent=True, calls=calls)

        cleanup.callback(stop)
        origin = f"http://{address[0]}:{address[1]}"
        original_send = httpx.AsyncClient.send

        async def redirect(client: httpx.AsyncClient, request: httpx.Request, *args: Any, **kwargs: Any) -> httpx.Response:
            if request.url.host in {"dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com"}:
                expected_host = "dashscope-intl.aliyuncs.com" if case in {"intl", "mixed"} else "dashscope.aliyuncs.com"
                assert request.url.host == expected_host
                journal.event("controlled-official-request", host=request.url.host, path=request.url.path)
                request.url = request.url.copy_with(scheme="http", host=address[0], port=address[1])
            return await original_send(client, request, *args, **kwargs)

        monkeypatch.setattr(httpx.AsyncClient, "send", redirect)
        key = json.dumps({"api_key": {"mineru_apiserver": origin, "mineru_delete_output": 1}}) if case == "mineru" else "fixture-after298"
        base = origin + "/custom/v1" if case == "explicit" else ""
        region = "intl" if case in {"intl", "explicit", "mixed"} else "cn" if case == "cn" else ""
        expected = {"MinerU" if case == "mineru" else "controlled-qwen": {"model_types": ["ocr" if case == "mineru" else "chat"], "max_tokens": 0, "is_tools": False}}
        if case == "mixed":
            expected["manual-qwen"] = {"model_types": ["chat"], "max_tokens": 0, "is_tools": False}
        run_web_discovery(
            client,
            owner,
            engine,
            base,
            journal,
            checkout,
            tmp_path,
            provider_name=provider_name,
            api_key=key,
            expected_models=expected,
            invalid_base_url="",
            region=region,
            descriptor_extra={"after298Case": case, "rejectedModelName": "controlled-fail"},
        )
        assert calls
        if case == "mineru":
            assert sum(call["path"] == "/file_parse" for call in calls) >= 2
        else:
            assert sum(call.get("model") == "controlled-qwen" for call in calls) >= 2
        monkeypatch.setattr(tempfile, "mkdtemp", original_mkdtemp)


def test_backfill_new_import_does_not_rebind_edited_instance(owned_engine: Engine) -> None:
    owner = uuid4().hex
    with Session(owned_engine) as db:
        db.add(tenant(owner))
        first = TenantLLM(tenant_id=owner, llm_factory="Tongyi-Qianwen", llm_name="first", mdl_type="chat", api_key="fixture", api_base="", max_tokens=0)
        db.add(first)
        db.commit()
        first_id = first.id
    with owned_engine.begin() as connection:
        backfill(connection)
    with Session(owned_engine) as db:
        original_instance = db.get(TenantModel, first_id).instance_id
        db.get(TenantModelInstance, original_instance).region = "intl"
        second = TenantLLM(tenant_id=owner, llm_factory="Tongyi-Qianwen", llm_name="second", mdl_type="chat", api_key='{"api_key":"fixture","is_tools":false}', api_base="", max_tokens=0)
        db.add(second)
        db.commit()
        second_id = second.id
    with owned_engine.begin() as connection:
        backfill(connection)
        backfill(connection)
    with Session(owned_engine) as reader:
        assert reader.get(TenantModel, first_id).instance_id == original_instance
        assert reader.get(TenantModelInstance, original_instance).region == "intl"
        new_instance = reader.get(TenantModelInstance, reader.get(TenantModel, second_id).instance_id)
        assert new_instance.id != original_instance and new_instance.region == ""
        assert get_model_config_by_id(reader, first_id, tenant_id=owner, model_type="chat")["api_base"] == "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
        assert get_model_config_by_id(reader, second_id, tenant_id=owner, model_type="chat")["api_base"] == "https://dashscope.aliyuncs.com/compatible-mode/v1"
        assert reader.scalar(select(func.count()).select_from(TenantModelInstance)) == 2
