"""Owned PostgreSQL rows, real save/read routes, bundles and SDK request payloads."""

import asyncio
import base64
import json
import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from api.db.db_models import LLM, LLMFactories, Tenant, TenantLLM, get_db
from api.db.joint_services.tenant_model_service import get_model_config_by_id, get_model_config_by_type_and_name
from api.db.services.llm_service import LLMBundle
from api.db.services.tenant_llm_service import TenantLLMService
from common import resources, settings

pytestmark = pytest.mark.integration


def test_saved_bedrock_and_huggingface_models_use_registered_consumers(bootstrapped_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    import litellm
    import requests

    from api.db.services import tenant_llm_service
    from core.llm.cv import BedrockCV

    owner = uuid.uuid4().hex
    engine = bootstrapped_engine
    with engine.connect() as connection:
        scratch_version = connection.exec_driver_sql("select version()").scalar_one()
    credentials = {"auth_mode": "assume_role", "bedrock_region": "", "is_tools": False}
    with Session(engine) as db:
        vision = TenantLLM(tenant_id=owner, llm_factory="Bedrock", llm_name="amazon.nova-pro-v1:0", mdl_type="image2text", api_key=json.dumps(credentials), api_base="")
        rerank = TenantLLM(
            tenant_id=owner,
            llm_factory="HuggingFace",
            llm_name="rerank___HuggingFace",
            mdl_type="rerank",
            api_key=json.dumps({"api_key": "x", "is_tools": False}),
            api_base="http://provider.invalid/prefix/rerank/",
        )
        db.add_all([vision, rerank])
        db.commit()
        vision_id, rerank_id = vision.id, rerank.id
    assert vision_id is not None and rerank_id is not None

    @contextmanager
    def scratch_connection() -> Iterator[Session]:
        with Session(engine) as db:
            yield db

    # Keep the production usage UPDATE and commit, on this owned scratch DB.
    monkeypatch.setattr(tenant_llm_service, "db_connection", scratch_connection)
    calls: list[dict[str, Any]] = []
    malformed = [False]

    def completion(**kwargs: Any) -> litellm.ModelResponse:
        assert kwargs["aws_region_name"] == "us-east-1"
        assert "aws_access_key_id" not in kwargs
        calls.append(kwargs)
        return litellm.ModelResponse(choices=[{"message": {"role": "assistant", "content": " caption "}}], usage={"total_tokens": 12})

    def post(url: str, **kwargs: Any) -> requests.Response:
        assert url == "http://provider.invalid/prefix/rerank"
        assert kwargs["timeout"] == 30
        calls.append(kwargs)
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps([] if malformed[0] else [{"index": 1, "score": 0.9}, {"index": 0, "score": 0.0}]).encode()
        response._content_consumed = True
        return response

    monkeypatch.setattr(litellm, "completion", completion)
    monkeypatch.setattr(requests, "post", post)
    try:
        with Session(engine) as db:
            config = get_model_config_by_id(db, vision_id, tenant_id=owner)
            bundle = LLMBundle(db, owner, config)
            assert type(bundle.mdl) is BedrockCV
            assert bundle.describe_with_prompt(b"image", "describe") == "caption"
        with Session(engine) as db:
            assert db.get(TenantLLM, vision_id).used_tokens == 12
            assert json.loads(db.get(TenantLLM, vision_id).api_key) == credentials
            with pytest.raises(LookupError):
                get_model_config_by_id(db, vision_id, tenant_id=uuid.uuid4().hex)

        with Session(engine) as db:
            bundle = LLMBundle(db, owner, get_model_config_by_id(db, rerank_id, tenant_id=owner))
            scores, tokens = bundle.similarity("query", ["zero", "high"])
            assert scores.tolist() == [0.0, 0.9]
            assert tokens > 0
        with Session(engine) as db:
            used_tokens = db.get(TenantLLM, rerank_id).used_tokens
            assert used_tokens > 0
        malformed[0] = True
        with Session(engine) as db:
            bundle = LLMBundle(db, owner, get_model_config_by_id(db, rerank_id, tenant_id=owner))
            with pytest.raises(ValueError, match="one result per input"):
                bundle.similarity("query", ["zero", "high"])
        with Session(engine) as db:
            assert db.get(TenantLLM, rerank_id).used_tokens == used_tokens
            assert db.get(TenantLLM, vision_id).used_tokens == 12
        assert len(calls) == 3
    finally:
        with Session(engine) as db:
            db.execute(delete(TenantLLM).where(TenantLLM.tenant_id == owner))
            db.commit()
        with Session(engine) as db:
            assert db.scalars(select(TenantLLM).where(TenantLLM.tenant_id == owner)).first() is None
        if path := os.getenv("A_MODEL_EVIDENCE_DIR"):
            Path(path).mkdir(parents=True, exist_ok=True)
            (Path(path) / "scratch-cleanup.json").write_text(json.dumps({"database": engine.url.database, "version": scratch_version, "owner": owner, "remaining_tenant_models": 0}) + "\n")


@pytest.fixture
def model_contract(bootstrapped_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    # This DB contract overrides authentication and never uses global storage/search.
    # Preseed only its signer before importing the application; no Redis writes.
    monkeypatch.setitem(resources._state, "secret_key", "model-contract-signing-key")
    from api.apps import llm_app

    engine = bootstrapped_engine
    owners = [uuid.uuid4().hex, uuid.uuid4().hex]
    current_owner = [owners[0]]
    for factory in ["Ollama", "MiniMax"]:
        with Session(engine) as db:
            db.merge(LLMFactories(name=factory, tags="LLM", status="1"))
            db.merge(LLM(fid=factory, llm_name="fixture-model", mdl_type="chat", max_tokens=8192, tags="LLM", is_tools=True))
            db.commit()
    with Session(engine) as db:
        db.add_all([Tenant(id=owner, name="Owned model fixture", llm_id="fixture-model", embd_id="", asr_id="", img2txt_id="", parser_ids="naive") for owner in owners])
        db.commit()
    monkeypatch.setattr(TenantLLMService, "ensure_mineru_from_env", classmethod(lambda cls, db, tenant_id: None))
    monkeypatch.setattr(TenantLLMService, "ensure_opendataloader_from_env", classmethod(lambda cls, db, tenant_id: None))
    monkeypatch.setattr(settings, "FACTORY_LLM_INFOS", [{"name": "Ollama"}, {"name": "MiniMax"}])
    app = FastAPI()
    app.include_router(llm_app.router, prefix="/v1/llm")

    def session() -> Iterator[Session]:
        with Session(engine) as db:
            yield db

    app.dependency_overrides[get_db] = session
    app.dependency_overrides[llm_app.manager] = lambda: SimpleNamespace(id=current_owner[0])
    wire: list[dict[str, Any]] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "fixture.invalid"
        body = json.loads(request.content)
        wire.append({"body": body, "path": request.url.path, "query": parse_qs(request.url.query.decode()), "authorization": request.headers.get("authorization")})
        tool_turn = bool(body.get("tools")) and not any(message["role"] == "tool" for message in body["messages"])
        stream = body.get("stream", False)
        if request.url.path == "/api/chat":
            message: dict[str, Any] = {"role": "assistant", "content": "" if tool_turn else "answer"}
            if tool_turn:
                message["tool_calls"] = [{"function": {"name": "first", "arguments": {"query": "hello"}}}]
            result = {"model": "fixture-model", "created_at": "2026-10-17T00:00:00Z", "message": message, "done": True, "done_reason": "stop", "prompt_eval_count": 2, "eval_count": 3}
            if stream:
                return httpx.Response(200, content=json.dumps(result) + "\n", headers={"content-type": "application/x-ndjson"}, request=request)
            return httpx.Response(200, json=result, request=request)
        assert request.url.path == "/v1/chat/completions"
        message = {"role": "assistant", "content": None if tool_turn else "answer"}
        if tool_turn:
            message["tool_calls"] = [{"id": "call-first", "type": "function", "function": {"name": "first", "arguments": '{"query":"hello"}'}}]
        result = {
            "id": "completion-fixture",
            "object": "chat.completion.chunk" if stream else "chat.completion",
            "created": 1,
            "model": "fixture-model",
            "choices": [{"index": 0, "delta" if stream else "message": message, "finish_reason": "tool_calls" if tool_turn else "stop"}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
        }
        if stream:
            return httpx.Response(200, content=f"data: {json.dumps(result)}\n\ndata: [DONE]\n\n", headers={"content-type": "text/event-stream"}, request=request)
        return httpx.Response(200, json=result, request=request)

    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(httpx.AsyncClient, "_transport_for_url", lambda self, url: transport)
    original_transport = httpx.Client._transport_for_url

    def respond_sync(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "fixture.invalid"
        body = json.loads(request.content)
        wire.append({"body": body, "path": request.url.path, "query": {}, "authorization": request.headers.get("authorization")})
        if request.url.path == "/api/generate":
            result = {"model": "fixture-model", "response": "answer", "done": True, "eval_count": 3, "prompt_eval_count": 2}
        else:
            assert request.url.path == "/api/chat"
            result = {"model": "fixture-model", "message": {"role": "assistant", "content": "answer"}, "done": True, "eval_count": 3, "prompt_eval_count": 2}
        return httpx.Response(200, json=result, request=request)

    sync_transport = httpx.MockTransport(respond_sync)
    monkeypatch.setattr(httpx.Client, "_transport_for_url", lambda self, url: sync_transport if url.host == "fixture.invalid" else original_transport(self, url))
    evidence_dir = os.getenv("LLM_TOOL_EVIDENCE_DIR")
    if evidence_dir:
        path = Path(evidence_dir)
        path.mkdir(parents=True, exist_ok=True)
        (path / "scratch-database.json").write_text(json.dumps({"database": engine.url.database}) + "\n")
    try:
        with TestClient(app) as client:
            yield {"engine": engine, "client": client, "owners": owners, "current_owner": current_owner, "wire": wire}
    finally:
        with Session(engine) as db:
            db.execute(delete(TenantLLM).where(TenantLLM.tenant_id.in_(owners)))
            db.execute(delete(Tenant).where(Tenant.id.in_(owners)))
            db.commit()
        with Session(engine) as db:
            assert db.scalars(select(TenantLLM).where(TenantLLM.tenant_id.in_(owners))).first() is None
            assert db.scalars(select(Tenant).where(Tenant.id.in_(owners))).first() is None
        if evidence_dir:
            (Path(evidence_dir) / f"rows-cleaned-{owners[0]}.json").write_text(json.dumps({"tenant_rows": 0, "model_rows": 0}) + "\n")


@pytest.mark.parametrize("factory", ["Ollama", "MiniMax"])
def test_save_reload_tool_switch_and_credential_preservation(model_contract: dict[str, Any], factory: str) -> None:
    engine = model_contract["engine"]
    client = model_contract["client"]
    owner = model_contract["owners"][0]
    base_url = "http://fixture.invalid" if factory == "Ollama" else "http://fixture.invalid/v1"
    provider_key = {"api_key": "fixture-key", "group_id": "fixture-group &中文", "opaque": {"retain": True}} if factory == "MiniMax" else "fixture-key"
    common = {"llm_factory": factory, "llm_name": "fixture-model", "mdl_type": "chat", "api_base": base_url, "max_tokens": 4096}

    def save(**fields: Any) -> None:
        response = client.post("/v1/llm/add_llm", json={**common, **fields})
        assert response.status_code == 200
        assert response.json()["retcode"] == 0, response.json()

    def read_row() -> dict[str, Any]:
        with Session(engine) as db:
            row = db.scalars(select(TenantLLM).where(TenantLLM.tenant_id == owner, TenantLLM.llm_factory == factory, TenantLLM.llm_name == "fixture-model")).one()
            return row.to_dict()

    save(api_key=provider_key)
    row = read_row()
    assert "is_tools" not in (json.loads(row["api_key"]) if row["api_key"].startswith("{") else {})
    with Session(engine) as db:
        assert get_model_config_by_id(db, row["id"], tenant_id=owner)["is_tools"] is True
    runtime_evidence: list[dict[str, Any]] = []
    for enabled in [False, True, False]:
        save(is_tools=enabled)
        row = read_row()
        saved_key, flag, payload = TenantLLMService._decode_api_key_config(row["api_key"])
        assert saved_key == "fixture-key" and flag is enabled
        if factory == "MiniMax":
            assert json.loads(payload or "{}") == provider_key
        for details in [False, True]:
            listed = client.get("/v1/llm/my_llms", params={"include_details": details}).json()
            assert listed["retcode"] == 0
            model = next(model for model in listed["data"][factory]["llm"] if model["name"] == "fixture-model")
            assert model["is_tools"] is enabled
            assert "fixture-key" not in json.dumps(listed) and "fixture-group" not in json.dumps(listed)
        with Session(engine) as db:
            by_id = get_model_config_by_id(db, row["id"], tenant_id=owner)
            by_name = get_model_config_by_type_and_name(db, owner, "chat", f"fixture-model@{factory}")
            legacy = TenantLLMService.get_model_config(db, owner, "chat", f"fixture-model@{factory}")
            assert all(config["is_tools"] is enabled for config in [by_id, by_name, legacy])
        calls: list[dict[str, Any]] = []

        class Tools:
            async def tool_call_async(self, name: str, arguments: dict[str, Any]) -> str:
                assert name == "first"
                calls.append(arguments)
                return "tool answer"

        async def run() -> str:
            with Session(engine) as db:
                bundle = LLMBundle(db, owner, get_model_config_by_id(db, row["id"], tenant_id=owner), max_retries=0)
                bundle.bind_tools(Tools(), [{"type": "function", "function": {"name": "first", "parameters": {"type": "object", "properties": {"query": {"type": "string"}}}}}])
                return await bundle.async_chat("", [{"role": "user", "content": "hello"}], {})

        start = len(model_contract["wire"])
        answer = asyncio.run(run())
        assert "answer" in answer and "**ERROR**" not in answer
        requests = model_contract["wire"][start:]
        assert bool(requests[0]["body"].get("tools")) is enabled
        assert calls == ([{"query": "hello"}] if enabled else [])
        if factory == "MiniMax":
            assert all(request["query"]["GroupId"] == [provider_key["group_id"]] for request in requests)
            assert all(request["authorization"] == "Bearer fixture-key" for request in requests)
        runtime_evidence.append(
            {
                "enabled": enabled,
                "requests": [{"path": request["path"], "body": request["body"], "group_id_checked": factory == "MiniMax", "authorization_checked": factory == "MiniMax"} for request in requests],
                "executed_tools": len(calls),
            }
        )
    response = client.post("/v1/llm/add_llm", json={"llm_factory": factory, "llm_name": "fixture-model", "mdl_type": "chat", "is_tools": False})
    assert response.status_code == 200 and response.json()["retcode"] == 0
    assert read_row()["api_base"] == base_url and read_row()["max_tokens"] == 4096
    save(api_key="replacement-key")
    row = read_row()
    key, flag, payload = TenantLLMService._decode_api_key_config(row["api_key"])
    assert key == "replacement-key" and flag is False
    if factory == "MiniMax":
        assert json.loads(payload or "{}") == {**provider_key, "api_key": "replacement-key"}
        rotated = client.post("/v1/llm/set_api_key", json={"llm_factory": factory, "api_key": "rotated-key", "base_url": base_url})
        assert rotated.status_code == 200 and rotated.json()["retcode"] == 0
        row = read_row()
        rotated_key, flag, payload = TenantLLMService._decode_api_key_config(row["api_key"])
        assert rotated_key == "rotated-key" and flag is False
        assert json.loads(payload or "{}") == {**provider_key, "api_key": "rotated-key"}
    before_verify = row["api_key"]
    save(api_key="verify-only", is_tools=True, verify=True)
    assert read_row()["api_key"] == before_verify
    invalid = client.post("/v1/llm/add_llm", json={**common, "is_tools": "false"})
    assert invalid.status_code == 422 and read_row()["api_key"] == before_verify
    model_contract["current_owner"][0] = model_contract["owners"][1]
    save(is_tools=True)
    with Session(engine) as db:
        other = db.scalars(select(TenantLLM).where(TenantLLM.tenant_id == model_contract["owners"][1], TenantLLM.llm_factory == factory)).one()
        assert TenantLLMService._decode_api_key_config(other.api_key)[0] == "x"
    assert read_row()["api_key"] == before_verify
    evidence_dir = os.getenv("LLM_TOOL_EVIDENCE_DIR")
    if evidence_dir:
        (Path(evidence_dir) / f"{factory.lower()}-contract.json").write_text(
            json.dumps(
                {
                    "factory": factory,
                    "sql_commit_readback": True,
                    "all_config_paths": True,
                    "false_and_absent_distinct": True,
                    "new_key_replaces_old": True,
                    "verify_no_write": True,
                    "invalid_bool_no_write": True,
                    "tenant_isolation": True,
                    "runtime": runtime_evidence,
                },
                indent=2,
            )
            + "\n"
        )


def test_ollama_vision_native_tools_keep_image_payload(model_contract: dict[str, Any]) -> None:
    engine = model_contract["engine"]
    client = model_contract["client"]
    owner = model_contract["owners"][0]
    png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aXioAAAAASUVORK5CYII="
    images = [f"data:image/png;base64,{png}"]
    for enabled in [True, False]:
        response = client.post(
            "/v1/llm/add_llm",
            json={"llm_factory": "Ollama", "llm_name": "fixture-model", "mdl_type": "image2text", "api_key": "fixture-key", "api_base": "http://fixture.invalid", "is_tools": enabled},
        )
        assert response.status_code == 200 and response.json()["retcode"] == 0, response.json()
        calls: list[str] = []

        class Tools:
            async def tool_call_async(self, name: str, arguments: dict[str, Any]) -> str:
                calls.append(name)
                return "tool answer"

        async def run() -> str:
            with Session(engine) as db:
                config = get_model_config_by_type_and_name(db, owner, "chat", "fixture-model@Ollama")
                assert config["mdl_type"] == "image2text" and config["is_tools"] is enabled
                bundle = LLMBundle(db, owner, config, max_retries=0)
                bundle.bind_tools(Tools(), [{"type": "function", "function": {"name": "first", "parameters": {"type": "object", "properties": {"query": {"type": "string"}}}}}])
                return await bundle.async_chat("", [{"role": "user", "content": "hello"}], {}, images=images)

        start = len(model_contract["wire"])
        answer = asyncio.run(run())
        assert "answer" in answer and "**ERROR**" not in answer
        requests = model_contract["wire"][start:]
        first = requests[0]["body"]
        assert bool(first.get("tools")) is enabled
        assert base64.b64decode(first["messages"][0]["images"][0]) == base64.b64decode(png)
        assert calls == (["first"] if enabled else [])
