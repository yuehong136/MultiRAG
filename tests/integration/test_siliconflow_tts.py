"""Real speech HTTP, typed provider verification and committed numeric defaults."""

import errno
import json
import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from api.db.db_models import TenantLLM, TenantModel, TenantModelInstance, TenantModelProvider
from api.db.joint_services.tenant_model_service import get_model_config_by_id, get_tenant_default_model_by_type
from api.db.services import provider_verification
from api.db.services.llm_service import LLMBundle
from common import settings
from tests.support.provider_instances import REAL_VERIFY_MODEL, tenant
from tests.support.provider_instances import http as http
from tests.support.provider_instances import owned_engine as owned_engine
from tests.support.resource_journal import ResourceJournal

pytestmark = pytest.mark.integration
MODELS = ("FunAudioLLM/CosyVoice2-0.5B", "fnlp/MOSS-TTSD-v0.5")
AUDIO = b"ID3-controlled-speech-first-last"


@contextmanager
def speech_provider() -> Iterator[tuple[str, dict[str, Any]]]:
    journal = ResourceJournal("siliconflow-speech-http")
    state: dict[str, Any] = {"status": 200, "empty": False, "requests": []}

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *_args: Any) -> None:
            return

        def do_POST(self) -> None:
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            valid = (
                self.path == "/v1/audio/speech"
                and self.headers.get("Authorization") == "Bearer synthetic-speech-key"
                and payload.get("model") in MODELS
                and payload.get("voice") == str(payload["model"]) + ":anna"
                and payload.get("sample_rate") == 32000
                and payload.get("response_format") == "mp3"
                and payload.get("stream") is True
                and payload.get("speed") == 1
                and payload.get("gain") == 0
            )
            state["requests"].append({"path": self.path, "payload": payload, "contractValid": valid})
            status = state["status"] if valid else 400
            content = (b"" if state["empty"] else AUDIO) if status == 200 else b'{"message":"upstream-private-detail"}'
            self.send_response(status)
            self.send_header("Content-Type", "audio/mpeg" if status == 200 else "application/json")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            try:
                self.wfile.write(content[:8])
                self.wfile.flush()
                self.wfile.write(content[8:])
            except (BrokenPipeError, ConnectionResetError):
                # A successful connection probe intentionally stops at its first byte.
                pass

    for attempt in range(32):
        address = ("127.0.0.1", 30000 + int(uuid4().hex[:4], 16) % 20000)
        journal.event("reserve-listener", address=list(address), attempt=attempt + 1)
        server = ThreadingHTTPServer(address, Provider, bind_and_activate=False)
        try:
            server.server_bind()
            server.server_activate()
        except OSError as error:
            server.server_close()
            if error.errno != errno.EADDRINUSE or attempt == 31:
                raise
            continue
        break
    worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, name="siliconflow-speech-fixture")
    worker.start()
    try:
        yield f"http://{address[0]}:{address[1]}/v1", state
    finally:
        server.shutdown()
        worker.join(5)
        server.server_close()
        assert not worker.is_alive()
        with socket.socket() as reader:
            assert reader.connect_ex(address) != 0
        journal.event("listener-independent-readback", address=list(address), absent=True, workerJoined=True)


def model_counts(engine: Engine) -> list[int]:
    with Session(engine) as reader:
        return [reader.scalar(select(func.count()).select_from(model)) for model in (TenantModelProvider, TenantModelInstance, TenantModel, TenantLLM)]


@pytest.mark.parametrize("model_name", MODELS)
def test_siliconflow_catalog_verify_create_default_and_speech(http: tuple[TestClient, str, Engine, ModuleType], monkeypatch: pytest.MonkeyPatch, model_name: str) -> None:
    client, owner, engine, _module = http
    factories = json.loads((Path(__file__).parents[2] / "configs/llm_factories.json").read_text())["factory_llm_infos"]
    monkeypatch.setattr(settings, "FACTORY_LLM_INFOS", factories)
    monkeypatch.setattr(provider_verification, "verify_model", REAL_VERIFY_MODEL)
    declarations = client.get("/api/v1/providers/SILICONFLOW/models?page_size=100").json()["data"]["items"]
    declaration = next(item for item in declarations if item["model_name"] == model_name)
    assert declaration["model_types"] == ["tts"] and declaration["is_tools"] is False
    assert declaration["max_tokens"] == 26214400
    assert model_counts(engine) == [0, 0, 0, 0]

    with speech_provider() as (provider_base, state):
        candidate = {"api_key": "synthetic-speech-key", "base_url": provider_base, "region": "cn", "model_ids": [model_name]}
        verified = client.post("/api/v1/providers/SILICONFLOW/connection", json=candidate)
        assert verified.status_code == 200, verified.text
        assert verified.json()["data"] == {"verified": True, "model_name": model_name, "model_type": "tts"}
        assert model_counts(engine) == [0, 0, 0, 0]
        created = client.post("/api/v1/providers/SILICONFLOW/instances", json={"instance_name": "speech", **candidate})
        assert created.status_code == 200, created.text
        assert len(state["requests"]) == 2
        assert state["requests"][0]["payload"] == state["requests"][1]["payload"]
        assert model_counts(engine) == [1, 1, 1, 1]
        listed = client.get("/api/v1/models").json()["data"]["items"]
        assert len(listed) == 1
        model = listed[0]
        identifier = model["id"]
        assert isinstance(identifier, int) and identifier > 0
        assert model["name"] == model_name and model["reference"] == str(identifier)
        assert model["model_types"] == ["tts"] and model["is_tools"] is False
        assert client.patch("/api/v1/models/default", json={"model_type": "tts", "model_id": identifier}).status_code == 200
        assert client.patch("/api/v1/models/default", json={"model_type": "chat", "model_id": identifier}).status_code == 400

        with Session(engine) as reader:
            config = get_tenant_default_model_by_type(reader, owner, "tts")
            assert config["id"] == identifier and config["llm_name"] == model_name
            assert config["max_tokens"] == 26214400 and config["api_base"] == provider_base
            bundle = LLMBundle(reader, owner, config)
            assert b"".join(bundle.tts("Runtime speech")) == AUDIO

        with Session(engine) as writer:
            foreign = uuid4().hex
            writer.add(tenant(foreign))
            writer.commit()
        before_denial = len(state["requests"])
        with Session(engine) as reader, pytest.raises(LookupError):
            get_model_config_by_id(reader, identifier, tenant_id=foreign, model_type="tts")
        assert len(state["requests"]) == before_denial

        state["status"] = 401
        rejected = client.post("/api/v1/providers/SILICONFLOW/instances", json={"instance_name": "rejected", **candidate})
        assert rejected.status_code == 400 and "upstream-private-detail" not in rejected.text
        assert model_counts(engine) == [1, 1, 1, 1]
        state.update(status=200, empty=True)
        empty = client.post("/api/v1/providers/SILICONFLOW/connection", json=candidate)
        assert empty.status_code == 400
        assert model_counts(engine) == [1, 1, 1, 1]
        assert all(item["contractValid"] for item in state["requests"])
