"""Real numeric registrations, Canvas invokes, chat/CV classes and controlled SDKs."""

import json
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from typing import Any
from uuid import uuid4

import httpx
import litellm
import pytest
from sqlalchemy import delete, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlalchemy.orm import Session

from agent.canvas import Canvas
from agent.component import agent_with_tools, llm
from api.db.db_models import TenantLLM, TenantModel, TenantModelInstance, TenantModelProvider
from api.db.services import tenant_llm_service
from core.llm import CvModel
from core.llm.chat import LiteLLMBase
from core.utils.redis_conn import REDIS_CONN
from tests.support.resource_journal import ResourceJournal

IMAGE = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aXioAAAAASUVORK5CYII="


class Tool:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def get_meta(self) -> dict[str, Any]:
        return {"type": "function", "function": {"name": "echo", "description": "Controlled echo", "parameters": {"type": "object", "properties": {"value": {"type": "string"}}}}}

    async def invoke_async(self, value: str) -> str:
        self.calls.append(value)
        return value


@pytest.fixture
async def routing(bootstrapped_engine: Engine, bootstrapped_async_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[dict[str, Any]]:
    engine, owner = bootstrapped_engine, uuid4().hex
    provider_id, instance_id = uuid4().hex, uuid4().hex
    journal = ResourceJournal("agent-numeric-model-routing")
    journal.event("reserve-sql-rows", database=engine.url.database, owner=owner, provider=provider_id, instance=instance_id)
    with Session(engine) as writer:
        writer.add(TenantModelProvider(id=provider_id, tenant_id=owner, provider_name="OpenAI", status="active"))
        writer.flush()
        writer.add(TenantModelInstance(id=instance_id, provider_id=provider_id, instance_name="controlled", api_key="fixture", base_url="http://fixture.invalid/v1", region="", status="active"))
        writer.flush()
        row = TenantLLM(tenant_id=owner, llm_factory="OpenAI", llm_name="routing-fixture", mdl_type="chat", api_key="fixture", api_base="http://fixture.invalid/v1", status="1", used_tokens=0)
        writer.add(row)
        writer.flush()
        identifier = row.id
        writer.add(TenantModel(id=identifier, instance_id=instance_id, model_name="routing-fixture", model_types=["chat", "image2text"], is_tools=True))
        writer.commit()
    with Session(engine) as reader:
        assert reader.get(TenantModel, identifier).model_types == ["chat", "image2text"]
        assert reader.get(TenantLLM, identifier).tenant_id == owner

    @contextmanager
    def scratch_session() -> Iterator[Session]:
        with Session(engine) as session:
            yield session

    monkeypatch.setattr(llm, "db_connection", scratch_session)
    monkeypatch.setattr(tenant_llm_service, "db_connection", scratch_session)
    monkeypatch.setattr(tenant_llm_service, "async_session_factory", async_sessionmaker(bootstrapped_async_engine, expire_on_commit=False))
    monkeypatch.setattr(Canvas, "is_canceled", lambda _self: False)
    monkeypatch.setattr(Canvas, "tool_use_callback", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(REDIS_CONN, "delete", lambda *_args: None)
    tool = Tool()
    monkeypatch.setattr(agent_with_tools.Agent, "_load_tool_obj", lambda *_args: tool)
    wire: list[dict[str, Any]] = []

    def response(body: dict[str, Any]) -> dict[str, Any]:
        wire.append(body)
        needs_tool = bool(body.get("tools")) and not any(message["role"] == "tool" for message in body["messages"])
        message: dict[str, Any] = {"role": "assistant", "content": "" if needs_tool else "answer"}
        if needs_tool:
            message["tool_calls"] = [{"id": "controlled-tool", "type": "function", "function": {"name": body["tools"][0]["function"]["name"], "arguments": '{"value":"called"}'}}]
        return {
            "id": "controlled",
            "object": "chat.completion",
            "created": 1,
            "model": "routing-fixture",
            "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if needs_tool else "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

    async def completion(**kwargs: Any) -> Any:
        return litellm.ModelResponse(**response(kwargs))

    async def sdk(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "fixture.invalid"
        return httpx.Response(200, json=response(json.loads(request.content)), request=request)

    monkeypatch.setattr(litellm, "acompletion", completion)
    transport = httpx.MockTransport(sdk)
    monkeypatch.setattr(httpx.AsyncClient, "_transport_for_url", lambda _self, _url: transport)
    canvases: list[Canvas] = []
    try:
        yield {"engine": engine, "owner": owner, "id": identifier, "provider": provider_id, "instance": instance_id, "wire": wire, "tool": tool, "canvases": canvases}
    finally:
        for canvas in canvases:
            for node in canvas.components.values():
                if isinstance(component := node["obj"], llm.LLM) and component.chat_mdl is not None:
                    await component.chat_mdl.aclose()
            canvas._thread_pool.shutdown(wait=True)
        await transport.aclose()
        with Session(engine) as writer:
            writer.execute(delete(TenantModel).where(TenantModel.id == identifier))
            writer.execute(delete(TenantLLM).where(TenantLLM.id == identifier))
            writer.execute(delete(TenantModelInstance).where(TenantModelInstance.id == instance_id))
            writer.execute(delete(TenantModelProvider).where(TenantModelProvider.id == provider_id))
            writer.commit()
        with Session(engine) as reader:
            absent = [reader.get(model, key) is None for model, key in [(TenantModel, identifier), (TenantLLM, identifier), (TenantModelInstance, instance_id), (TenantModelProvider, provider_id)]]
            assert all(absent)
            journal.event("sql-cleanup-independent-readback", absent=absent, database=engine.url.database)


def load(env: dict[str, Any], kind: str, owner: str | None = None) -> tuple[Canvas, llm.LLM]:
    params: dict[str, Any] = {
        "llm_id": str(env["id"]),
        "sys_prompt": "system",
        "prompts": [{"role": "user", "content": "{sys.query}"}],
        "visual_files_var": "sys.visual_files",
        "cite": False,
        "max_retries": 0,
    }
    if kind == "Agent":
        params.update(tools=[{"component_name": "Fixture", "params": {}}], max_rounds=2)
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
        tenant_id=owner or env["owner"],
    )
    env["canvases"].append(canvas)
    return canvas, canvas.get_component_obj("node")


@pytest.mark.parametrize("kind", ["LLM", "Agent"])
async def test_numeric_chat_and_cv_invocation_restores_text_and_reuses_tools(routing: dict[str, Any], kind: str) -> None:
    env = routing
    canvas, component = load(env, kind)
    initial = component.chat_mdl
    assert isinstance(initial.mdl, LiteLLMBase)
    canvas.globals["sys.visual_files"] = [IMAGE]
    [event async for event in canvas.run(query="image")]
    assert not canvas.error and component.output("content") == "answer"
    vision = component.chat_mdl
    assert isinstance(vision.mdl, CvModel["OpenAI"]) and vision.db is None
    assert initial._provider_closed
    first_request = env["wire"][0]
    assert IMAGE in json.dumps(first_request["messages"])
    if kind == "Agent":
        assert vision.mdl.max_rounds == vision.mdl._tool_chat_model.max_rounds == 2
        assert vision.verbose_tool_use is False and env["tool"].calls == ["called"]
    canvas.globals["sys.visual_files"] = []
    [event async for event in canvas.run(query="text")]
    assert not canvas.error and component.output("content") == "answer"
    assert isinstance(component.chat_mdl.mdl, LiteLLMBase) and component.chat_mdl.db is None
    assert vision._provider_closed and vision.mdl.client.is_closed() and vision.mdl.async_client.is_closed()
    text = component.chat_mdl
    [event async for event in canvas.run(query="same type")]
    assert not canvas.error and component.chat_mdl is text
    with Session(env["engine"]) as reader:
        assert reader.get(TenantLLM, env["id"]).used_tokens > 0
        assert reader.get(TenantModel, env["id"]).model_types == ["chat", "image2text"]
    if kind == "Agent":
        assert env["tool"].calls == ["called"] * 3
    with Session(env["engine"]) as writer:
        writer.get(TenantModelInstance, env["instance"]).api_key = "fixture-rotated"
        writer.get(TenantModel, env["id"]).is_tools = False
        writer.commit()
    [event async for event in canvas.run(query="configuration changed")]
    assert not canvas.error and component.chat_mdl is not text and text._provider_closed
    assert env["wire"][-1]["api_key"] == "fixture-rotated"
    assert not env["wire"][-1].get("tools")
    if kind == "Agent":
        assert env["tool"].calls == ["called"] * 3


@pytest.mark.parametrize("kind", ["LLM", "Agent"])
@pytest.mark.parametrize("denial", ["foreign", "inactive-model", "inactive-instance", "inactive-provider", "unsupported"])
async def test_numeric_denials_make_zero_provider_and_tool_calls(routing: dict[str, Any], kind: str, denial: str) -> None:
    env = routing
    canvas, component = load(env, kind)
    if denial == "foreign":
        canvas._tenant_id = uuid4().hex
    else:
        with Session(env["engine"]) as writer:
            if denial == "inactive-model":
                writer.get(TenantLLM, env["id"]).status = "0"
            elif denial == "inactive-instance":
                writer.get(TenantModelInstance, env["instance"]).status = "inactive"
            elif denial == "inactive-provider":
                writer.get(TenantModelProvider, env["provider"]).status = "inactive"
            else:
                writer.get(TenantModel, env["id"]).model_types = ["embedding"]
                writer.get(TenantLLM, env["id"]).mdl_type = "embedding"
            writer.commit()
    canvas.globals["sys.visual_files"] = [IMAGE]
    [event async for event in canvas.run(query="rejected")]
    assert canvas.error and env["wire"] == [] and env["tool"].calls == []
    with Session(env["engine"]) as reader:
        assert reader.scalar(select(TenantLLM.used_tokens).where(TenantLLM.id == env["id"])) == 0
