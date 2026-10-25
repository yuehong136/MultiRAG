"""Authenticated Dify/chunk contracts with real PostgreSQL and Milvus readback."""

import asyncio
import hashlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from uuid import uuid4

import numpy as np
import pytest
import requests
import sqlalchemy as sa
import uvicorn
from pymilvus import MilvusClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from api.db.db_models import APIToken, Document, Knowledgebase, Tenant, TenantLLM, User, UserTenant, get_async_db, get_db
from api.db.services.knowledgebase_service import KnowledgebaseService
from common import settings
from common.bootstrap import ensure_initialized
from common.config_utils import CONFIGS
from core.nlp.search import index_name_one
from tests.support.database import scratch_database
from tests.support.event_loop import UVICORN_LOOP
from tests.support.web_consumer import tsx_runner


def _save(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, default=str, indent=2))


@pytest.fixture(scope="module")
def shared_database(_require_services: Any, tmp_path_factory: Any) -> Iterator[sa.Engine]:
    evidence = Path(os.environ.get("MULTIRAG_C_EVIDENCE_DIR", str(tmp_path_factory.mktemp("shared-owner"))))
    with scratch_database(evidence) as engine:
        yield engine


@pytest.fixture
def shared_api(shared_database: sa.Engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[dict[str, Any]]:
    ensure_initialized()
    import api.apps as apps
    from api.db import db_models

    with ExitStack() as cleanup:
        evidence = Path(os.environ.get("MULTIRAG_C_EVIDENCE_DIR", str(tmp_path)))
        evidence.mkdir(parents=True, exist_ok=True)
        ids = {key: uuid4().hex for key in ["owner", "member", "outsider", "team", "private", "invalid", "doc", "private_doc", "invalid_doc"]}
        collections = {key: index_name_one(ids["owner"], "shared_" + ids[key]) for key in ["team", "private", "invalid"]}
        manifest = {"database": shared_database.url.database, "ids": ids, "collections": collections, "port": None, "pid": os.getpid(), "cleanup": False}
        path = evidence / (ids["team"] + ".resources.json")
        _save(path, manifest)
        monkeypatch.setattr(settings, "FACTORY_LLM_INFOS", [*settings.FACTORY_LLM_INFOS, {"name": "Controlled"}])
        store = settings.docStoreConn
        assert store.db_type() == "milvus"
        cfg = CONFIGS["milvus"]
        reader = MilvusClient(uri=cfg["hosts"], user=cfg.get("username", ""), password=cfg.get("password", ""), db_name=cfg.get("db_name") or "default")
        cleanup.callback(reader.close)

        def remove_collections() -> None:
            for collection in collections.values():
                if reader.has_collection(collection):
                    reader.drop_collection(collection)
                assert not reader.has_collection(collection)
            manifest.update(collections_absent=True, cleanup=True)
            _save(path, manifest)

        cleanup.callback(remove_collections)
        for collection in collections.values():
            assert not reader.has_collection(collection)
        tokens = {role: "shared-" + uuid4().hex for role in ["owner", "member", "outsider"]}
        with Session(shared_database) as db:
            for role in ["owner", "member", "outsider"]:
                db.add(User(id=ids[role], email=ids[role] + "@shared.test", nickname=role, access_token="active", is_active=True))
                db.add(Tenant(id=ids[role], name=role, llm_id="", embd_id="", asr_id="", img2txt_id="", parser_ids="naive"))
                db.add(UserTenant(id=uuid4().hex, user_id=ids[role], tenant_id=ids[role], role="owner", invited_by=ids[role]))
                db.add(APIToken(tenant_id=ids[role], token=tokens[role], name="shared-owner"))
            db.add(UserTenant(id=uuid4().hex, user_id=ids["member"], tenant_id=ids["owner"], role="normal", invited_by=ids["owner"]))
            model = TenantLLM(tenant_id=ids["owner"], llm_factory="Controlled", llm_name="shared-embedding", mdl_type="embedding", api_key="controlled", api_base="https://controlled.invalid")
            foreign = TenantLLM(tenant_id=ids["outsider"], llm_factory="Controlled", llm_name="shared-embedding", mdl_type="embedding", api_key="controlled", api_base="https://controlled.invalid")
            db.add_all([model, foreign])
            db.flush()
            model_id, foreign_id = model.id, foreign.id
            manifest["model_ids"] = [model_id, foreign_id]
            _save(path, manifest)
            for key, doc in [("team", "doc"), ("private", "private_doc"), ("invalid", "invalid_doc")]:
                db.add(
                    Knowledgebase(
                        id=ids[key],
                        tenant_id=ids["owner"],
                        created_by=ids["owner"],
                        name="shared_" + ids[key],
                        embd_id="shared-embedding@Controlled",
                        tenant_embd_id=model_id,
                        permission="me" if key == "private" else "team",
                        status="0" if key == "invalid" else "1",
                        parser_id="naive",
                        parser_config={},
                    )
                )
                db.add(Document(id=ids[doc], kb_id=ids[key], created_by=ids["owner"], name="shared.txt", type="txt", parser_id="naive", parser_config={}, chunk_num=0, token_num=0))
            db.commit()
        sessions = sessionmaker(shared_database, expire_on_commit=False)
        async_engine = create_async_engine(shared_database.url, poolclass=NullPool)
        cleanup.callback(lambda: asyncio.run(async_engine.dispose()))
        async_sessions = async_sessionmaker(async_engine, expire_on_commit=False)

        def sync_db() -> Iterator[Session]:
            with sessions() as db:
                yield db

        async def async_db() -> AsyncIterator[AsyncSession]:
            async with async_sessions() as db:
                yield db

        monkeypatch.setitem(apps.app.dependency_overrides, get_db, sync_db)
        monkeypatch.setitem(apps.app.dependency_overrides, get_async_db, async_db)
        monkeypatch.setattr(db_models, "SessionLocal", sessions)
        monkeypatch.setattr(db_models, "async_session_factory", async_sessions)
        monkeypatch.setattr(apps, "SessionLocal", sessions)
        calls: list[dict[str, Any]] = []
        retrieval_trace: list[dict[str, Any]] = []
        original_search = settings.retriever.search

        async def traced_search(req: dict[str, Any], indices: Any, kb_ids: list[str], *args: Any, **kwargs: Any) -> Any:
            result = await original_search(req, indices, kb_ids, *args, **kwargs)
            retrieval_trace.append({"request": req, "indices": indices, "dataset_ids": kb_ids, "total": result.total, "ids": result.ids})
            return result

        monkeypatch.setattr(settings.retriever, "search", traced_search)
        cleanup.callback(lambda: _save(evidence / (ids["team"] + ".retrieval-trace.json"), retrieval_trace))
        storage_calls: list[str] = []

        def forbidden_storage(*args: Any, **kwargs: Any) -> None:
            storage_calls.append("unexpected object mutation")
            raise AssertionError("Text chunk contract must not mutate object storage")

        monkeypatch.setattr(settings.STORAGE_IMPL, "put", forbidden_storage)
        monkeypatch.setattr(settings.STORAGE_IMPL, "rm", forbidden_storage)

        class Embedding:
            db = None

            def encode(self, texts: list[str]) -> tuple[np.ndarray, int]:
                return np.ones((len(texts), 768)), 2

            def encode_queries(self, text: str) -> tuple[np.ndarray, int]:
                return np.ones(768), 1

        def bundle(db: Session, tenant_id: str, config: dict[str, Any]) -> Embedding:
            assert tenant_id == ids["owner"] and config["tenant_id"] == ids["owner"]
            calls.append({"tenant_id": tenant_id, "model_id": config["id"]})
            return Embedding()

        for name in ["api.apps.restful_apis.chunk", "api.apps.restful_apis.dify_retrieval"]:
            monkeypatch.setattr(sys.modules[name], "LLMBundle", bundle)
        monkeypatch.setattr(sys.modules["api.apps.restful_apis.dify_retrieval"], "label_question", lambda *_: {})
        listener = socket.socket()
        cleanup.callback(listener.close)
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        manifest["port"] = port
        _save(path, manifest)
        server = uvicorn.Server(uvicorn.Config(apps.app, lifespan="off", log_level="error", access_log=False, loop=UVICORN_LOOP))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)

        def stop() -> None:
            server.should_exit = True
            thread.join(timeout=15)
            listener.close()
            assert not thread.is_alive()
            with socket.socket() as probe:
                assert probe.connect_ex(("127.0.0.1", port)) != 0
            manifest["listener_closed"] = True
            _save(path, manifest)

        cleanup.callback(stop)
        thread.start()
        deadline = time.monotonic() + 30
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started
        jwt = {role: apps.manager.create_access_token(data={"sub": ids[role] + "@shared.test"}) for role in tokens}
        yield {
            "ids": ids,
            "tokens": tokens,
            "jwt": jwt,
            "base": f"http://127.0.0.1:{port}",
            "engine": shared_database,
            "reader": reader,
            "collections": collections,
            "calls": calls,
            "evidence": evidence,
            "model_id": model_id,
            "foreign_id": foreign_id,
            "storage_calls": storage_calls,
        }


