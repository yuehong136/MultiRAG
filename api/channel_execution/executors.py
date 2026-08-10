"""Executors that adapt existing MultiRAG targets to sanitized Channel events."""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from contextlib import aclosing
from copy import deepcopy
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from api.channel_capabilities import TargetCapabilities
from api.channel_execution.errors import TargetExecutionFailedError, TargetRevisionUnavailableError
from api.channel_execution.history import SqlAlchemyCanvasHistoryTransaction, SqlAlchemyDialogHistoryTransaction, canvas_regeneration_is_safe
from api.channel_execution.models import ChannelExecutionCommand, ExecutionEvent, ExecutionOperation, ExecutionTargetRef, TrustedChannelContext
from api.channel_execution.protocols import (
    CanvasHistoryTransaction,
    CanvasTargetDriver,
    DialogHistoryTransaction,
    DialogTargetDriver,
)
from api.channel_execution.reasoning import StreamingReasoningFilter, strip_reasoning
from api.db.db_models import Dialog
from common.constants import StatusEnum

_CANVAS_CAPABILITIES = TargetCapabilities(
    streaming=True,
    cancellable=True,
    regeneration="conditional",
    retryable=False,
    feedback=True,
    commit_mode="candidate_cas",
    effect_class="unknown",
)
_DIALOG_CAPABILITIES = TargetCapabilities(
    streaming=True,
    cancellable=True,
    regeneration="always",
    retryable=True,
    feedback=True,
    commit_mode="detached_cas",
    effect_class="generation_only",
)


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


def _payload_session_id(payload: dict[str, Any]) -> str | None:
    raw_session_id = payload.get("session_id")
    if isinstance(raw_session_id, str) and raw_session_id:
        return raw_session_id
    data = payload.get("data")
    if isinstance(data, dict):
        raw_session_id = data.get("session_id")
        if isinstance(raw_session_id, str) and raw_session_id:
            return raw_session_id
    return None


def _rewrite_frame_session(
    payload: dict[str, Any],
    *,
    public_session_id: str | None,
) -> str:
    if public_session_id:
        if isinstance(payload.get("session_id"), str):
            payload["session_id"] = public_session_id
        data = payload.get("data")
        if isinstance(data, dict) and isinstance(data.get("session_id"), str):
            data["session_id"] = public_session_id
    return "data:" + json.dumps(payload, ensure_ascii=False) + "\n\n"


def _merge_generated_session(
    current: str | None,
    payload: dict[str, Any],
) -> str | None:
    observed = _payload_session_id(payload)
    if observed and current and observed != current:
        raise TargetExecutionFailedError()
    return observed or current


class SqlAlchemyCanvasTargetDriver:
    """Guard the binding revision and own the Canvas history transaction."""

    def __init__(
        self,
        db: AsyncSession,
        history: CanvasHistoryTransaction | None = None,
    ) -> None:
        self._db = db
        self._history = history or SqlAlchemyCanvasHistoryTransaction(db)

    async def capabilities(
        self,
        *,
        tenant_id: str,
        target: ExecutionTargetRef,
    ) -> TargetCapabilities:
        revision_id = target.revision_id
        if not revision_id:
            raise TargetRevisionUnavailableError()

        def _resolve(sync_db: Session) -> bool | None:
            from api.db.services.canvas_service import UserCanvasService
            from api.db.services.user_canvas_version import UserCanvasVersionService

            canvas = UserCanvasService.get_by_id(sync_db, target.target_id)
            if canvas is None or canvas.user_id != tenant_id:
                return None
            latest_release = UserCanvasVersionService.get_latest_released(sync_db, target.target_id)
            if latest_release is None or latest_release.id != revision_id:
                return None
            return canvas_regeneration_is_safe(latest_release.dsl)

        regeneration_safe = await self._db.run_sync(_resolve)  # TODO(async-phase4)
        if regeneration_safe is None:
            raise TargetRevisionUnavailableError()
        return _CANVAS_CAPABILITIES.model_copy(
            update={
                "regeneration": "always" if regeneration_safe else "never",
                "retryable": regeneration_safe,
                "effect_class": "generation_only" if regeneration_safe else "unknown",
            }
        )

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
        operation: ExecutionOperation = "message",
    ) -> AsyncIterator[str]:
        return self._stream(
            tenant_id=tenant_id,
            target=target,
            question=question,
            session_id=session_id,
            principal_id=principal_id,
            operation=operation,
        )

    async def _stream(
        self,
        *,
        tenant_id: str,
        target: ExecutionTargetRef,
        question: str,
        session_id: str | None,
        principal_id: str | None,
        operation: ExecutionOperation,
    ) -> AsyncIterator[str]:
        from api.db.services.canvas_service import completion as canvas_completion

        prepared = await self._history.prepare(
            target_id=target.target_id,
            session_id=session_id,
            question=question,
            operation=operation,
        )
        generated_session_id = prepared.execution_session_id
        terminal = False
        promoted = False
        try:
            async with aclosing(
                canvas_completion(
                    db=self._db,
                    tenant_id=tenant_id,
                    agent_id=target.target_id,
                    session_id=prepared.execution_session_id,
                    query=question,
                    release=True,
                    user_id=principal_id or "",
                )
            ) as frames:
                async for frame in frames:
                    payload = _decode_sse_payload(frame)
                    if payload is None:
                        yield frame
                        continue
                    generated_session_id = _merge_generated_session(generated_session_id, payload)
                    terminal = terminal or payload.get("event") == "message_end"
                    yield _rewrite_frame_session(
                        payload,
                        public_session_id=prepared.public_session_id,
                    )
            if not terminal or not generated_session_id:
                raise TargetExecutionFailedError()
            await self._history.commit(prepared, generated_session_id)
            promoted = True
        finally:
            if not promoted:
                await self._history.abort(prepared, generated_session_id)


