"""Per-run tool-call serialization for durable MCP interactions."""

from __future__ import annotations

import asyncio
from concurrent.futures import Future, InvalidStateError
from threading import Lock
from typing import Any, Protocol

from common.mcp_interactions import MCPInteractionPaused
from common.mcp_tool_call_conn import ToolCallSession


class AsyncToolCallSession(ToolCallSession, Protocol):
    async def tool_call_async(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> Any: ...


class SerializedInteractionToolCallSession:
    """Serialize one run's tools and close the gate after its first pause.

    A model may emit sibling tool calls in one response and the LLM adapter may
    schedule all of them concurrently.  This wrapper preserves their admitted
    order.  Calls ahead of the interaction may complete normally; once a
    delegate raises :class:`MCPInteractionPaused`, later calls receive the same
    control signal without invoking their delegate.

    The tail uses ``concurrent.futures.Future`` rather than an event-loop-bound
    lock so the sync and async ``ToolCallSession`` entry points share one queue.
    """

    def __init__(self, delegate: AsyncToolCallSession) -> None:
        self._delegate = delegate
        self._state_lock = Lock()
        self._paused: tuple[str, int] | None = None
        initial: Future[None] = Future()
        initial.set_result(None)
        self._tail = initial

    def tool_call(self, name: str, arguments: dict[str, Any]) -> Any:
        previous, ticket = self._reserve()
        previous.result()
        try:
            self._raise_if_paused()
            try:
                return self._delegate.tool_call(name, arguments)
            except MCPInteractionPaused as paused:
                self._record_pause(paused)
                raise
        finally:
            self._complete(ticket)

    async def tool_call_async(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> Any:
        previous, ticket = self._reserve()
        admitted = False
        try:
            await asyncio.shield(asyncio.wrap_future(previous))
            admitted = True
            self._raise_if_paused()
            try:
                return await self._delegate.tool_call_async(name, arguments)
            except MCPInteractionPaused as paused:
                self._record_pause(paused)
                raise
        finally:
            if admitted:
                self._complete(ticket)
            else:
                previous.add_done_callback(
                    lambda _completed: self._complete(ticket),
                )

    def _reserve(self) -> tuple[Future[None], Future[None]]:
        with self._state_lock:
            previous = self._tail
            ticket: Future[None] = Future()
            self._tail = ticket
        return previous, ticket

    def _record_pause(self, paused: MCPInteractionPaused) -> None:
        with self._state_lock:
            if self._paused is None:
                self._paused = (paused.interaction_id, paused.revision)

    def _raise_if_paused(self) -> None:
        with self._state_lock:
            paused = self._paused
        if paused is not None:
            raise MCPInteractionPaused(
                interaction_id=paused[0],
                revision=paused[1],
            )

    @staticmethod
    def _complete(ticket: Future[None]) -> None:
        try:
            ticket.set_result(None)
        except InvalidStateError:
            pass


def maybe_serialize_interaction_tool_calls(
    session: AsyncToolCallSession,
    *,
    enabled: bool,
) -> AsyncToolCallSession:
    """Keep the ordinary tool path untouched unless interactions are active."""

    if not enabled:
        return session
    return SerializedInteractionToolCallSession(session)


__all__ = [
    "AsyncToolCallSession",
    "SerializedInteractionToolCallSession",
    "maybe_serialize_interaction_tool_calls",
]
