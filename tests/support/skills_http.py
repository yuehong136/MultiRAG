"""Isolated authenticated Skills HTTP with real SQL, object and index stores."""

import asyncio
import io
import json
import threading
import zipfile
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from uuid import uuid4

import pytest
import requests
import sqlalchemy as sa
from pymilvus import MilvusClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session

from api.db.db_models import APIToken, File, Skill, SkillIndexGeneration, SkillOperation, SkillSearchConfig, SkillSpace, SkillVersion, SkillVersionFile, TenantLLM
from api.skills.milvus_store import MilvusSkillStore
from api.skills.model_runtime import ModelRuntime
from api.skills.runtime import cleanup_files, get_search_runtime
from api.skills.search import SkillSearchRuntime
from api.skills.storage import SkillStorage
from api.skills.worker import SkillWorker
from common.config_utils import CONFIGS
from tests.support.document_image_http import bootstrapped_engine as bootstrapped_engine
from tests.support.document_image_http import image_http_api as image_http_api
from tests.support.document_image_http import image_http_database as image_http_database
from tests.support.document_image_read_service import image_resources as image_resources


@pytest.fixture
def skill_http(image_http_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    from api.apps import app
    from api.skills import model_runtime

    env = image_http_api
    tenant = env["ids"]["owner"]
    sessions = async_sessionmaker(env["async_engine"], expire_on_commit=False)
    calls: list[dict[str, Any]] = []
    faults = {"embedding": False, "rerank": False}

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def do_POST(self) -> None:
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append({"path": self.path, "model": payload.get("model"), "authorized": self.headers.get("Authorization") == "Bearer skills-fixture-key"})
            if self.path.endswith("embeddings"):
                if faults["embedding"]:
                    self.send_response(503)
                    self.end_headers()
                    return
                values = payload["input"]
                if isinstance(values, str):
                    values = [values]
                dimension = 3 if payload["model"] == "three" else 2
                result = {
                    "object": "list",
                    "model": payload["model"],
                    "data": [{"object": "embedding", "index": i, "embedding": ([1.0, 0.1] if "orange" in value else [0.1, 1.0]) + ([0.2] if dimension == 3 else [])} for i, value in enumerate(values)],
                    "usage": {"prompt_tokens": len(values), "total_tokens": len(values)},
                }
            else:
                result = (
                    {"error": "injected malformed success"}
                    if faults["rerank"]
                    else {"results": [{"index": i, "relevance_score": 0.95 if "orange" in value else 0.2} for i, value in enumerate(payload["documents"])]}
                )
            encoded = json.dumps(result).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    provider = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    provider_thread = threading.Thread(target=provider.serve_forever, daemon=True)
    provider_thread.start()
    cfg = CONFIGS["milvus"]
    reader = MilvusClient(uri=cfg["hosts"], user=cfg.get("username", ""), password=cfg.get("password", ""), db_name=cfg.get("db_name") or "default")
    runtime = SkillSearchRuntime(MilvusSkillStore(reader), ModelRuntime())

    @asynccontextmanager
    async def scratch_models() -> AsyncIterator[AsyncSession]:
        async with sessions() as db:
            yield db

    monkeypatch.setattr(model_runtime, "async_db_connection", scratch_models)
    monkeypatch.setitem(app.dependency_overrides, get_search_runtime, lambda: runtime)
    model_ids = [9007199254740993 + i for i in range(3)]
    api_token = "skill-api-" + uuid4().hex
    with Session(env["engine"]) as db:
        for identifier, name, kind in zip(model_ids, ["two", "three", "rank"], ["embedding", "embedding", "rerank"], strict=True):
            db.add(
                TenantLLM(
                    id=identifier,
                    tenant_id=tenant,
                    llm_factory="OpenAI-API-Compatible",
                    llm_name=name,
                    mdl_type=kind,
                    api_base=f"http://127.0.0.1:{provider.server_port}",
                    api_key="skills-fixture-key",
                    max_tokens=8192,
                    status="1",
                )
            )
        db.add(APIToken(tenant_id=tenant, token=api_token, name="image-http"))
        db.commit()
    worker = SkillWorker(sessions, SkillStorage(env["storage"]), runtime, cleanup_files)
    env.update(skill_worker=worker, skill_models=[str(value) for value in model_ids], skill_runtime=runtime, skill_reader=reader, skill_calls=calls, skill_faults=faults, skill_api_token=api_token)
    try:
        yield env
    finally:
        provider.shutdown()
        provider.server_close()
        provider_thread.join(timeout=5)
        assert not provider_thread.is_alive()
        with Session(env["engine"]) as db:
            names = list(db.scalars(sa.select(SkillIndexGeneration.index_name).where(SkillIndexGeneration.tenant_id == tenant)))
            for name in names:
                if reader.has_collection(name):
                    reader.drop_collection(name)
                assert not reader.has_collection(name)
            db.execute(sa.update(SkillSpace).where(SkillSpace.tenant_id == tenant).values(active_generation_id=None))
            db.execute(sa.update(Skill).where(Skill.tenant_id == tenant).values(active_version_id=None))
            for model in (SkillOperation, SkillVersionFile, SkillIndexGeneration, SkillSearchConfig, SkillVersion, Skill, SkillSpace):
                db.execute(sa.delete(model).where(model.tenant_id == tenant))
            db.execute(sa.delete(File).where(File.tenant_id == tenant))
            db.execute(sa.delete(TenantLLM).where(TenantLLM.id.in_(model_ids)))
            db.commit()
            for model in (SkillOperation, SkillVersionFile, SkillIndexGeneration, SkillSearchConfig, SkillVersion, Skill, SkillSpace):
                assert db.scalar(sa.select(sa.func.count()).select_from(model).where(model.tenant_id == tenant)) == 0
        reader.close()


def skill_request(env: dict[str, Any], method: str, path: str, *, token: str | None = None, body: Any = None, files: Any = None, data: Any = None, key: str | None = None, expected: int = 200) -> Any:
    headers = {"Authorization": "Bearer " + (token or env["tokens"]["owner"])}
    if key is not None:
        headers["Idempotency-Key"] = key
    response = requests.request(method, env["base"] + "/api/v1/skills" + path, headers=headers, json=body, files=files, data=data, timeout=60)
    assert response.status_code == expected, response.text
    if "application/json" not in response.headers.get("Content-Type", ""):
        return response.content
    payload = response.json()
    assert payload["code"] == (0 if expected < 400 else expected), payload
    return payload["data"]


def skill_complete(env: dict[str, Any], accepted: dict[str, Any], *, state: str = "succeeded") -> dict[str, Any]:
    assert asyncio.run(env["skill_worker"].run_once())
    operation = skill_request(env, "GET", "/operations/" + accepted["operation_id"])
    assert operation["state"] == state, operation
    return operation


def install(env: dict[str, Any], space: str, version: str, *, description: str = "orange version", activate: bool = True) -> dict[str, Any]:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("SKILL.md", f"---\nname: orange\ndescription: {description}\ntags: [fruit]\n---\nOrange manual")
        archive.writestr("nested/中文.txt", "orange nested bytes")
        archive.writestr("image.bin", b"\xff\x00")
    return skill_request(
        env,
        "POST",
        f"/spaces/{space}/versions",
        data={"manifest": json.dumps({"name": "orange", "version": version, "activate": activate})},
        files={"archive": ("package.zip", output.getvalue(), "application/zip")},
        key="install-" + version,
        expected=202,
    )
