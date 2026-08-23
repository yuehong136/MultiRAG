from __future__ import annotations

import asyncio
from typing import Any

import pytest

from api.identity.mcp_interactions.tool_gate import (
    SerializedInteractionToolCallSession,
    maybe_serialize_interaction_tool_calls,
)
from common.mcp_interactions import MCPInteractionPaused


class _PauseDelegate:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.read_started = asyncio.Event()
        self.release_read = asyncio.Event()

    def tool_call(self, name: str, arguments: dict[str, Any]) -> object:
        raise AssertionError(f"unexpected synchronous call: {name} {arguments}")

    async def tool_call_async(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> object:
        del arguments
        self.calls.append(name)
        if name == "read":
            self.read_started.set()
            await self.release_read.wait()
            return "read-result"
        if name == "prepare":
            return "prepare-result"
        if name == "pause":
            raise MCPInteractionPaused(
                interaction_id="interaction-first",
                revision=1,
            )
        raise MCPInteractionPaused(
            interaction_id="interaction-second",
            revision=1,
        )


@pytest.mark.asyncio
async def test_parallel_calls_stop_not_started_siblings_after_first_pause() -> None:
    delegate = _PauseDelegate()
    session = SerializedInteractionToolCallSession(delegate)

    read = asyncio.create_task(session.tool_call_async("read", {}))
    await delegate.read_started.wait()
    prepare = asyncio.create_task(session.tool_call_async("prepare", {}))
    await asyncio.sleep(0)
    pause = asyncio.create_task(session.tool_call_async("pause", {}))
    await asyncio.sleep(0)
    sibling = asyncio.create_task(session.tool_call_async("second-pause", {}))
    await asyncio.sleep(0)

    delegate.release_read.set()

    assert await read == "read-result"
    assert await prepare == "prepare-result"
    with pytest.raises(MCPInteractionPaused) as first:
        await pause
    with pytest.raises(MCPInteractionPaused) as blocked:
        await sibling

    assert first.value.interaction_id == "interaction-first"
    assert blocked.value.interaction_id == "interaction-first"
    assert blocked.value.revision == 1
    assert delegate.calls == ["read", "prepare", "pause"]


class _FailureDelegate:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def tool_call(self, name: str, arguments: dict[str, Any]) -> object:
        raise AssertionError(f"unexpected synchronous call: {name} {arguments}")

    async def tool_call_async(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> object:
        del arguments
        self.calls.append(name)
        if name == "fails":
            raise RuntimeError("tool failed")
        return "ok"


@pytest.mark.asyncio
async def test_parallel_non_pause_failure_does_not_strand_serial_queue() -> None:
    delegate = _FailureDelegate()
    session = SerializedInteractionToolCallSession(delegate)

    failing = asyncio.create_task(session.tool_call_async("fails", {}))
    await asyncio.sleep(0)
    following = asyncio.create_task(session.tool_call_async("following", {}))

    with pytest.raises(RuntimeError, match="tool failed"):
        await failing
    assert await following == "ok"
    assert delegate.calls == ["fails", "following"]


class _ConcurrentDelegate:
    def __init__(self) -> None:
        self.active = 0
        self.peak = 0
        self.both_started = asyncio.Event()
        self.release = asyncio.Event()

    def tool_call(self, name: str, arguments: dict[str, Any]) -> object:
        raise AssertionError(f"unexpected synchronous call: {name} {arguments}")

    async def tool_call_async(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> object:
        del arguments
        self.active += 1
        self.peak = max(self.peak, self.active)
        if self.active == 2:
            self.both_started.set()
        try:
            await self.release.wait()
            return name
        finally:
            self.active -= 1


@pytest.mark.asyncio
async def test_non_interaction_session_keeps_parallel_behavior() -> None:
    delegate = _ConcurrentDelegate()
    session = maybe_serialize_interaction_tool_calls(delegate, enabled=False)

    first = asyncio.create_task(session.tool_call_async("first", {}))
    second = asyncio.create_task(session.tool_call_async("second", {}))
    await asyncio.wait_for(delegate.both_started.wait(), timeout=1)
    delegate.release.set()

    assert await asyncio.gather(first, second) == ["first", "second"]
    assert session is delegate
    assert delegate.peak == 2
