"""Pinned Web discovery, active SDK probes and SQL against owned supplier HTTP."""

import json
import socket
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from api.db.db_models import TenantLLM, TenantModel, TenantModelInstance, TenantModelProvider
from api.db.model_contract import ModelRegistration
from api.db.services import model_provider_service, provider_verification
from common import settings
from tests.support.model_discovery_web import ExpectedDiscoveryModel, run_web_discovery
from tests.support.provider_instances import REAL_VERIFY_MODEL
from tests.support.provider_instances import http as http
from tests.support.provider_instances import owned_engine as owned_engine
from tests.support.resource_journal import ResourceJournal
from tests.support.web_consumer import pinned_web_checkout

pytestmark = pytest.mark.external_consumer
PROVIDERS = ("OpenAI-API-Compatible", "VLLM", "LM-Studio", "Xinference", "LocalAI", "OpenRouter", "VolcEngine")


def _inventory(provider: str, names: tuple[str, ...]) -> dict[str, Any]:
    if provider == "VolcEngine":
        return {
            "data": [
                {
                    "id": name,
                    "modalities": {"input_modalities": ["text", "image"], "output_modalities": ["text"]},
                    "features": {"tools": {"function_calling": True}},
                    "token_limits": {"max_input_token_length": 0},
                }
                for name in names
            ]
        }
    if provider == "OpenRouter":
        return {
            "data": [
                {
                    "id": name,
                    "architecture": {"input_modalities": ["text", "image"], "output_modalities": ["text"]},
                    "top_provider": {"max_completion_tokens": 0},
                    "supported_parameters": ["tools", "reasoning"],
                }
                for name in names
            ]
        }
    if provider == "Xinference":
        return {"data": [{"id": name, "model_type": "LLM", "model_ability": ["chat", "vision", "tools", "reasoning"], "context_length": 0} for name in names]}
    return {"object": "list", "data": [{"id": name, "max_tokens": 0} for name in names]}


