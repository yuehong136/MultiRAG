"""Current-turn images choose a declared capability without losing tools or clients."""

import asyncio
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from sqlalchemy.orm import Session

from agent.canvas import Canvas
from agent.component import agent_with_tools, llm
from api.db.services.llm_service import LLMBundle
from api.db.services.tenant_llm_service import TenantLangfuseService, TenantLLMService
from core.utils.redis_conn import REDIS_CONN

IMAGE = "data:image/png;base64,aW1hZ2U="


class Provider:
    def __init__(self, capability: str, options: dict[str, Any]) -> None:
        self.capability = capability
        self.options = options
        self.is_tools = False
        self.session: Any = None
        self.metadata: list[dict[str, Any]] = []
        self.calls: list[dict[str, Any]] = []
        self.closed = 0

    def bind_tools(self, session: Any, metadata: list[dict[str, Any]]) -> None:
        self.is_tools, self.session, self.metadata = True, session, metadata

    async def async_chat(self, system: str, history: list[dict[str, Any]], gen_conf: dict[str, Any], **kwargs: Any) -> tuple[str, int]:
        assert not self.closed
        self.calls.append({"tools": False, **kwargs})
        return "answer", 0

    async def async_chat_with_tools(self, system: str, history: list[dict[str, Any]], gen_conf: dict[str, Any], **kwargs: Any) -> tuple[str, int]:
        assert not self.closed and self.session is not None and self.metadata
        self.calls.append({"tools": True, **kwargs})
        assert await self.session.tool_call_async(self.metadata[0]["function"]["name"], {"value": "called"}) == "called"
        return "answer", 0

    async def aclose(self) -> None:
        self.closed += 1


class Tool:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def get_meta(self) -> dict[str, Any]:
        return {"type": "function", "function": {"name": "echo", "description": "Fixture echo", "parameters": {"type": "object", "properties": {"value": {"type": "string"}}}}}

    async def invoke_async(self, value: str) -> str:
        self.calls.append(value)
        return value