class SqlAlchemyDialogTargetDriver:
    """Own the Dialog history transaction around MultiRAG completion."""

    def __init__(
        self,
        db: AsyncSession,
        history: DialogHistoryTransaction | None = None,
    ) -> None:
        self._db = db
        self._history = history or SqlAlchemyDialogHistoryTransaction(db)

    def stream(
        self,
        *,
        tenant_id: str,
        target: ExecutionTargetRef,
        question: str,
        session_id: str | None,
        principal_id: str | None,
        operation: ExecutionOperation = "message",
    ) -> AsyncIterator[str]:
        return self._stream(
            tenant_id=tenant_id,
            target=target,
            question=question,
            session_id=session_id,
            principal_id=principal_id,
            operation=operation,
        )

    async def _stream(
        self,
        *,
        tenant_id: str,
        target: ExecutionTargetRef,
        question: str,
        session_id: str | None,
        principal_id: str | None,
        operation: ExecutionOperation,
    ) -> AsyncIterator[str]:
        from api.db.services.conversation_service import structure_answer
        from api.db.services.dialog_service import async_chat

        dialog = await self._load_dialog_snapshot(
            tenant_id=tenant_id,
            target_id=target.target_id,
        )
        prepared = await self._history.prepare(
            target_id=target.target_id,
            session_id=session_id,
            question=question,
            operation=operation,
            user_id=principal_id or "",
        )
        working = prepared.working_copy
        if prepared.expected_head is None:
            working.message.append(
                {
                    "role": "assistant",
                    "content": dialog.prompt_config.get("prologue"),
                    "created_at": time.time(),
                }
            )

        question_message: dict[str, Any] = {
            "content": question,
            "role": "user",
            "id": str(uuid4()),
        }
        working.message.append(question_message)
        prompt_messages: list[dict[str, Any]] = []
        for message in working.message:
            if message.get("role") == "system":
                continue
            if message.get("role") == "assistant" and not prompt_messages:
                continue
            prompt_messages.append(deepcopy(message))

        message_id = question_message["id"]
        working.message.append(
            {
                "role": "assistant",
                "content": "",
                "id": message_id,
            }
        )
        working.reference.append({"chunks": [], "doc_aggs": []})

        saw_final = False
        committed = False
        try:
            async with aclosing(
                async_chat(
                    dialog,
                    prompt_messages,
                    self._db,
                    True,
                    user_id=principal_id or "",
                )
            ) as answers:
                async for answer in answers:
                    if not isinstance(answer, dict):
                        raise TargetExecutionFailedError()
                    is_final = answer.get("final", True) is True
                    structured = structure_answer(
                        working,
                        answer,
                        message_id,
                        prepared.public_session_id,
                    )
                    # Keep MultiRAG's final snapshot distinct from its deltas.
                    # The target executor projects it into terminal content so
                    # every Channel can replace, rather than append, decorated
                    # answers such as citations inserted in the middle.
                    yield (
                        "data:"
                        + json.dumps(
                            {"code": 0, "data": structured},
                            ensure_ascii=False,
                        )
                        + "\n\n"
                    )
                    saw_final = saw_final or is_final
            if not saw_final:
                raise TargetExecutionFailedError()
            await self._history.commit(prepared)
            committed = True
            yield "data:" + json.dumps({"code": 0, "data": True}, ensure_ascii=False) + "\n\n"
        finally:
            if not committed:
                await self._history.abort(prepared)

    async def _load_dialog_snapshot(
        self,
        *,
        tenant_id: str,
        target_id: str,
    ) -> Dialog:
        try:
            stmt = select(Dialog).where(
                Dialog.id == target_id,
                Dialog.tenant_id == tenant_id,
                Dialog.status == StatusEnum.VALID.value,
            )
            row = (await self._db.scalars(stmt)).one_or_none()
            if row is None:
                raise TargetExecutionFailedError()
            snapshot = deepcopy(row.to_dict())
        finally:
            # The generator must never retain an attached Dialog ORM instance.
            await self._db.rollback()
        return Dialog(**snapshot)