def _snapshot(env: dict[str, Any]) -> dict[str, Any]:
    with env["engine"].connect() as db:
        sql = {model.__tablename__: [dict(row) for row in db.execute(sa.select(model.__table__).order_by(model.id)).mappings()] for model in [Knowledgebase, Document, UserTenant]}
    index = {
        name: sorted(env["reader"].query(collection, filter='pk != ""', output_fields=["*", "q_768_vec", "vector"], consistency_level="Strong"), key=lambda row: row["pk"])
        if env["reader"].has_collection(collection)
        else []
        for name, collection in env["collections"].items()
    }
    return {"sql": sql, "index": index, "model_calls": list(env["calls"]), "storage_calls": list(env["storage_calls"])}


def _request(env: dict[str, Any], method: str, path: str, role: str = "member", mode: str = "jwt", **kwargs: Any) -> requests.Response:
    response = requests.request(method, env["base"] + path, headers={"Authorization": "Bearer " + env[mode][role]}, timeout=60, **kwargs)
    return response


def test_shared_chunk_routes_and_dify_use_owner_with_denial_readback(shared_api: dict[str, Any]) -> None:
    env, ids = shared_api, shared_api["ids"]
    before = _snapshot(env)
    for headers in [{}, {"Authorization": "Bearer invalid-fixture-key"}]:
        for params in [{}, {"knowledge_id": ids["private"], "query": "probe", "top_k": "invalid", "use_kg": "true"}]:
            response = requests.get(env["base"] + "/api/v1/dify/retrieval", headers=headers, params=params, timeout=30)
            assert response.status_code == 200 and response.json() == {"retcode": 0, "retmsg": "success", "data": True}, response.text
            assert _snapshot(env) == before
    collection = f"/api/v1/datasets/{ids['team']}/documents/{ids['doc']}/chunks"
    response = _request(env, "POST", collection, json={"content": "shared owner index acceptance"})
    assert response.status_code == 200 and response.json()["code"] == 0, response.text
    chunk = response.json()["data"]["chunk"]["id"]
    assert env["calls"] == [{"tenant_id": ids["owner"], "model_id": env["model_id"]}]
    assert len(_snapshot(env)["index"]["team"]) == 1
    with Session(env["engine"]) as db:
        assert db.get(Document, ids["doc"]).chunk_num == 1
    for mode in ["jwt", "tokens"]:
        response = _request(env, "GET", collection, mode=mode)
        assert response.status_code == 200 and response.json()["data"]["chunks"][0]["id"] == chunk, response.text
        response = _request(env, "GET", collection + "/" + chunk, mode=mode)
        assert response.json()["data"]["content_with_weight"] == ("shared owner index acceptance" if mode == "jwt" else "shared owner updated")
        response = _request(env, "PATCH", collection + "/" + chunk, mode=mode, json={"content": "shared owner updated"})
        assert response.json().get("code") == 0, response.text
        response = _request(env, "PATCH", collection, mode=mode, json={"chunk_ids": [chunk], "available": False})
        assert response.json().get("code") == 0, response.text
        assert _snapshot(env)["index"]["team"][0]["available_int"] == 0
        assert _request(env, "PATCH", collection, mode=mode, json={"chunk_ids": [chunk], "available": True}).json()["code"] == 0
    for key, doc, role in [("private", "private_doc", "member"), ("team", "doc", "outsider"), ("invalid", "invalid_doc", "owner")]:
        route = f"/api/v1/datasets/{ids[key]}/documents/{ids[doc]}/chunks"
        for mode in ["jwt", "tokens"]:
            before = _snapshot(env)
            for method, suffix, payload in [
                ("GET", "", None),
                ("GET", "/" + chunk, None),
                ("POST", "", {"content": "denied", "image_base64": "ZGVuaWVk"}),
                ("PATCH", "/" + chunk, {"content": "denied", "image_base64": "ZGVuaWVk"}),
                ("PATCH", "", {"chunk_ids": [chunk], "available": False}),
                ("DELETE", "", {"chunk_ids": [chunk]}),
            ]:
                response = _request(env, method, route + suffix, role=role, mode=mode, **({"json": payload} if payload is not None else {}))
                assert response.json()["code"] != 0, response.text
            response = _request(env, "POST", "/api/v1/dify/retrieval", role=role, mode="tokens", json={"knowledge_id": ids[key], "query": "denied"})
            assert response.status_code == 403 and response.json()["error_code"] == 109, response.text
            assert _snapshot(env) == before
    before = _snapshot(env)
    for headers in [{}, {"Authorization": "Bearer invalid-fixture-key"}, {"Authorization": "Bearer " + env["jwt"]["owner"]}]:
        response = requests.post(env["base"] + "/api/v1/dify/retrieval", headers=headers, json={"knowledge_id": ids["team"], "query": "denied"}, timeout=30)
        assert response.status_code == 403 and response.json()["error_code"] == 403, response.text
    response = _request(env, "POST", "/api/v1/dify/retrieval", mode="tokens", json={"knowledge_id": uuid4().hex, "query": "absent"})
    assert response.status_code == 404 and response.json()["error_code"] == 404, response.text
    assert _snapshot(env) == before
    private_route = f"/api/v1/datasets/{ids['private']}/documents/{ids['private_doc']}/chunks"
    assert _request(env, "POST", private_route, role="owner", json={"content": "private owner accepted"}).json()["code"] == 0
    for role, key in [("owner", "private"), ("member", "team")]:
        # The retrieval exercise starts from independently verified native data.
        native = env["reader"].search(env["collections"][key], data=[[1.0] * 768], anns_field="q_768_vec", limit=3, consistency_level="Strong")
        assert len(native[0]) == 1
        response = _request(env, "POST", "/api/v1/dify/retrieval", role=role, mode="tokens", json={"knowledge_id": ids[key], "query": "owner", "retrieval_setting": {"top_k": 3}})
        assert response.status_code == 200 and response.json()["records"], response.text
        assert response.json()["records"][0]["metadata"]["document_id"] == ids["private_doc" if key == "private" else "doc"]
        before = _snapshot(env)
        response = _request(env, "GET", "/api/v1/dify/retrieval", role=role, mode="tokens", params={"knowledge_id": ids[key], "query": "owner", "top_k": "3", "score_threshold": "0"})
        assert response.status_code == 200 and response.json() == {"retcode": 0, "retmsg": "success", "data": True}, response.text
        assert _snapshot(env) == before
    # All visibility helper consumers use active membership/status and private creator rules.
    for role, expected in [("owner", {ids["team"], ids["private"]}), ("member", {ids["team"]}), ("outsider", set())]:
        with Session(env["engine"]) as db:
            listed = KnowledgebaseService.get_by_tenant_ids(db, [ids["owner"]], ids[role], 1, 100, "id", False)[0]
            assert {row["id"] for row in listed} == expected
            if expected:
                offset = KnowledgebaseService.get_by_tenant_ids_by_offset(db, [ids["owner"]], ids[role], 0, 100, "id", False)
                assert {row["id"] for row in offset} == expected
            else:
                with pytest.raises(IndexError):
                    KnowledgebaseService.get_by_tenant_ids_by_offset(db, [ids["owner"]], ids[role], 0, 100, "id", False)
            all_kbs = KnowledgebaseService.get_all_kb_by_tenant_ids(db, [ids["owner"]], ids[role])
            assert {row["name"] for row in all_kbs} == {"shared_" + kb for kb in expected}
    before = _snapshot(env)
    with env["engine"].begin() as db:
        db.execute(sa.update(Knowledgebase).where(Knowledgebase.id == ids["team"]).values(tenant_embd_id=env["foreign_id"]))
    calls = len(env["calls"])
    response = _request(env, "POST", collection, json={"content": "foreign model must fail"})
    assert response.json()["retcode"] == 100 and "Tenant Model with id" in response.json()["retmsg"] and len(env["calls"]) == calls, response.text
    assert _snapshot(env)["index"] == before["index"]
    with env["engine"].begin() as db:
        db.execute(sa.update(Knowledgebase).where(Knowledgebase.id == ids["team"]).values(tenant_embd_id=env["model_id"]))
    assert _snapshot(env) == before
    # Name fallback must resolve the same dataset-owner model as its numeric ID.
    with env["engine"].begin() as db:
        db.execute(sa.update(Knowledgebase).where(Knowledgebase.id == ids["team"]).values(tenant_embd_id=None))
    response = _request(env, "POST", collection, mode="tokens", json={"content": "owner name fallback"})
    assert response.json().get("code") == 0, response.text
    assert env["calls"][-1]["model_id"] == env["model_id"]
    extra = response.json()["data"]["chunk"]["id"]
    assert _request(env, "DELETE", collection, mode="tokens", json={"chunk_ids": [extra]}).json()["code"] == 0
    assert _request(env, "DELETE", collection, json={"chunk_ids": [chunk]}).json()["code"] == 0
    assert _snapshot(env)["index"]["team"] == []
    assert _request(env, "DELETE", private_route, role="owner", mode="tokens", json={"delete_all": True}).json()["code"] == 0
    assert _snapshot(env)["index"]["private"] == [] and env["storage_calls"] == []
    _save(
        env["evidence"] / "shared-chunk-http.json",
        {"result": "passed", "owner_model_calls": env["calls"], "final_snapshot_sha256": hashlib.sha256(json.dumps(_snapshot(env), default=str, sort_keys=True).encode()).hexdigest()},
    )