@pytest.fixture
def model_env(monkeypatch: pytest.MonkeyPatch, db: Session) -> Iterator[dict[str, Any]]:
    env: dict[str, Any] = {"types": ["chat", "image2text"], "providers": [], "canvases": [], "tool": Tool(), "denied": False}

    @contextmanager
    def model_db() -> Iterator[Session]:
        yield db

    def capabilities(session: Session, owner: str, reference: str) -> list[str]:
        assert session is db and owner == "routing-owner" and reference == "71"
        if env["denied"]:
            raise LookupError("Tenant model not found")
        return env["types"]

    def configuration(session: Session, owner: str, capability: str, reference: str) -> dict[str, Any]:
        assert capability in capabilities(session, owner, reference)
        return {"id": 71, "tenant_id": owner, "llm_factory": "Fixture", "llm_name": "model", "mdl_type": capability, "api_key": "fixture", "is_tools": True, "max_tokens": 4096}

    def provider(cls: type[TenantLLMService], session: Session, owner: str, config: dict[str, Any], **kwargs: Any) -> Provider:
        value = Provider(config["mdl_type"], kwargs)
        env["providers"].append(value)
        return value

    monkeypatch.setattr(llm, "db_connection", model_db)
    monkeypatch.setattr(llm, "resolve_model_type", capabilities)
    monkeypatch.setattr(llm, "get_model_config_by_type_and_name", configuration)
    monkeypatch.setattr(TenantLLMService, "model_instance", classmethod(provider))
    monkeypatch.setattr(TenantLangfuseService, "filter_by_tenant", classmethod(lambda *_args, **_kwargs: None))
    monkeypatch.setattr(agent_with_tools.Agent, "_load_tool_obj", lambda *_args: env["tool"])
    monkeypatch.setattr(Canvas, "is_canceled", lambda _self: False)
    monkeypatch.setattr(Canvas, "tool_use_callback", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(REDIS_CONN, "delete", lambda *_args: None)
    yield env
    for canvas in env["canvases"]:
        canvas._thread_pool.shutdown(wait=True)


def load(env: dict[str, Any], kind: str) -> tuple[Canvas, llm.LLM]:
    params: dict[str, Any] = {
        "llm_id": "71",
        "sys_prompt": "system",
        "prompts": [{"role": "user", "content": "{sys.query}"}],
        "visual_files_var": "sys.visual_files",
        "cite": False,
        "max_retries": 0,
        "delay_after_error": 0.25,
    }
    if kind == "Agent":
        params.update(tools=[{"component_name": "Fixture", "params": {}}], max_rounds=3)
    canvas = Canvas(
        json.dumps(
            {
                "components": {
                    "begin": {"obj": {"component_name": "Begin", "params": {"prologue": ""}}, "upstream": [], "downstream": ["node"]},
                    "node": {"obj": {"component_name": kind, "params": params}, "upstream": ["begin"], "downstream": []},
                },
                "path": [],
                "history": [],
                "retrieval": [],
            }
        ),
        tenant_id="routing-owner",
    )
    env["canvases"].append(canvas)
    return canvas, canvas.get_component_obj("node")


async def run(canvas: Canvas, images: list[str]) -> None:
    canvas.globals["sys.visual_files"] = images
    events = [event async for event in canvas.run(query="question")]
    assert events[-1]["event"] == "workflow_finished" and not canvas.error


@pytest.mark.parametrize("kind", ["LLM", "Agent"])
@pytest.mark.parametrize("capabilities", [["chat"], ["image2text"], ["image2text", "chat"]], ids=["chat-only", "vision-only", "multi"])
async def test_real_invoke_routes_each_turn_reuses_bundle_and_preserves_tool_options(model_env: dict[str, Any], kind: str, capabilities: list[str]) -> None:
    env = model_env
    env["types"] = capabilities
    canvas, component = load(env, kind)
    try:
        await run(canvas, [IMAGE, IMAGE])
        selected = "image2text" if "image2text" in capabilities else "chat"
        first = component.chat_mdl
        assert first.llm_type == selected and first.db is None
        assert first.mdl.calls == [{"tools": kind == "Agent", "images": [IMAGE]}]
        await run(canvas, [IMAGE])
        assert component.chat_mdl is first and len(first.mdl.calls) == 2
        await run(canvas, [])
        final = component.chat_mdl
        assert final.llm_type == ("chat" if "chat" in capabilities else "image2text")
        assert final.mdl.calls[-1] == {"tools": kind == "Agent"}
        assert component.imgs == []
        expected_builds = 3 if len(capabilities) > 1 else 1
        assert len(env["providers"]) == expected_builds
        assert all(provider.closed == (provider is not final.mdl) for provider in env["providers"])
        if kind == "Agent":
            assert env["tool"].calls == ["called"] * 3
            for provider in env["providers"]:
                assert provider.options["max_rounds"] == 3
                assert provider.options["verbose_tool_use"] is False
                assert provider.options["max_retries"] == 0 and provider.options["retry_interval"] == 0.25
    finally:
        if component.chat_mdl is not None:
            await component.chat_mdl.aclose()


@pytest.mark.parametrize("kind", ["LLM", "Agent"])
async def test_revoked_registration_fails_before_any_provider_or_tool_call(model_env: dict[str, Any], kind: str) -> None:
    canvas, component = load(model_env, kind)
    model_env["denied"] = True
    try:
        canvas.globals["sys.visual_files"] = []
        [event async for event in canvas.run(query="question")]
        assert canvas.error
        assert all(not provider.calls for provider in model_env["providers"])
        assert model_env["tool"].calls == []
    finally:
        if component.chat_mdl is not None:
            await component.chat_mdl.aclose()


def test_thoughts_does_not_reconstruct_or_switch_model(model_env: dict[str, Any]) -> None:
    canvas, component = load(model_env, "LLM")
    canvas.globals.update({"sys.query": "question", "sys.visual_files": [IMAGE]})
    initial = component.chat_mdl
    component.thoughts()
    component.thoughts()
    assert component.chat_mdl is initial and len(model_env["providers"]) == 1


async def test_cancelled_model_switch_finishes_construction_and_closes_old_bundle(model_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    _canvas, component = load(model_env, "LLM")
    previous = component.chat_mdl
    component.imgs = [IMAGE]
    started, resume = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()

    def build(*args: Any, **kwargs: Any) -> LLMBundle:
        value = LLMBundle(*args, **kwargs)
        loop.call_soon_threadsafe(started.set)
        assert resume.wait(timeout=3)
        return value

    monkeypatch.setattr(llm, "LLMBundle", build)
    task = asyncio.create_task(component._prepare_chat_model())
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        task.cancel()
        await asyncio.sleep(0)
        resume.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert component.chat_mdl is not previous and previous._provider_closed
        assert [provider.closed for provider in model_env["providers"]] == [1, 0]
    finally:
        resume.set()
        await asyncio.gather(task, return_exceptions=True)
        await component.chat_mdl.aclose()


async def test_old_client_close_failure_disposes_replacement_and_prevents_reuse(model_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    _canvas, component = load(model_env, "LLM")
    previous = component.chat_mdl
    close = previous.aclose

    async def failed_close() -> None:
        await close()
        raise RuntimeError("close failed")

    monkeypatch.setattr(previous, "aclose", failed_close)
    component.imgs = [IMAGE]
    with pytest.raises(RuntimeError, match="close failed"):
        await component._prepare_chat_model()
    assert component.chat_mdl is None
    assert [provider.closed for provider in model_env["providers"]] == [1, 1]
    assert all(not provider.calls for provider in model_env["providers"])
    await component._prepare_chat_model()
    assert component.chat_mdl is not previous
    await component.chat_mdl.aclose()


async def test_tool_binding_failure_closes_new_bundle_and_keeps_previous(model_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    _canvas, component = load(model_env, "Agent")
    previous = component.chat_mdl

    def failed_bind(bundle: LLMBundle) -> None:
        raise ValueError("tool binding failed")

    monkeypatch.setattr(component, "_configure_chat_model", failed_bind)
    component.imgs = [IMAGE]
    try:
        with pytest.raises(ValueError, match="tool binding failed"):
            await component._prepare_chat_model()
        assert component.chat_mdl is previous
        assert [provider.closed for provider in model_env["providers"]] == [0, 1]
        assert all(not provider.calls for provider in model_env["providers"])
    finally:
        await previous.aclose()
