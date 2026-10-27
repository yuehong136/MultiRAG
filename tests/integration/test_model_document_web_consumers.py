"""Real Web domain clients, SQL save/readback and protected generated downloads."""

import base64
import hashlib
import json
import os
import subprocess
import threading
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import uvicorn  # noqa: F401 -- initialize before legacy component nest_asyncio patches
from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from agent.component.docs_generator import DocGenerator, DocGeneratorParam
from agent.component.message import Message
from api.db.db_models import LLM, Conversation, Dialog, LLMFactories, TenantLLM, UserCanvas, UserCanvasVersion, get_db
from api.db.services.tenant_llm_service import TenantLLMService
from tests.support.agent_update_release import release_api as release_api
from tests.support.runtime_upload import read_object
from tests.support.runtime_upload import runtime_upload_api as runtime_upload_api
from tests.support.web_consumer import tsx_runner

pytestmark = pytest.mark.external_consumer


def test_actual_web_chat_model_types_save_and_readback(runtime_upload_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, record_property: Callable[[str, object], None]) -> None:
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
    checkout = Path(os.environ["WEB_COMPLETION_CHECKOUT"])
    script = Path(__file__).resolve().parents[1] / "support/chat_model_web_consumer.mts"
    try:
        with sessions() as db:
            db.merge(LLMFactories(name="Ollama", tags="LLM", status="1"))
            for name, kind in [("vision", "image2text"), ("text", "chat")]:
                db.add(TenantLLM(tenant_id=env["owners"][0], llm_factory="Ollama", llm_name=name, mdl_type=kind, api_key="controlled-no-provider", status="1"))
            db.commit()
        completed = subprocess.run(
            [str(tsx_runner(checkout)), "--tsconfig", str(checkout / "tsconfig.app.json"), str(script)],
            cwd=checkout,
            env={**os.environ, "DOMAIN_ACCEPTANCE_BASE": env["base"], "DOMAIN_ACCEPTANCE_TOKEN": env["jwt"]},
            capture_output=True,
            text=True,
            timeout=90,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        result = json.loads(completed.stdout.splitlines()[-1])
        with sessions() as db:
            for identifier, kind in zip(result["ids"], ["image2text", "chat"], strict=True):
                row = db.get(Dialog, identifier)
                assert row is not None and row.tenant_id == env["owners"][0]
                assert row.llm_setting == {"temperature": 0, "model_type": kind}
                assert row.prompt_config["quote"] is False
        record_property("actual_web_chat_model_sql_readback", json.dumps(result))
    finally:
        with sessions() as db:
            ids = list(db.scalars(select(Dialog.id).where(Dialog.tenant_id == env["owners"][0])))
            db.execute(delete(Conversation).where(Conversation.dialog_id.in_(ids)))
            db.execute(delete(Dialog).where(Dialog.tenant_id == env["owners"][0]))
            db.execute(delete(TenantLLM).where(TenantLLM.tenant_id.in_(env["owners"])))
            db.commit()
            assert db.scalar(select(Dialog.id).where(Dialog.tenant_id == env["owners"][0])) is None


def run_web(env: dict[str, Any], mode: str, **values: str) -> dict[str, Any]:
    checkout = Path(os.environ.get("WEB_COMPLETION_CHECKOUT", "/Users/xldu/project/web"))
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
    factory = "Ollama"
    with Session(env["engine"]) as db:
        db.merge(LLMFactories(name=factory, tags="LLM", status="1"))
        db.merge(LLM(fid=factory, llm_name="combined-fixture", mdl_type="chat", max_tokens=8192, tags="LLM", is_tools=True))
        db.commit()
    monkeypatch.setattr(settings, "FACTORY_LLM_INFOS", [{"name": factory}])
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
            assert body["model"] == "combined-fixture"
            assert self.path == "/api/chat"
            requests.append(self.path)
            result = {"model": "combined-fixture", "message": {"role": "assistant", "content": "answer"}, "done": True, "eval_count": 3, "prompt_eval_count": 2}
            payload = (json.dumps(result) + "\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = run_web(env, "model", DOMAIN_ACCEPTANCE_PROVIDER=f"http://127.0.0.1:{server.server_port}")
        assert result["flags"] == [False, True, False] and len(requests) == 3
        with Session(env["engine"]) as db:
            row = db.scalars(select(TenantLLM).where(TenantLLM.tenant_id == env["owners"][0], TenantLLM.llm_factory == factory, TenantLLM.llm_name == "combined-fixture")).one()
            assert json.loads(row.api_key) == {"api_key": "fixture-key", "is_tools": False}
        record_property("web_consumer_readback", json.dumps({**result, "database": env["engine"].url.database, "stored_is_tools": False, "provider_http_requests": len(requests)}))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)
        assert not thread.is_alive()
        with Session(env["engine"]) as db:
            db.execute(delete(TenantLLM).where(TenantLLM.tenant_id.in_(env["owners"])))
            db.commit()
            assert db.scalar(select(TenantLLM.id).where(TenantLLM.tenant_id.in_(env["owners"]))) is None


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
