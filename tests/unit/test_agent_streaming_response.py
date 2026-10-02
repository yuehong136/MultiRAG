"""ASGI response-owned close, distinct from TCP integration acceptance."""

import asyncio
from collections.abc import AsyncGenerator
from typing import Any

import pytest
from starlette.requests import ClientDisconnect

from api.utils.agent_streaming import AgentStreamingResponse


@pytest.mark.parametrize("spec,at_start", [("2.3", False), ("2.4", False), ("2.4", True)])
async def test_response_closes_owned_iterator_before_dependencies_exit(spec: str, at_start: bool) -> None:
    closed: list[str] = []
    sending = asyncio.Event()
    never = asyncio.Event()

    async def source() -> AsyncGenerator[str, None]:
        try:
            yield "prefetched"
            yield "message"
            pytest.fail("response resumed a disconnected source")
        finally:
            closed.append("source")

    answers = source()
    assert await anext(answers) == "prefetched"

    async def body() -> AsyncGenerator[str, None]:
        async for answer in answers:
            yield answer

    async def callback() -> None:
        await answers.aclose()
        closed.append("callback")

    response = AgentStreamingResponse(body(), close_callback=callback)

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == ("http.response.start" if at_start else "http.response.body"):
            sending.set()
            if spec == "2.4":
                raise OSError("actual ASGI send failure")
            await never.wait()

    async def receive() -> dict[str, Any]:
        await sending.wait()
        return {"type": "http.disconnect"}

    scope = {"type": "http", "asgi": {"spec_version": spec}}
    if spec == "2.4":
        with pytest.raises(ClientDisconnect):
            await response(scope, receive, send)
    else:
        await response(scope, receive, send)
    assert closed == ["source", "callback"]


async def test_response_propagates_task_cancellation_after_closing() -> None:
    closed: list[bool] = []
    entered = asyncio.Event()

    async def source() -> AsyncGenerator[str, None]:
        try:
            yield "message"
        finally:
            closed.append(True)

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.body":
            entered.set()
            await asyncio.Event().wait()

    async def receive() -> dict[str, Any]:
        await asyncio.Event().wait()
        return {"type": "http.disconnect"}

    response = AgentStreamingResponse(source())
    task = asyncio.create_task(response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed == [True]
