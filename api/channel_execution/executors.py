"""Executors that adapt existing MultiRAG targets to sanitized Channel events."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from api.channel_execution.errors import TargetExecutionFailedError, TargetRevisionUnavailableError
from api.channel_execution.models import ChannelExecutionCommand, ExecutionEvent, ExecutionTargetRef, TrustedChannelContext
from api.channel_execution.protocols import CanvasCompletionAdapter, DialogCompletionAdapter


def _decode_sse_payload(frame: str) -> dict[str, Any] | None:
    """Decode one existing service frame without exposing its raw payload."""

    if not isinstance(frame, str) or not frame.startswith("data:"):
        return None
    payload_text = frame[5:].strip()
    if not payload_text or payload_text == "[DONE]":
        return None
    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError as exc:
        raise TargetExecutionFailedError() from exc
    if not isinstance(payload, dict):
        raise TargetExecutionFailedError()
    return payload


class SqlAlchemyCanvasCompletionAdapter:
    """Guard the binding revision, then use the upstream latest-release path."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def validate_revision(self, *, tenant_id: str, target: ExecutionTargetRef) -> None:
        revision_id = target.revision_id
        if not revision_id:
            raise TargetRevisionUnavailableError()

        def _validate(sync_db: Session) -> bool:
            # Keep the heavy Canvas graph lazy so importing the private route
            # does not initialize model providers or document parsers.
            from api.db.services.canvas_service import UserCanvasService
            from api.db.services.user_canvas_version import UserCanvasVersionService

            canvas = UserCanvasService.get_by_id(sync_db, target.target_id)
            if canvas is None or canvas.user_id != tenant_id:
                return False
            latest_release = UserCanvasVersionService.get_latest_released(sync_db, target.target_id)
            return latest_release is not None and latest_release.id == revision_id

        if not await self._db.run_sync(_validate):  # TODO(async-phase4)
            raise TargetRevisionUnavailableError()

    def stream(
        self,
        *,
        tenant_id: str,
        target: ExecutionTargetRef,
        question: str,
        session_id: str | None,
        principal_id: str | None,
    ) -> AsyncIterator[str]:
        from api.db.services.canvas_service import completion as canvas_completion

        return canvas_completion(
            db=self._db,
            tenant_id=tenant_id,
            agent_id=target.target_id,
            session_id=session_id,
            query=question,
            release=True,
            user_id=principal_id or "",
        )