class MultiRAGCanvasAgentExecutor:
    """Executes ``multirag.canvas_agent`` and filters its internal SSE stream."""

    target_type = "multirag.canvas_agent"

    def __init__(self, driver: CanvasTargetDriver) -> None:
        self._driver = driver

    async def capabilities(
        self,
        *,
        context: TrustedChannelContext,
    ) -> TargetCapabilities:
        return await self._driver.capabilities(
            tenant_id=context.tenant_id,
            target=context.target,
        )

    async def execute(
        self,
        *,
        context: TrustedChannelContext,
        command: ChannelExecutionCommand,
    ) -> AsyncIterator[ExecutionEvent]:
        await self._driver.validate_revision(tenant_id=context.tenant_id, target=context.target)
        frames = self._driver.stream(
            tenant_id=context.tenant_id,
            target=context.target,
            question=command.message.content,
            session_id=context.session_id,
            principal_id=context.principal_id,
            operation=command.operation,
        )
        return self._events(frames)

    async def _events(self, frames: AsyncIterator[str]) -> AsyncIterator[ExecutionEvent]:
        session_id: str | None = None
        in_reasoning = False
        saw_completion = False
        saw_content = False
        async with aclosing(frames) as managed_frames:
            async for frame in managed_frames:
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
                    saw_content = True
                    yield ExecutionEvent(event="message_delta", content=sanitized, session_id=session_id)

        if not saw_completion or not session_id or not saw_content:
            raise TargetExecutionFailedError()
        yield ExecutionEvent(event="message_completed", session_id=session_id)


class MultiRAGDialogExecutor:
    """Executes ``multirag.dialog`` through the existing conversation service."""

    target_type = "multirag.dialog"

    def __init__(self, driver: DialogTargetDriver) -> None:
        self._driver = driver

    async def capabilities(
        self,
        *,
        context: TrustedChannelContext,
    ) -> TargetCapabilities:
        del context
        return _DIALOG_CAPABILITIES

    async def execute(
        self,
        *,
        context: TrustedChannelContext,
        command: ChannelExecutionCommand,
    ) -> AsyncIterator[ExecutionEvent]:
        if context.target.revision_id is not None:
            # Dialog snapshots are not version-addressable in the current model.
            raise TargetRevisionUnavailableError()
        frames = self._driver.stream(
            tenant_id=context.tenant_id,
            target=context.target,
            question=command.message.content,
            session_id=context.session_id,
            principal_id=context.principal_id,
            operation=command.operation,
        )
        return self._dialog_events(frames)

    async def _dialog_events(
        self,
        frames: AsyncIterator[str],
    ) -> AsyncIterator[ExecutionEvent]:
        session_id: str | None = None
        saw_completion = False
        reasoning_filter = StreamingReasoningFilter()
        visible_fragments: list[str] = []
        saw_visible_delta = False
        authoritative_content: str | None = None

        async with aclosing(frames) as managed_frames:
            async for frame in managed_frames:
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
                    reasoning_filter.feed("<think>")
                    continue
                if data.get("end_to_think") is True:
                    reasoning_filter.feed("</think>")
                    continue
                answer = data.get("answer")
                if not isinstance(answer, str):
                    continue
                if data.get("final", True) is True:
                    authoritative_content = strip_reasoning(answer) or strip_reasoning("".join(visible_fragments))
                    # Older workers ignore terminal content and require at least
                    # one visible delta. Preserve that mixed-version path for
                    # final-only Dialog responses while newer workers replace it
                    # with the same authoritative snapshot before completion.
                    if authoritative_content and not saw_visible_delta:
                        saw_visible_delta = True
                        yield ExecutionEvent(
                            event="message_delta",
                            content=authoritative_content,
                            session_id=session_id,
                        )
                    continue
                if not answer:
                    continue
                sanitized = reasoning_filter.feed(answer)
                if sanitized:
                    visible_fragments.append(sanitized)
                    saw_visible_delta = saw_visible_delta or bool(sanitized.strip())
                    yield ExecutionEvent(event="message_delta", content=sanitized, session_id=session_id)

        if not saw_completion or not session_id or not authoritative_content:
            raise TargetExecutionFailedError()
        yield ExecutionEvent(
            event="message_completed",
            content=authoritative_content,
            session_id=session_id,
        )