@pytest.mark.external_consumer
def test_actual_web_member_chunk_client(shared_api: dict[str, Any]) -> None:
    env, ids = shared_api, shared_api["ids"]
    collection = f"/api/v1/datasets/{ids['team']}/documents/{ids['doc']}/chunks"
    response = _request(env, "POST", collection, json={"content": "shared Web acceptance"})
    assert response.status_code == 200 and response.json()["code"] == 0, response.text
    chunk = response.json()["data"]["chunk"]["id"]
    # Exercise the active independent Web API module against this live listener.
    web = Path(os.environ.get("WEB_DATASET_CHECKOUT", str(Path(__file__).resolve().parents[3] / "web")))
    assert tsx_runner(web).is_file(), "Set WEB_DATASET_CHECKOUT to the installed independent Web consumer checkout"
    script = env["evidence"] / "web-chunks.mts"
    script.write_text(
        "import assert from 'node:assert/strict';\n"
        + "import { apiClient } from "
        + json.dumps(str(web / "src/api/client.ts"))
        + ";\n"
        + "import { knowledgeRestConfig } from "
        + json.dumps(str(web / "src/api/knowledge-config.ts"))
        + ";\n"
        + "import { knowledgeDocumentAPI as api } from "
        + json.dumps(str(web / "src/api/knowledge-documents.ts"))
        + ";\n"
        + "knowledgeRestConfig.baseURL = process.env.C_BASE + '/api'; apiClient.setAuthToken(process.env.C_TOKEN!);\n"
        + "const p = {kb_id: process.env.C_KB!, doc_id: process.env.C_DOC!};\n"
        + "const list = await api.listChunks(p); assert.equal(list.total, 1);\n"
        + "await api.setChunk({...p, chunk_id: process.env.C_CHUNK!, content_with_weight: 'web member updated'});\n"
        + "const item = await api.getChunk({...p, chunk_id: process.env.C_CHUNK!}); assert.equal(item.content_with_weight, 'web member updated');\n"
        + "await api.switchChunks({...p, chunk_ids: [process.env.C_CHUNK!], available_int: 0});\n"
        + "assert.equal((await api.getChunk({...p, chunk_id: process.env.C_CHUNK!})).available_int, 0);\n"
        + "await api.switchChunks({...p, chunk_ids: [process.env.C_CHUNK!], available_int: 1});\n"
        + "console.log('actual Web member list/update/get/toggle passed');\n"
    )
    execution = subprocess.run(
        [str(tsx_runner(web)), "--tsconfig", str(web / "tsconfig.app.json"), str(script)],
        cwd=web,
        capture_output=True,
        text=True,
        timeout=90,
        env={**os.environ, "C_BASE": env["base"], "C_TOKEN": env["jwt"]["member"], "C_KB": ids["team"], "C_DOC": ids["doc"], "C_CHUNK": chunk},
    )
    (env["evidence"] / "web-consumer.log").write_text(execution.stdout + execution.stderr)
    assert execution.returncode == 0, execution.stdout + execution.stderr
    assert _snapshot(env)["index"]["team"][0]["content_with_weight"] == "web member updated"