class SqlAlchemyDialogCompletionAdapter:
    """Reuses the async MultiRAG Dialog completion implementation."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    def stream(
        self,
        *,
        tenant_id: str,
        target: ExecutionTargetRef,
        question: str,
        session_id: str | None,
        principal_id: str | None,
    ) -> AsyncIterator[str]:
        from api.db.services.conversation_service import async_completion as dialog_completion

        return dialog_completion(
            db=self._db,
            tenant_id=tenant_id,
            chat_id=target.target_id,
            question=question,
            session_id=session_id,
            stream=True,
            user_id=principal_id or "",
        )


class MultiRAGCanvasAgentExecutor:
    """Executes ``multirag.canvas_agent`` and filters its internal SSE stream."""

    target_type = "multirag.canvas_agent"

    def __init__(self, adapter: CanvasCompletionAdapter) -> None:
        self._adapter = adapter

    async def execute(
        self,
        *,
        context: TrustedChannelContext,
        command: ChannelExecutionCommand,
    ) -> AsyncIterator[ExecutionEvent]:
        await self._adapter.validate_revision(tenant_id=context.tenant_id, target=context.target)
        frames = self._adapter.stream(
            tenant_id=context.tenant_id,
            target=context.target,
            question=command.message.content,
            session_id=context.session_id,
            principal_id=context.principal_id,
        )
        return self._events(frames)

    async def _events(self, frames: AsyncIterator[str]) -> AsyncIterator[ExecutionEvent]:
        session_id: str | None = None
        in_reasoning = False
        saw_completion = False
        async for frame in frames:
            payload = _decode_sse_payload(frame)
            if payload is None:
                continue

            raw_session_id = payload.get("session_id")
            if isinstance(raw_session_id, str) and raw_session_id:
                session_id = raw_session_id

            event = payload.get("event")
            data = payload.get("data")
            if event == "message_end":
                saw_completion = True
                continue
            if event != "message" or not isinstance(data, dict):
                # node traces, references, A2UI and tool details are private.
                continue
            if data.get("start_to_think") is True:
                in_reasoning = True
                continue
            if data.get("end_to_think") is True:
                in_reasoning = False
                continue
            content = data.get("content")
            if in_reasoning or not isinstance(content, str) or not content:
                continue
            sanitized = content.replace("<think>", "").replace("</think>", "")
            if sanitized:
                yield ExecutionEvent(event="message_delta", content=sanitized, session_id=session_id)

        if not saw_completion or not session_id:
            raise TargetExecutionFailedError()
        yield ExecutionEvent(event="message_completed", session_id=session_id)


class MultiRAGDialogExecutor:
    """Executes ``multirag.dialog`` through the existing conversation service."""

    target_type = "multirag.dialog"

    def __init__(self, adapter: DialogCompletionAdapter) -> None:
        self._adapter = adapter

    async def execute(
        self,
        *,
        context: TrustedChannelContext,
        command: ChannelExecutionCommand,
    ) -> AsyncIterator[ExecutionEvent]:
        if context.target.revision_id is not None:
            # Dialog snapshots are not version-addressable in the current model.
            raise TargetRevisionUnavailableError()
        return self._events(context=context, command=command)

    async def _events(
        self,
        *,
        context: TrustedChannelContext,
        command: ChannelExecutionCommand,
    ) -> AsyncIterator[ExecutionEvent]:
        session_id = context.session_id
        if session_id is None:
            bootstrap_frames = self._adapter.stream(
                tenant_id=context.tenant_id,
                target=context.target,
                question=command.message.content,
                session_id=None,
                principal_id=context.principal_id,
            )
            session_id = await self._consume_dialog_bootstrap(bootstrap_frames)

        frames = self._adapter.stream(
            tenant_id=context.tenant_id,
            target=context.target,
            question=command.message.content,
            session_id=session_id,
            principal_id=context.principal_id,
        )
        async for event in self._dialog_events(frames, require_content=True):
            yield event

    async def _consume_dialog_bootstrap(self, frames: AsyncIterator[str]) -> str:
        session_id: str | None = None
        async for event in self._dialog_events(frames, require_content=False):
            if event.session_id:
                session_id = event.session_id
        if not session_id:
            raise TargetExecutionFailedError()
        return session_id

    async def _dialog_events(
        self,
        frames: AsyncIterator[str],
        *,
        require_content: bool,
    ) -> AsyncIterator[ExecutionEvent]:
        session_id: str | None = None
        saw_completion = False
        saw_content = False
        in_reasoning = False

        async for frame in frames:
            payload = _decode_sse_payload(frame)
            if payload is None:
                continue
            if payload.get("code") != 0:
                raise TargetExecutionFailedError()
            data = payload.get("data")
            if data is True:
                saw_completion = True
                continue
            if not isinstance(data, dict):
                continue

            raw_session_id = data.get("session_id")
            if isinstance(raw_session_id, str) and raw_session_id:
                session_id = raw_session_id
            if data.get("start_to_think") is True:
                in_reasoning = True
                continue
            if data.get("end_to_think") is True:
                in_reasoning = False
                continue
            answer = data.get("answer")
            if in_reasoning or not isinstance(answer, str) or not answer:
                continue
            sanitized = answer.replace("<think>", "").replace("</think>", "")
            if sanitized:
                saw_content = True
                yield ExecutionEvent(event="message_delta", content=sanitized, session_id=session_id)

        if not saw_completion or not session_id or (require_content and not saw_content):
            raise TargetExecutionFailedError()
        yield ExecutionEvent(event="message_completed", session_id=session_id)
