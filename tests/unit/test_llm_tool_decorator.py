"""Trusted Python tool registration, dispatch, and lifetime contracts."""

import asyncio
import threading
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest
from beartype.roar import BeartypeCallHintParamViolation

from api.db.services.llm_service import LLMBundle
from core.llm.chat import Base, LiteLLMBase
from core.llm.cv import Base as VisionBase
from core.llm.tool_decorator import FunctionToolSession, tool


@tool
def sample(value: int | None, *, labels: list[str] | None = None, enabled: bool = False) -> dict[str, Any]:
    """Inspect a value.

    Args:
        value (int): The requested value,
            including zero.
        labels: Optional labels.
        enabled: Whether to enable processing.

    Returns:
        The result object.
    """
    return {"value": value, "labels": labels, "enabled": enabled}


def test_schema_required_defaults_modern_types_and_google_docs() -> None:
    schema = sample.openai_schema["function"]
    assert schema["description"] == "Inspect a value."
    params = schema["parameters"]
    assert params["required"] == ["value"]
    assert params["additionalProperties"] is False
    assert params["properties"]["value"] == {"anyOf": [{"type": "integer"}, {"type": "null"}], "description": "The requested value, including zero."}
    assert params["properties"]["labels"]["anyOf"] == [{"type": "array", "items": {"type": "string"}}, {"type": "null"}]
    assert params["properties"]["enabled"]["type"] == "boolean"


def test_rst_multiline_docs_and_schema_copy() -> None:
    @tool
    def lookup(query: str) -> str:
        """Find records.

        :param query: Search text,
            with multiple words.
        """
        return query

    session = FunctionToolSession([lookup])
    schemas = session.schemas
    assert schemas[0]["function"]["parameters"]["properties"]["query"]["description"] == "Search text, with multiple words."
    schemas[0]["function"]["name"] = "changed"
    assert session.schemas[0]["function"]["name"] == "lookup"


@pytest.mark.parametrize("bad", [True, -1, float("inf"), float("nan"), "1"])
def test_invalid_timeout_is_rejected_at_registration(bad: Any) -> None:
    with pytest.raises(BeartypeCallHintParamViolation if isinstance(bad, str) else ValueError, match="timeout"):
        tool(timeout=bad)


@pytest.mark.parametrize("fn", [lambda *args: None, lambda **kwargs: None, lambda value, /: None])
def test_non_keyword_signatures_rejected(fn: Callable[..., Any]) -> None:
    with pytest.raises(TypeError, match="named argument"):
        tool(fn)


async def test_argument_failures_have_zero_tool_side_effects() -> None:
    calls: list[str] = []

    @tool
    def action(value: str) -> str:
        calls.append(value)
        return value

    session = FunctionToolSession([action])
    for name, args, error in [("absent", {}, KeyError), ("action", [], BeartypeCallHintParamViolation), ("action", {}, TypeError), ("action", {"value": "x", "extra": 1}, TypeError)]:
        with pytest.raises(error):
            await session.tool_call_async(name, args)
    assert calls == []
    assert await session.tool_call_async("action", {"value": "ok"}) == "ok"


async def test_async_stays_on_current_loop_sync_runs_off_loop() -> None:
    current = threading.get_ident()
    loop = asyncio.get_running_loop()

    @tool
    async def asynchronous() -> bool:
        return asyncio.get_running_loop() is loop and threading.get_ident() == current

    @tool
    def synchronous() -> bool:
        return threading.get_ident() != current

    session = FunctionToolSession([asynchronous, synchronous])
    assert await session.tool_call_async("asynchronous", {})
    assert await session.tool_call_async("synchronous", {})
    with pytest.raises(RuntimeError, match="tool_call_async"):
        session.tool_call("asynchronous", {})


def test_sync_entry_can_dispatch_async_function() -> None:
    @tool
    async def asynchronous(value: str) -> str:
        return value

    assert FunctionToolSession([asynchronous]).tool_call("asynchronous", {"value": "ok"}) == "ok"


@pytest.mark.parametrize("timeout", [0, 0.01])
async def test_async_timeout_cancels_tool(timeout: float) -> None:
    stopped = asyncio.Event()

    @tool(timeout=timeout)
    async def waiting() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    with pytest.raises(TimeoutError):
        await FunctionToolSession([waiting]).tool_call_async("waiting", {}, request_timeout=1)
    if timeout:
        assert stopped.is_set()


async def test_timeout_unset_and_explicit_none_are_distinct() -> None:
    @tool
    async def inherited() -> str:
        await asyncio.sleep(0.01)
        return "done"

    @tool(timeout=None)
    async def unlimited() -> str:
        await asyncio.sleep(0.01)
        return "done"

    session = FunctionToolSession([inherited, unlimited])
    with pytest.raises(TimeoutError):
        await session.tool_call_async("inherited", {}, request_timeout=0)
    assert await session.tool_call_async("unlimited", {}, request_timeout=0) == "done"


async def test_cancellation_propagates_to_async_tool() -> None:
    started, stopped = asyncio.Event(), asyncio.Event()

    @tool
    async def waiting() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    task = asyncio.create_task(FunctionToolSession([waiting]).tool_call_async("waiting", {}))
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped.is_set()


@pytest.mark.parametrize("cancel", [False, True])
async def test_sync_worker_survives_waiter_timeout_or_cancellation(cancel: bool) -> None:
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    @tool(timeout=0.1)
    def waiting() -> str:
        started.set()
        try:
            if not release.wait(2):
                raise RuntimeError("Test did not release worker")
            return "done"
        finally:
            finished.set()

    task = asyncio.create_task(FunctionToolSession([waiting]).tool_call_async("waiting", {}))
    try:
        assert await asyncio.to_thread(started.wait, 1)
        if cancel:
            task.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else TimeoutError):
            await task
        assert not finished.is_set()
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 1)


@pytest.mark.parametrize("backend", [Base, LiteLLMBase])
def test_registration_replacement_is_atomic_and_empty_clears(backend: type[Any]) -> None:
    model = backend.__new__(backend)
    model.bind_tools([sample])
    original = model.toolcall_session
    assert model.is_tools and model.tools == original.schemas
    for bad in [[sample, {}], [sample, lambda: None], [sample, sample], [sample.openai_schema]]:
        with pytest.raises((TypeError, ValueError)):
            model.bind_tools(tools=bad)
        assert model.toolcall_session is original
    with pytest.raises(ValueError, match="Duplicate"):
        model.bind_tools(object(), [sample.openai_schema, sample.openai_schema])
    model.bind_tools()
    assert model.toolcall_session is original
    explicit = SimpleNamespace(tool_call=lambda *args: None)
    model.bind_tools(explicit, [sample.openai_schema])
    assert model.toolcall_session is explicit
    model.bind_tools(tools=[])
    assert not model.is_tools and model.tools == [] and model.toolcall_session is None


def test_bundle_and_vision_facades_support_decorated_and_explicit_bindings() -> None:
    backend = Base.__new__(Base)
    vision = VisionBase(_tool_chat_model_factory=lambda: backend)
    bundle = LLMBundle.__new__(LLMBundle)
    bundle.mdl = vision
    bundle.is_tools = True
    bundle.bind_tools(tools=[sample])
    assert vision.is_tools and isinstance(backend.toolcall_session, FunctionToolSession)
    explicit = object()
    bundle.bind_tools(explicit, [sample.openai_schema])
    assert backend.toolcall_session is explicit
    bundle.bind_tools(tools=[])
    assert not backend.is_tools and not vision.is_tools