@contextmanager
def _supplier_http(provider: str, names: tuple[str, ...], state: dict[str, Any], calls: list[dict[str, Any]], journal: ResourceJournal) -> Iterator[str]:
    class Supplier(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *_args: Any) -> None:
            return

        def reply(self, value: dict[str, Any], status: int = 200) -> None:
            content = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def authorized(self) -> bool:
            if self.headers.get("Authorization") == "Bearer fixture-discovery-key":
                return True
            state["errors"].append("unexpected Authorization header")
            self.reply({"error": {"message": "Unauthorized", "type": "authentication_error"}}, 401)
            return False

        def do_GET(self) -> None:
            if not self.authorized():
                return
            path = urlsplit(self.path)
            calls.append({"method": "GET", "path": self.path})
            if provider == "LocalAI" and path.path == "/api/tags":
                self.reply({"models": None if state["bad_inventory"] else [{"name": name + ":latest"} for name in names]})
            elif path.path == ("/api/v1/models" if provider == "OpenRouter" else "/api/v3/models" if provider == "VolcEngine" else "/v1/models"):
                if provider == "OpenRouter" and parse_qs(path.query) != {"output_modalities": ["all"]}:
                    state["errors"].append("OpenRouter modalities query missing")
                self.reply({"data": None} if state["bad_inventory"] else _inventory(provider, names))
            else:
                state["errors"].append("unexpected GET " + path.path)
                self.reply({"error": {"message": "Unexpected fixture path"}}, 404)

        def do_POST(self) -> None:
            if not self.authorized():
                return
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            path = urlsplit(self.path).path
            model = payload.get("model")
            calls.append({"method": "POST", "path": path, "model": model})
            if provider == "LocalAI" and path == "/api/show" and model in {name + ":latest" for name in names}:
                self.reply({"capabilities": ["completion", "vision", "tools", "thinking"], "model_info": {"general.context_length": 0}})
            elif path == ("/api/v1/chat/completions" if provider == "OpenRouter" else "/api/v3/chat/completions" if provider == "VolcEngine" else "/v1/chat/completions") and model in names:
                self.reply(
                    {
                        "id": "controlled-provider-probe",
                        "object": "chat.completion",
                        "created": 1,
                        "model": model,
                        "choices": [{"index": 0, "message": {"role": "assistant", "content": "" if state["bad_chat"] else "OK"}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                    }
                )
            else:
                state["errors"].append("unexpected POST " + path)
                self.reply({"error": {"message": "Unexpected fixture path"}}, 404)

    server: ThreadingHTTPServer | None = None
    thread: threading.Thread | None = None
    address = ("127.0.0.1", 0)
    try:
        for _attempt in range(32):
            address = ("127.0.0.1", 30000 + int(uuid4().hex[:4], 16) % 20000)
            journal.event("reserve-listener", address=list(address), provider=provider, boundary="controlled supplier HTTP")
            server = ThreadingHTTPServer(address, Supplier, bind_and_activate=False)
            try:
                server.server_bind()
                break
            except OSError:
                server.server_close()
                server = None
        assert server is not None, "No owned supplier listener available"
        server.server_activate()
        thread = threading.Thread(target=server.serve_forever, name="after5e-supplier-" + journal.run_id[:12])
        journal.event("reserve-thread", name=thread.name, listener=list(address))
        thread.start()
        yield f"http://{address[0]}:{address[1]}"
    finally:
        if server is not None:
            if thread is not None and thread.ident is not None:
                server.shutdown()
                thread.join(timeout=5)
            server.server_close()
        with socket.socket() as probe:
            probe.settimeout(1)
            absent = probe.connect_ex(address) != 0
        journal.event("supplier-cleanup-readback", address=list(address), listener_absent=absent, thread_absent=thread is None or not thread.is_alive(), calls=calls)
        assert absent and (thread is None or not thread.is_alive())


@pytest.mark.parametrize("provider", PROVIDERS)
def test_after5e_provider_discovery_web_sql(
    http: tuple[TestClient, str, Engine, ModuleType], provider: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, record_property: Callable[[str, object], None]
) -> None:
    client, owner, engine, _module = http
    checkout, web_sha = pinned_web_checkout()
    journal = ResourceJournal("after5e-provider-discovery-" + provider)
    names = tuple(f"vision-{index:03}" for index in range(125)) if provider == "OpenRouter" else ("first-vision", "second-vision")
    key = "fixture-discovery-key"
    has_tools = provider in {"Xinference", "LocalAI", "OpenRouter", "VolcEngine"}
    expected: dict[str, ExpectedDiscoveryModel] = {name: {"model_types": ["chat", "image2text"], "max_tokens": 0, "is_tools": has_tools} for name in names[:2]}
    state: dict[str, Any] = {"bad_inventory": False, "bad_chat": False, "errors": []}
    calls: list[dict[str, Any]] = []
    official_requests: list[dict[str, str]] = []
    for variable in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost,openrouter.ai,ark.cn-beijing.volces.com")
    monkeypatch.setattr(settings, "FACTORY_LLM_INFOS", [{"name": provider, "llm": []}])

    async def active_probe(name: str, credential: str, base: str, declaration: ModelRegistration) -> dict[str, Any]:
        from litellm.litellm_core_utils.logging_worker import GLOBAL_LOGGING_WORKER

        assert GLOBAL_LOGGING_WORKER._worker_task is None
        journal.event("reserve-sdk-logging-worker", provider=name, model=declaration.model_name)
        try:
            return await REAL_VERIFY_MODEL(name, credential, base, declaration)
        finally:
            await GLOBAL_LOGGING_WORKER.stop()
            await GLOBAL_LOGGING_WORKER.clear_queue()
            assert GLOBAL_LOGGING_WORKER._worker_task is None and not GLOBAL_LOGGING_WORKER._running_tasks
            journal.event("sdk-logging-worker-cleanup", stopped=True, drained=True)

    monkeypatch.setattr(provider_verification, "verify_model", active_probe)

    def counts() -> list[int]:
        with Session(engine) as reader:
            return [reader.scalar(select(func.count()).select_from(model)) for model in (TenantModelProvider, TenantModelInstance, TenantModel, TenantLLM)]

    with _supplier_http(provider, names, state, calls, journal) as supplier:
        base_url = "https://openrouter.ai/api/v1" if provider == "OpenRouter" else "https://ark.cn-beijing.volces.com/api/v3" if provider == "VolcEngine" else supplier + "/v1"
        if provider in {"OpenRouter", "VolcEngine"}:
            original_transport = httpx.AsyncHTTPTransport.handle_async_request
            destination = urlsplit(supplier)
            assert destination.hostname is not None and destination.port is not None

            async def controlled_transport(transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
                # All supplier HTTP remains real, but no public connection is made.
                assert request.url.scheme == "https" and request.url.host == ("openrouter.ai" if provider == "OpenRouter" else "ark.cn-beijing.volces.com") and request.url.port in {None, 443}
                assert request.url.path in ({"/api/v1/models", "/api/v1/chat/completions"} if provider == "OpenRouter" else {"/api/v3/models", "/api/v3/chat/completions"})
                identity = {"method": request.method, "official_origin": "https://" + str(request.url.host), "path": request.url.path}
                official_requests.append(identity)
                journal.event("controlled-supplier-wire", **identity, destination=supplier)
                request.url = request.url.copy_with(scheme="http", host=destination.hostname, port=destination.port)
                return await original_transport(transport, request)

            monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", controlled_transport)

        body = {"api_key": key, "base_url": base_url, "region": ""}
        from api.utils.api_utils import async_current_user

        auth_override = client.app.dependency_overrides.pop(async_current_user)
        try:
            denied = client.post(f"/api/v1/providers/{provider}/models/discovery", json=body, headers={"Authorization": "Bearer invalid-fixture-token"})
            assert denied.status_code == 401 and not calls and counts() == [0, 0, 0, 0]
        finally:
            client.app.dependency_overrides[async_current_user] = auth_override
        response = client.post(f"/api/v1/providers/{provider}/models/discovery", json=body)
        assert response.status_code == 200 and response.headers["Cache-Control"] == "no-store", response.text
        inventory = response.json()["data"]
        assert inventory["total"] == len(names) and len(inventory["items"]) == min(100, len(names))
        assert all("id" not in item and "status" not in item for item in inventory["items"])
        if provider == "OpenRouter":
            second_page = client.post(f"/api/v1/providers/{provider}/models/discovery?page=2", json=body).json()["data"]
            assert second_page["total"] == 125 and len(second_page["items"]) == 25
            all_items = inventory["items"] + second_page["items"]
            assert {item["model_name"] for item in all_items} == set(names)
            before = len(calls)
            excessive = {
                **body,
                "instance_name": "excessive",
                "model_ids": [],
                "custom_models": [{key: item[key] for key in ("model_name", "model_types", "max_tokens", "is_tools")} for item in all_items[:101]],
            }
            assert client.post(f"/api/v1/providers/{provider}/instances", json=excessive).status_code == 422
            assert len(calls) == before and counts() == [0, 0, 0, 0]
        items = inventory["items"][:2]
        candidate = {**body, "model_ids": [], "custom_models": [{key: item[key] for key in ("model_name", "model_types", "max_tokens", "is_tools")} for item in items]}
        assert counts() == [0, 0, 0, 0]
        state["bad_inventory"] = True
        failed_inventory = client.post(f"/api/v1/providers/{provider}/models/discovery", json=body)
        assert failed_inventory.status_code == 502 and "fixture-discovery-key" not in failed_inventory.text
        assert counts() == [0, 0, 0, 0]
        state["bad_inventory"] = False
        state["bad_chat"] = True
        assert client.post(f"/api/v1/providers/{provider}/connection", json=candidate).status_code == 400
        assert counts() == [0, 0, 0, 0]
        state["bad_chat"] = False
        add_model = model_provider_service.add_model

        async def fail_second(db: AsyncSession, tenant_id: str, provider_name: str, instance_name: str, registration: ModelRegistration) -> dict[str, Any]:
            if registration.model_name == names[1]:
                raise ValueError("Controlled second model failure")
            return await add_model(db, tenant_id, provider_name, instance_name, registration)

        with monkeypatch.context() as patch:
            patch.setattr(model_provider_service, "add_model", fail_second)
            assert client.post(f"/api/v1/providers/{provider}/instances", json={**candidate, "instance_name": "rolled-back"}).status_code == 400
        assert counts() == [0, 0, 0, 0]
        run_web_discovery(
            client,
            owner,
            engine,
            base_url,
            journal,
            checkout,
            tmp_path,
            provider_name=provider,
            api_key=key,
            expected_models=expected,
            expected_page_lengths=(100, 25) if provider == "OpenRouter" else (),
            controlled_transport=provider in {"OpenRouter", "VolcEngine"},
            invalid_base_url="http://arbitrary.invalid",
        )
        assert counts() == [1, 0, 0, 0], "Only the registered parent provider may remain until scratch DB DROP"
        assert calls and any(call["path"].endswith("/chat/completions") for call in calls) and not state["errors"]
        if provider == "OpenRouter":
            assert {item["path"] for item in official_requests} == {"/api/v1/models", "/api/v1/chat/completions"}
        if provider == "VolcEngine":
            assert {item["path"] for item in official_requests} == {"/api/v3/models", "/api/v3/chat/completions"}
        journal.event(
            "after5e-provider-consumer-readback", provider=provider, web_sha=web_sha, database=engine.url.database, discovery_no_writes=True, failure_zero_writes=True, retained_parent_only=True
        )
        record_property("web_sha", web_sha)
        record_property("provider", provider)
