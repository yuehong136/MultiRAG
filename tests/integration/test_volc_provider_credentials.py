"""Real HTTP/SQL proof for raw and structured Volc instance credentials."""

import asyncio
import hashlib
import json
import socket
import threading
from collections.abc import Iterator
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from api.db.db_models import TenantLLM, TenantModel, TenantModelInstance
from api.db.joint_services.tenant_model_service import get_model_config_by_id
from api.db.services import provider_verification
from api.db.services.llm_service import LLMBundle
from common import settings
from common.config_utils import CONFIGS
from tests.integration.test_provider_instances import REAL_VERIFY_MODEL
from tests.integration.test_provider_instances import http as http
from tests.integration.test_provider_instances import owned_engine as owned_engine
from tests.support.resource_journal import ResourceJournal

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def register_postgresql_endpoint() -> None:
    config = CONFIGS["postgresql"]
    ResourceJournal("volc-postgresql-input").event(
        "borrow-postgresql", endpoint={key: config[key] for key in ("host", "port", "user", "dbname")}, config_sha256=hashlib.sha256(Path("configs/service_conf.yaml").read_bytes()).hexdigest()
    )


@pytest.fixture
def provider_http() -> Iterator[tuple[str, list[tuple[str, str, str]]]]:
    journal = ResourceJournal("volc-http")
    seen: list[tuple[str, str, str]] = []

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            return

        def do_POST(self) -> None:
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append((self.path, self.headers["Authorization"], payload["model"]))
            if self.path.endswith("/embeddings/multimodal"):
                data = {"data": {"embedding": [0.25, 0.75]}, "usage": {"total_tokens": 0}}
            else:
                data = {
                    "id": "fixture",
                    "object": "chat.completion",
                    "created": 1,
                    "model": payload["model"],
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "fixture OK"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                }
            content = json.dumps(data).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

    address = ("127.0.0.1", 30000 + int(uuid4().hex[:4], 16) % 20000)
    journal.event("reserve-listener", address=list(address))
    with ExitStack() as cleanup:
        server = ThreadingHTTPServer(address, Provider, bind_and_activate=False)
        cleanup.callback(server.server_close)
        server.server_bind()
        server.server_activate()
        thread = threading.Thread(target=server.serve_forever, name="volc-fixture")
        thread.start()
        cleanup.callback(thread.join, 5)
        cleanup.callback(server.shutdown)
        yield f"http://127.0.0.1:{address[1]}/custom/v3", seen
    with socket.socket() as reader:
        assert reader.connect_ex(address) != 0
    assert not thread.is_alive()
    journal.event("listener-cleaned", address=list(address), absent=True, thread_stopped=True)


@pytest.mark.parametrize("capability", ["chat", "image2text", "embedding"])
def test_verify_atomic_create_and_numeric_bundle_share_key_semantics(http: tuple[Any, ...], provider_http: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch, capability: str) -> None:
    client, owner, engine, _module = http
    base, seen = provider_http
    monkeypatch.setattr(settings, "FACTORY_LLM_INFOS", [{"name": "VolcEngine", "llm": []}])
    monkeypatch.setattr(provider_verification, "verify_model", REAL_VERIFY_MODEL)
    for name, key, expected in (("raw", "fixture-raw-ark", "display-model"), ("envelope", json.dumps({"ark_api_key": "fixture-envelope-ark", "endpoint_id": "ep-instance"}), "ep-instance")):
        if capability == "embedding":
            expected = "display-model"
        candidate = {"api_key": key, "base_url": base, "region": "cn-beijing", "model_ids": [], "custom_model": {"model_name": "display-model", "model_types": [capability], "max_tokens": 0}}
        before = len(seen)
        response = client.post("/api/v1/providers/VolcEngine/connection", json=candidate)
        assert response.status_code == 200 and response.json()["data"]["verified"] is True, response.text
        with Session(engine) as reader:
            assert reader.scalar(select(func.count()).select_from(TenantModelInstance)) == (0 if name == "raw" else 1)
        response = client.post("/api/v1/providers/VolcEngine/instances", json={**candidate, "instance_name": name})
        assert response.status_code == 200 and response.json()["code"] == 0, response.text
        assert response.json()["data"]["has_api_key"] is True and "ark" not in response.text
        model = client.get(f"/api/v1/providers/VolcEngine/instances/{name}/models").json()["data"]["items"][0]
        identifier = model["id"]
        assert type(identifier) is int and model["max_tokens"] == 0
        with Session(engine) as reader:
            row = reader.scalar(select(TenantModelInstance).where(TenantModelInstance.instance_name == name))
            assert row.api_key == key and row.region == "cn-beijing" and row.base_url == base
            config = get_model_config_by_id(reader, identifier, tenant_id=owner, model_type=capability)
            with pytest.raises(LookupError, match="not found"):
                get_model_config_by_id(reader, identifier, tenant_id="foreign-owner", model_type=capability)
            bundle = LLMBundle(reader, owner, config, max_retries=0)
            bundle.db = None

        async def infer() -> None:
            try:
                assert bundle.mdl.model_name == expected
                if capability == "embedding":
                    vectors, tokens = bundle.mdl.encode(["hello"])
                    assert vectors.tolist() == [[0.25, 0.75]] and tokens == 0
                else:
                    answer, tokens = await bundle.mdl.async_chat("", [{"role": "user", "content": "hello"}], {})
                    assert answer == "fixture OK" and tokens == 0
            finally:
                await bundle.aclose()

        asyncio.run(infer())
        expected_key = "fixture-raw-ark" if name == "raw" else "fixture-envelope-ark"
        assert len(seen) == before + 3
        assert all(auth == "Bearer " + expected_key and model_name == expected for _path, auth, model_name in seen[before:])
    before = len(seen)
    for key in ('{"ark_api_key":"fixture-secret","ep_id":"a","endpoint_id":"b"}', '{"ark_api_key":"fixture-secret"', "[]", '{"ark_api_key":""}', {"ark_api_key": "fixture-secret"}):
        response = client.post("/api/v1/providers/VolcEngine/instances", json={**candidate, "instance_name": "invalid", "api_key": key})
        # Typed request-shape errors use 422; a credential rejected by the
        # provider probe uses the existing business-validation 400 contract.
        assert response.status_code == (422 if isinstance(key, dict) else 400)
        assert "fixture-secret" not in response.text
    assert len(seen) == before
    with Session(engine) as reader:
        assert reader.scalar(select(func.count()).select_from(TenantModelInstance)) == 2
        assert reader.scalar(select(func.count()).select_from(TenantModel)) == 2
        assert reader.scalar(select(func.count()).select_from(TenantLLM)) == 2
        assert all(tokens == 0 for tokens in reader.scalars(select(TenantLLM.used_tokens)))
