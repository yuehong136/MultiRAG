"""Real Web domain clients, SQL save/readback and protected generated downloads."""

import base64
import hashlib
import json
import os
import shutil
import socket
import subprocess
import threading
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
import uvicorn  # noqa: F401 -- initialize before legacy component nest_asyncio patches
from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from agent.component.docs_generator import DocGenerator, DocGeneratorParam
from agent.component.message import Message
from api.db.db_models import Conversation, Dialog, TenantModel, TenantModelInstance, TenantModelProvider, UserCanvas, UserCanvasVersion, get_db
from api.db.model_contract import MODEL_TYPE_BITS
from api.db.services.tenant_llm_service import TenantLLMService
from tests.support.agent_update_release import release_api as release_api
from tests.support.resource_journal import ResourceJournal
from tests.support.runtime_upload import read_object
from tests.support.runtime_upload import runtime_upload_api as runtime_upload_api
from tests.support.web_consumer import pinned_web_checkout, tsx_runner

pytestmark = pytest.mark.external_consumer


def test_actual_web_chat_model_types_save_and_readback(runtime_upload_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, record_property: Callable[[str, object], None], tmp_path: Path) -> None:
    from api import apps

    env = runtime_upload_api
    sessions = sessionmaker(env["engine"])
    monkeypatch.setattr(apps, "SessionLocal", sessions)

    def scratch_db() -> Iterator[Session]:
        with sessions() as db:
            yield db

    monkeypatch.setitem(apps.app.dependency_overrides, get_db, scratch_db)
    monkeypatch.setattr(TenantLLMService, "ensure_mineru_from_env", classmethod(lambda cls, db, tenant_id: None))
    monkeypatch.setattr(TenantLLMService, "ensure_opendataloader_from_env", classmethod(lambda cls, db, tenant_id: None))
    checkout, _ = pinned_web_checkout()
    script = Path(__file__).resolve().parents[1] / "support/chat_model_web_consumer.mts"
    provider_id, instance_id = uuid4().hex, uuid4().hex
    cache = tmp_path / "web-consumer-vite-cache"
    env["journal"].event("reserve-web-vite-ssr-cache", path=str(cache), web_checkout=str(checkout))
    env["journal"].event("reserve-chat-model-registrations", tenant_id=env["owners"][0], provider_id=provider_id, instance_id=instance_id, names=["vision", "text"])
    try:
        model_ids = []
        with sessions() as db:
            provider = TenantModelProvider(id=provider_id, tenant_id=env["owners"][0], provider_name="Ollama", status="active")
            instance = TenantModelInstance(id=instance_id, provider_id=provider.id, instance_name="chat-types", api_key="controlled-no-provider", base_url="http://127.0.0.1:1", status="active")
            db.add(provider)
            db.flush()
            db.add(instance)
            db.flush()
            for name, kind in [("vision", "image2text"), ("text", "chat")]:
                model = TenantModel(provider_id=provider_id, instance_id=instance_id, model_name=name, model_type=MODEL_TYPE_BITS[kind], extra={})
                db.add(model)
                db.flush()
                model_ids.append(model.id)
            db.commit()
        completed = subprocess.run(
            [str(tsx_runner(checkout)), "--tsconfig", str(checkout / "tsconfig.app.json"), str(script)],
            cwd=checkout,
            env={**os.environ, "DOMAIN_ACCEPTANCE_BASE": env["base"], "DOMAIN_ACCEPTANCE_TOKEN": env["jwt"], "WEB_CONSUMER_VITE_CACHE": str(cache)},
            capture_output=True,
            text=True,
            timeout=90,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        result = json.loads(completed.stdout.splitlines()[-1])
        with sessions() as db:
            for identifier, reference, model_id, kind in zip(result["ids"], result["references"], model_ids, ["image2text", "chat"], strict=True):
                row = db.get(Dialog, identifier)
                assert row is not None and row.tenant_id == env["owners"][0]
                assert row.llm_id == row.tenant_llm_id == model_id == reference
                assert row.llm_setting == {"temperature": 0, "model_type": kind}
                assert row.prompt_config["quote"] is False
        record_property("actual_web_chat_model_sql_readback", json.dumps(result))
    finally:
        if cache.exists():
            shutil.rmtree(cache)
        assert not cache.exists()
        env["journal"].event("web-vite-cache-cleanup-readback", path=str(cache), absent=True)
        with sessions() as db:
            ids = list(db.scalars(select(Dialog.id).where(Dialog.tenant_id == env["owners"][0])))
            db.execute(delete(Conversation).where(Conversation.dialog_id.in_(ids)))
            db.execute(delete(Dialog).where(Dialog.tenant_id == env["owners"][0]))
            db.execute(delete(TenantModel).where(TenantModel.id.in_(model_ids)))
            db.execute(delete(TenantModelInstance).where(TenantModelInstance.id == instance_id))
            db.execute(delete(TenantModelProvider).where(TenantModelProvider.id == provider_id))
            db.commit()
        with sessions() as reader:
            assert reader.scalar(select(Dialog.id).where(Dialog.tenant_id == env["owners"][0])) is None
            assert reader.scalar(select(TenantModel.id).where(TenantModel.id.in_(model_ids))) is None
            assert reader.get(TenantModelInstance, instance_id) is None
            assert reader.get(TenantModelProvider, provider_id) is None


def run_web(env: dict[str, Any], mode: str, **values: str) -> dict[str, Any]:
    checkout, _ = pinned_web_checkout()
    runner = tsx_runner(checkout)
    script = checkout / "scripts/verify-model-document-routes.ts"
    assert runner.exists() and script.exists(), "Production Web consumer checkout is required"
    completed = subprocess.run(
        [str(runner), "--tsconfig", str(checkout / "tsconfig.app.json"), str(script)],
        cwd=checkout,
        env={**os.environ, "DOMAIN_ACCEPTANCE_BASE": env["base"], "DOMAIN_ACCEPTANCE_TOKEN": env.get("jwt") or env["jwts"][0], "DOMAIN_ACCEPTANCE_MODE": mode, **values},
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return json.loads(completed.stdout.splitlines()[-1])


def test_actual_web_model_save_reload(release_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, record_property: Callable[[str, object], None]) -> None:
    from api import apps
    from common import settings

    env = release_api
    journal = ResourceJournal("model-document-web-provider")
    factory = "Ollama"
    monkeypatch.setattr(settings, "FACTORY_LLM_INFOS", [{"name": factory, "llm": []}])
    monkeypatch.setattr(TenantLLMService, "ensure_mineru_from_env", classmethod(lambda cls, db, tenant_id: None))
    monkeypatch.setattr(TenantLLMService, "ensure_opendataloader_from_env", classmethod(lambda cls, db, tenant_id: None))
    # register_page imports this router under api.apps.llm; retain the real JWT loader.
    assert apps.manager is not None
    requests: list[str] = []

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path == "/api/show":
                assert (body.get("model") or body.get("name")) == "combined-fixture"
                result = {"capabilities": ["completion", "tools"]}
            else:
                assert self.path == "/api/chat" and body.get("model") == "combined-fixture", (self.path, sorted(body))
                requests.append(self.path)
                result = {"model": "combined-fixture", "message": {"role": "assistant", "content": "answer"}, "done": True, "eval_count": 3, "prompt_eval_count": 2}
            payload = (json.dumps(result) + "\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    journal.event("reserve-controlled-ollama-provider", host="127.0.0.1", port=0)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    journal.event("created-controlled-ollama-provider", host="127.0.0.1", port=server.server_port)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = run_web(env, "model", DOMAIN_ACCEPTANCE_PROVIDER=f"http://127.0.0.1:{server.server_port}")
        assert result["flags"] == [False, True, False] and len(requests) == 3
        assert result["paths"].count("/api/v1/providers/Ollama/instances") == 6
        assert result["paths"].count("/api/v1/models") == 7
        assert set(result["paths"]) == {"/api/v1/providers/Ollama/instances", "/api/v1/models"}
        with Session(env["engine"]) as db:
            for expected in result["models"]:
                model = db.get(TenantModel, expected["id"])
                assert model is not None
                instance = db.get(TenantModelInstance, model.instance_id)
                provider = db.get(TenantModelProvider, model.provider_id)
                assert provider is not None and provider.tenant_id == env["owners"][0] and provider.provider_name == factory
                assert model.model_name == "combined-fixture" and model.model_types == ["chat"] and model.is_tools is expected["is_tools"]
                assert model.max_tokens == 4096
                assert instance is not None and instance.instance_name == expected["instance"] and instance.api_key == "fixture-key"
                assert instance.base_url == f"http://127.0.0.1:{server.server_port}"
            assert result["anonymous_denied"] is True
        record_property("web_consumer_readback", json.dumps({**result, "database": env["engine"].url.database, "provider_http_requests": len(requests)}))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)
        assert not thread.is_alive()
        with socket.socket() as probe:
            assert probe.connect_ex(("127.0.0.1", server.server_port)) != 0
        journal.event("controlled-provider-cleanup-readback", thread_absent=True, listener_closed=True)
        with Session(env["engine"]) as db:
            providers = list(db.scalars(select(TenantModelProvider.id).where(TenantModelProvider.tenant_id == env["owners"][0])))
            model_ids = list(db.scalars(select(TenantModel.id).where(TenantModel.provider_id.in_(providers))))
            db.execute(delete(TenantModel).where(TenantModel.id.in_(model_ids)))
            db.execute(delete(TenantModelInstance).where(TenantModelInstance.provider_id.in_(providers)))
            db.execute(delete(TenantModelProvider).where(TenantModelProvider.id.in_(providers)))
            db.commit()
        with Session(env["engine"]) as reader:
            assert reader.scalar(select(TenantModel.id).where(TenantModel.id.in_(model_ids))) is None
            assert reader.scalar(select(TenantModelInstance.id).where(TenantModelInstance.provider_id.in_(providers))) is None
            assert reader.scalar(select(TenantModelProvider.id).where(TenantModelProvider.id.in_(providers))) is None
        journal.event("model-consumer-sql-cleanup-readback", model_ids=model_ids, provider_ids=providers, remaining=0)


def test_actual_web_document_save_reload_download(runtime_upload_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path, record_property: Callable[[str, object], None]) -> None:
    from api import apps

    env = runtime_upload_api
    sessions = sessionmaker(bind=env["engine"])
    monkeypatch.setattr(apps, "SessionLocal", sessions)

    def scratch_db() -> Iterator[Session]:
        with sessions() as db:
            yield db

    monkeypatch.setitem(apps.app.dependency_overrides, get_db, scratch_db)
    agent_id: str | None = None
    try:
        saved = run_web(env, "document-save")
        agent_id = saved["agent_id"]
        with sessions() as db:
            row = db.get(UserCanvas, agent_id)
            assert row is not None and row.user_id == env["owners"][0]
            dsl = json.loads(row.dsl) if isinstance(row.dsl, str) else row.dsl
            values = dsl["components"]["DocGenerator:combined"]["obj"]["params"]
        generator = object.__new__(DocGenerator)
        generator._param = DocGeneratorParam()
        generator._param.update(values)
        generator._canvas = SimpleNamespace(get_tenant_id=lambda: env["owners"][0])
        generator._default_output_directory = str(tmp_path / "generated")
        expected = b"controlled document conversion\n"
        # Only the external converter is controlled; component, Message and storage are real.
        monkeypatch.setattr(generator, "_generate_txt", lambda content: generator._write_bytes_output(expected, "txt"))
        info = generator._invoke()
        downloads: list[dict[str, Any]] = []
        message = object.__new__(Message)
        content = message._stringify_message_value(generator.output("download"), downloads=downloads)
        assert json.loads(content) == downloads[0]
        physical = read_object(env["storage"], env["bucket"], f"{env['owners'][0]}/{info['doc_id']}")
        assert physical == expected == base64.b64decode(downloads[0]["base64"], validate=True)
        result = run_web(
            env,
            "document-download",
            DOMAIN_ACCEPTANCE_ATTACHMENT=json.dumps({"doc_id": info["doc_id"], "name": info["filename"], "format": "txt"}),
            DOMAIN_ACCEPTANCE_SHA256=hashlib.sha256(physical).hexdigest(),
        )
        assert result["anonymous_denied"] is True and result["size"] == len(physical)
        record_property("web_consumer_readback", json.dumps({**result, "database": env["engine"].url.database, "agent_id": agent_id}))
        env["record"]["actual_web_document_readback"] = {"agent_id": agent_id, "sha256": result["sha256"], "size": result["size"], "paths": result["paths"]}
    finally:
        if agent_id:
            with sessions() as db:
                db.execute(delete(UserCanvasVersion).where(UserCanvasVersion.user_canvas_id == agent_id))
                db.execute(delete(UserCanvas).where(UserCanvas.id == agent_id))
                db.commit()
                assert db.get(UserCanvas, agent_id) is None

            from core.utils.redis_conn import REDIS_CONN

            redis = REDIS_CONN.REDIS
            assert redis is not None
            keys = list(redis.scan_iter(match=f"*{agent_id}*"))
            if keys:
                redis.delete(*keys)
            assert not list(redis.scan_iter(match=f"*{agent_id}*"))
