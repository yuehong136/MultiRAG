"""The sync LocalLLM facade owns its stream/loop and preserves caller loops."""

import asyncio
import concurrent.futures
import threading
from collections.abc import AsyncGenerator
from types import SimpleNamespace
from typing import Any

import pytest

from core.llm.chat import LocalLLM


@pytest.fixture
def local(monkeypatch: pytest.MonkeyPatch) -> tuple[LocalLLM, list[Any]]:
    model = LocalLLM.__new__(LocalLLM)
    events: list[Any] = []

    def client() -> Any:
        events.append(("create", asyncio.get_running_loop(), threading.get_ident()))

        async def close() -> None:
            events.append(("client-close", asyncio.get_running_loop(), threading.get_ident()))

        return SimpleNamespace(close=close)

    def stream(client: Any, endpoint: str, prompt: str) -> AsyncGenerator[Any, None]:
        async def responses() -> AsyncGenerator[Any, None]:
            try:
                yield SimpleNamespace(text="one")
                if prompt == "error":
                    raise ValueError("controlled stream failure")
                if prompt == "cancel":
                    raise asyncio.CancelledError
                yield SimpleNamespace(text="final")
            finally:
                events.append(("stream-close", asyncio.get_running_loop(), threading.get_ident()))

        return responses()

    monkeypatch.setattr(model, "_create_client", client)
    monkeypatch.setattr(model, "_request_stream", stream)
    monkeypatch.setattr(model, "_prepare_prompt", lambda *args: "ok")
    return model, events


def assert_closed(events: list[Any]) -> None:
    assert [event[0] for event in events] == ["create", "stream-close", "client-close"]
    assert len({id(event[1]) for event in events}) == 1
    assert events[0][1].is_closed()
    assert not any(thread.name == "local-llm-stream" for thread in threading.enumerate())


@pytest.mark.parametrize("kind", ["normal", "early", "error", "cancel", "chat"])
def test_worker_thread_without_current_loop(local: tuple[LocalLLM, list[Any]], kind: str) -> None:
    model, events = local

    def work() -> None:
        if kind == "chat":
            answer, tokens = model.chat("", [], {})
            assert answer == "final" and isinstance(tokens, int)
            return
        stream = model._stream_response("/stream", kind)
        assert next(stream) == "one"
        if kind == "early":
            stream.close()
        elif kind == "cancel":
            with pytest.raises(asyncio.CancelledError):
                next(stream)
        else:
            result = list(stream)
            assert isinstance(result[-1], int)
            assert result[0] == "final" if kind == "normal" else "ERROR" in result[0]

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(work).result(timeout=10)
    assert_closed(events)


@pytest.mark.parametrize("kind", ["normal", "early", "error", "cancel"])
async def test_running_caller_loop_is_not_nested_or_closed(local: tuple[LocalLLM, list[Any]], kind: str) -> None:
    model, events = local
    caller = asyncio.get_running_loop()
    stream = model._stream_response("/stream", kind)
    assert next(stream) == "one"
    if kind == "early":
        stream.close()
    elif kind == "cancel":
        with pytest.raises(asyncio.CancelledError):
            next(stream)
    else:
        result = list(stream)
        assert isinstance(result[-1], int)
    assert_closed(events)
    assert events[0][1] is not caller and not caller.is_closed()
    assert events[0][2] != threading.get_ident()
    assert await asyncio.sleep(0, result="alive") == "alive"


def test_idle_caller_loop_is_preserved(local: tuple[LocalLLM, list[Any]]) -> None:
    model, events = local
    caller = asyncio.new_event_loop()
    asyncio.set_event_loop(caller)
    try:
        responses = list(model._stream_response("/stream", "normal"))
        assert responses[0] == "one"
        assert asyncio.get_event_loop() is caller and not caller.is_closed()
        assert_closed(events)
    finally:
        asyncio.set_event_loop(None)
        caller.close()


async def test_owned_loop_drains_client_child_tasks() -> None:
    from core.llm.async_stream import iterate_owned_stream

    closed = threading.Event()
    owned: list[asyncio.Task[None]] = []

    async def child() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()

    async def stream() -> AsyncGenerator[str, None]:
        owned.append(asyncio.create_task(child()))
        await asyncio.sleep(0)
        yield "body"

    responses = list(iterate_owned_stream(stream))
    assert responses == ["body"] and closed.is_set() and owned[0].cancelled()
    assert not any(thread.name == "local-llm-stream" for thread in threading.enumerate())


async def test_async_client_close_api_is_awaited(local: tuple[LocalLLM, list[Any]], monkeypatch: pytest.MonkeyPatch) -> None:
    model, events = local
    original = model._create_client

    def client() -> Any:
        resource = original()
        return SimpleNamespace(aclose=resource.close)

    monkeypatch.setattr(model, "_create_client", client)
    assert isinstance(list(model._stream_response("/stream", "normal"))[-1], int)
    assert_closed(events)
