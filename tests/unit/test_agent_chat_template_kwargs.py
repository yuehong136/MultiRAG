"""Request overrides reach the real Canvas, LLM and model facade per turn."""

import json
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.orm import Session

from agent.canvas import Canvas
from agent.component import llm
from api.db.services.llm_service import LLMBundle
from core.llm.chat import Base, LiteLLMBase
from core.utils.redis_conn import REDIS_CONN


class Model:
    max_length = 4096

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def async_chat(self, system: str, history: list[dict[str, Any]], gen_conf: dict[str, Any], **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return '{"answer": 0}'

    async def async_chat_streamly_delta(self, system: str, history: list[dict[str, Any]], gen_conf: dict[str, Any], **kwargs: Any) -> AsyncIterator[str]:
        self.calls.append(kwargs)
        yield "answer"


@pytest.fixture
def workflow(monkeypatch: pytest.MonkeyPatch, db: Session) -> Iterator[tuple[list[Canvas], Model]]:
    @contextmanager
    def model_db() -> Iterator[Session]:
        yield db

    model = Model()
    monkeypatch.setattr(llm, "db_connection", model_db)
    monkeypatch.setattr(llm, "resolve_model_type", lambda *args: ["chat"])
    monkeypatch.setattr(llm, "get_model_config_by_type_and_name", lambda *args: object())
    monkeypatch.setattr(llm, "LLMBundle", lambda *args, **kwargs: model)
    monkeypatch.setattr(Canvas, "is_canceled", lambda self: False)
    monkeypatch.setattr(REDIS_CONN, "delete", lambda *args: None)
    canvases: list[Canvas] = []
    yield canvases, model
    for canvas in canvases:
        canvas._thread_pool.shutdown(wait=True)


def load(canvases: list[Canvas], mode: str, source: str | None = None) -> Canvas:
    structured = {"structured": {"type": "object", "properties": {"answer": {"type": "number"}}}} if mode == "structured" else {}
    nodes = ["begin", "LLM:test"] + (["Message:test"] if mode == "stream" else [])
    params = [
        {"prologue": ""},
        {"llm_id": "model", "sys_prompt": "system", "prompts": [{"role": "user", "content": "{sys.query}"}], "outputs": structured, "max_retries": 0},
        {"content": ["{LLM:test@content}"]},
    ]
    components = {
        node: {"obj": {"component_name": "Begin" if node == "begin" else node.split(":")[0], "params": params[i]}, "upstream": nodes[i - 1 : i] if i else [], "downstream": nodes[i + 1 : i + 2]}
        for i, node in enumerate(nodes)
    }
    canvas = Canvas(source or json.dumps({"components": components, "path": [], "history": [], "retrieval": []}), tenant_id="unit-owner")
    canvases.append(canvas)
    return canvas


@pytest.mark.parametrize("mode", ["normal", "structured", "stream"])
@pytest.mark.parametrize("value", [{}, {"enable_thinking": False, "limit": 0, "nested": {"empty": []}}])
async def test_real_workflow_preserves_empty_and_false_then_clears_next_turn(workflow: tuple[list[Canvas], Model], mode: str, value: dict[str, Any]) -> None:
    canvases, model = workflow
    canvas = load(canvases, mode)
    events = [event async for event in canvas.run(query="first", chat_template_kwargs=value)]
    assert events[-1]["event"] == "workflow_finished" and not canvas.error
    assert model.calls == [{"chat_template_kwargs": value}]
    if mode == "structured":
        assert canvas.get_component_obj("LLM:test").output("structured") == {"answer": 0}
    restored = load(canvases, mode, str(canvas))
    assert restored.globals["sys.chat_template_kwargs"] == value
    events = [event async for event in restored.run(query="second")]
    assert events[-1]["event"] == "workflow_finished" and not restored.error
    assert model.calls[-1] == {} and restored.globals["sys.chat_template_kwargs"] is None
    restored.globals["sys.chat_template_kwargs"] = value
    restored.reset()
    assert restored.globals["sys.chat_template_kwargs"] is None


async def test_runtime_copies_request_and_rejects_invalid_before_history(workflow: tuple[list[Canvas], Model]) -> None:
    canvases, model = workflow
    canvas = load(canvases, "normal")
    value = {"nested": {"zero": 0}}
    events = canvas.run(query="first", chat_template_kwargs=value)
    assert (await anext(events))["event"] == "workflow_started"
    value["nested"]["zero"] = 1
    [event async for event in events]
    assert model.calls[-1] == {"chat_template_kwargs": {"nested": {"zero": 0}}}
    before = list(canvas.history)
    for invalid in ["{}", "broken", [], False, 0]:
        with pytest.raises(ValueError, match="JSON object"):
            [event async for event in canvas.run(query="invalid", chat_template_kwargs=invalid)]
        assert canvas.history == before


def bundle(model: Any) -> LLMBundle:
    value = object.__new__(LLMBundle)
    value.mdl, value.is_tools, value.langfuse, value.verbose_tool_use = model, False, None, True
    return value


@pytest.mark.parametrize("tools", [False, True])
@pytest.mark.parametrize("backend", ["base", "litellm"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("value", [{}, {"enable_thinking": False, "limit": 0}])
async def test_real_facade_encodes_template_options_at_sdk_boundary(monkeypatch: pytest.MonkeyPatch, backend: str, stream: bool, tools: bool, value: dict[str, Any]) -> None:
    calls: list[dict[str, Any]] = []

    class Response:
        choices = [SimpleNamespace(message=SimpleNamespace(content="answer"), finish_reason="stop")]
        usage = None

        async def __aenter__(self) -> "Response":
            return self

        async def __aexit__(self, *args: Any) -> None:
            pass

        def __aiter__(self) -> AsyncIterator[Any]:
            async def chunks() -> AsyncIterator[Any]:
                yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="answer", reasoning_content=None), finish_reason="stop")], usage=None)

            return chunks()

    async def create(**kwargs: Any) -> Response:
        calls.append(kwargs)
        return Response()

    if backend == "base":
        model = object.__new__(Base)
        model.model_name, model.max_retries = "fixture", 0
        model.async_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    else:
        model = LiteLLMBase("fixture", "fixture", provider="OpenAI", max_retries=0)
        monkeypatch.setattr("core.llm.chat.litellm.acompletion", create)
    monkeypatch.setattr("core.llm.chat.total_token_count_from_response", lambda _: 0)
    monkeypatch.setattr("core.llm.chat.num_tokens_from_string", lambda _: 0)
    facade = bundle(model)
    model.max_rounds = 1
    model.tools = [{"type": "function", "function": {"name": "unused"}}]
    facade.is_tools = model.is_tools = tools
    if stream:
        assert [part async for part in facade.async_chat_streamly_delta("system", [{"role": "user", "content": "query"}], {}, chat_template_kwargs=value)] == ["answer"]
    else:
        assert await facade.async_chat("system", [{"role": "user", "content": "query"}], {}, chat_template_kwargs=value) == "answer"
    assert len(calls) == 1
    assert calls[0]["extra_body"]["chat_template_kwargs"] == value
    assert "chat_template_kwargs" not in calls[0]


@pytest.mark.parametrize("stream", [False, True])
async def test_inherited_agent_generation_reads_canvas_options_without_explicit_kwargs(stream: bool) -> None:
    model = Model()
    component = object.__new__(llm.LLM)
    component._id = "inherited-agent"
    component._canvas = SimpleNamespace(globals={"sys.chat_template_kwargs": {"enable_thinking": False, "limit": 0}})
    component._param = SimpleNamespace(gen_conf=lambda: {})
    component.chat_mdl, component.imgs = model, []
    component.check_if_canceled = lambda operation: False
    messages = [{"role": "system", "content": "system"}, {"role": "user", "content": "query"}]
    if stream:
        assert [part async for part in component._generate_streamly(messages)] == ["answer"]
    else:
        assert await component._generate_async(messages) == '{"answer": 0}'
    assert model.calls == [{"chat_template_kwargs": {"enable_thinking": False, "limit": 0}}]
