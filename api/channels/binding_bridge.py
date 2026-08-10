"""Lifecycle bridge for trusted server-side Channel bindings, any provider."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, Protocol, runtime_checkable

from api.channel_capabilities import EffectiveReplyCapabilities
from api.channels.agent_bridge import (
    DEMO_ONLY_TEXT,
    QUESTION_TOO_LONG_TEXT,
    SESSION_RESET_TEXT,
    TEXT_ONLY_TEXT,
    AgentExecutionError,
)
from api.channels.core.base import (
    Channel,
    ChannelAction,
    ChannelActionResponse,
    IncomingMessage,
    OutgoingMessage,
    ReplyActionIds,
    ReplyActionKind,
    ReplyContext,
    ReplySession,
    ReplySessionState,
    ReplyStatus,
)
from api.channels.core.reply import SERVICE_UNAVAILABLE_TEXT
from api.channels.execution_events import (
    BindingExecutionEvent,
    ExecutionFailedEvent,
    MessageCompletedEvent,
    MessageDeltaEvent,
)
from api.channels.state_store import ChannelStateStore, binding_conversation_key

LOGGER = logging.getLogger(__name__)

QUEUE_BUSY_TEXT = "当前会话排队较多，请稍后再试。"
_ACTION_TTL_SECONDS = 86_400
_MAX_ACTIONS = 4_096


@runtime_checkable
class BindingExecutor(Protocol):
    def stream(
        self,
        *,
        question: str,
        event_id: str,
        conversation_key: str,
        provider: str,
        subject: str,
        conversation: str,
        operation: Literal["message", "regenerate"] = "message",
    ) -> AsyncIterator[BindingExecutionEvent]: ...

    async def reset(self, *, conversation_key: str) -> None: ...


class _WorkKind(StrEnum):
    QUESTION = "question"
    DIRECT = "direct"
    RESET = "reset"
    EMPTY = "empty"


@dataclass(slots=True)
class _ExecutionRecord:
    source: IncomingMessage
    conversation_key: str
    question: str
    action_ids: ReplyActionIds
    capabilities: EffectiveReplyCapabilities
    status: ReplyStatus = ReplyStatus.QUEUED
    reply_session: ReplySession | None = None
    reply_message_id: str = ""
    execution_task: asyncio.Task[None] | None = None
    cancel_requested: bool = False
    feedback: bool | None = None


@dataclass(slots=True)
class _PreparedMessage:
    source: IncomingMessage
    conversation_key: str
    kind: _WorkKind
    direct_content: str = ""
    record: _ExecutionRecord | None = None


@dataclass(slots=True)
class _RegisteredAction:
    record: _ExecutionRecord
    kind: ReplyActionKind
    expires_at: float
    used: bool = False


def _short_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


class BindingBridge:
    """Own one message's prepare/run/terminal lifecycle.

    The worker is the only conversation queue owner. This bridge deliberately
    has no second per-conversation lock: accepted messages are prepared early
    so every source gets an immediate queued card, then the worker starts them
    in ticket order. Card callbacks only validate an opaque action, claim it in
    this single-leader process and enqueue asynchronous work.
    """

    def __init__(
        self,
        *,
        channel: Channel,
        executor: BindingExecutor,
        state_store: ChannelStateStore,
        binding_id: str,
        allowed_sender_ids: set[str] | frozenset[str],
        max_question_chars: int,
        max_answer_chars: int,
        capabilities: EffectiveReplyCapabilities,
        private_chat_only: bool = True,
    ) -> None:
        self._channel = channel
        self._executor = executor
        self._state_store = state_store
        self._binding_id = binding_id
        self._allowed_sender_ids = frozenset(allowed_sender_ids)
        self._max_question_chars = max_question_chars
        self._max_answer_chars = max_answer_chars
        self._private_chat_only = private_chat_only
        self._capabilities = capabilities
        self._scheduler: Callable[[IncomingMessage], Awaitable[None]] | None = None
        self._preparations: dict[int, asyncio.Task[_PreparedMessage | None]] = {}
        self._actions: dict[str, _RegisteredAction] = {}
        self._latest_execution_by_conversation: dict[str, str] = {}
        self._background_tasks: set[asyncio.Task[None]] = set()

    def set_message_scheduler(
        self,
        scheduler: Callable[[IncomingMessage], Awaitable[None]],
    ) -> None:
        self._scheduler = scheduler

    def accepts_message(self, message: IncomingMessage) -> bool:
        """Apply transport-only policy before the worker allocates a ticket."""

        if self._private_chat_only and message.chat_type != "p2p":
            return False
        if message.sender_type != "user":
            return False
        if not message.message_id or not message.chat_id or not message.sender_id:
            self._log(logging.WARNING, "event_rejected", message, "MESSAGE_IDENTITY_MISSING")
            return False
        return True

    def message_queued(self, message: IncomingMessage, *, queue_position: int) -> None:
        """Start claim/card preparation without blocking the SDK callback."""

        task = asyncio.create_task(
            self._prepare_message(message, queue_position=queue_position),
            name=f"channel-prepare-{_short_hash(message.execution_id)}",
        )
        self._preparations[id(message)] = task

    def message_rejected(self, message: IncomingMessage, *, reason: str) -> None:
        """Make queue overflow user-visible; never silently drop it."""

        self._spawn_background(
            self._reply_queue_rejected(message, reason=reason),
            name=f"channel-reject-{_short_hash(message.execution_id)}",
        )

    async def handle_message(self, message: IncomingMessage) -> None:
        """Run a previously prepared message after its worker ticket starts."""

        preparation = self._preparations.pop(id(message), None)
        if preparation is None:
            if not self.accepts_message(message):
                return
            preparation = asyncio.create_task(self._prepare_message(message, queue_position=0))
        prepared = await preparation
        if prepared is None:
            return
        if prepared.kind is _WorkKind.DIRECT:
            await self._reply_and_complete(message, prepared.direct_content)
            return
        if prepared.kind is _WorkKind.EMPTY:
            await self._mark_replied(message)
            return
        if prepared.kind is _WorkKind.RESET:
            await self._run_reset(prepared)
            return
        if prepared.record is not None:
            await self._run_question(prepared.record)

    async def handle_action(self, action: ChannelAction) -> ChannelActionResponse:
        """Claim a low-risk action and enqueue work without provider I/O."""

        self._cleanup_actions()
        registered = self._actions.get(action.action_id)
        if registered is None or registered.expires_at <= time.monotonic():
            return ChannelActionResponse("warning", "操作已过期，请使用最新卡片。")
        if registered.used:
            return ChannelActionResponse("info", "该操作已经处理。")

        record = registered.record
        if action.operator_id != record.source.sender_id or action.chat_id != record.source.chat_id or (record.reply_message_id and action.message_id != record.reply_message_id):
            self._log(
                logging.WARNING,
                "action_rejected",
                record.source,
                "ACTION_IDENTITY_MISMATCH",
            )
            return ChannelActionResponse("error", "你不能操作这张卡片。")

        if registered.kind is ReplyActionKind.CANCEL:
            if record.status not in {ReplyStatus.QUEUED, ReplyStatus.RUNNING}:
                registered.used = True
                return ChannelActionResponse("info", "生成已经结束。")
            can_cancel = record.capabilities.cancel_queued if record.status is ReplyStatus.QUEUED else record.capabilities.cancel_running
            if not can_cancel:
                registered.used = True
                return ChannelActionResponse("warning", "当前阶段不支持停止生成。")
            registered.used = True
            record.cancel_requested = True
            task = record.execution_task
            if task is not None and not task.done():
                task.cancel()
            elif record.reply_session is not None:
                self._spawn_background(
                    self._finalize_cancel(record),
                    name=f"channel-cancel-{_short_hash(record.source.execution_id)}",
                )
            return ChannelActionResponse("success", "正在停止生成。")

        if registered.kind in {ReplyActionKind.REGENERATE, ReplyActionKind.RETRY}:
            replace_completed = registered.kind is ReplyActionKind.REGENERATE
            action_label = "重新生成" if replace_completed else "重试"
            latest_subject = "回答" if replace_completed else "请求"
            task_kind = "regenerate" if replace_completed else "retry"
            capability_enabled = record.capabilities.regenerate if replace_completed else record.capabilities.retry
            if not capability_enabled:
                registered.used = True
                return ChannelActionResponse("warning", "当前回答不支持该操作。")
            expected_statuses = {ReplyStatus.FINAL} if replace_completed else {ReplyStatus.ERROR, ReplyStatus.CANCELLED}
            if record.status not in expected_statuses:
                return ChannelActionResponse("warning", "请等待当前生成结束。")
            if self._scheduler is None:
                return ChannelActionResponse("error", f"{action_label}暂时不可用。")
            if self._latest_execution_by_conversation.get(record.conversation_key) != record.source.execution_id:
                registered.used = True
                return ChannelActionResponse("warning", f"只能{action_label}当前会话的最新{latest_subject}。")
            registered.used = True
            regenerated = self._regenerated_message(
                record.source,
                action,
                replace_completed=replace_completed,
            )
            self._spawn_background(
                self._scheduler(regenerated),
                name=f"channel-{task_kind}-{_short_hash(regenerated.execution_id)}",
            )
            return ChannelActionResponse("success", "已加入当前会话队列。")

        if record.status is not ReplyStatus.FINAL:
            return ChannelActionResponse("warning", "回答尚未完成。")
        if not record.capabilities.feedback:
            registered.used = True
            return ChannelActionResponse("warning", "当前回答不支持反馈。")
        if record.feedback is not None:
            self._mark_feedback_actions_used(record)
            return ChannelActionResponse("info", "感谢，你已经反馈过了。")

        helpful = registered.kind is ReplyActionKind.HELPFUL
        record.feedback = helpful
        self._mark_feedback_actions_used(record)
        self._spawn_background(
            self._acknowledge_feedback(record, helpful=helpful),
            name=f"channel-feedback-{_short_hash(record.source.execution_id)}",
        )
        return ChannelActionResponse("success", "感谢反馈。")

    async def close(self) -> None:
        """Cancel bridge-owned preparation and callback tasks on shutdown."""

        tasks = [*self._preparations.values(), *self._background_tasks]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._preparations.clear()
        self._background_tasks.clear()
        self._actions.clear()
        self._latest_execution_by_conversation.clear()

    async def _prepare_message(
        self,
        message: IncomingMessage,
        *,
        queue_position: int,
    ) -> _PreparedMessage | None:
        try:
            claimed = await self._state_store.claim_message(message.execution_id)
        except Exception:
            self._log(logging.ERROR, "state_failed", message, "REDIS_CLAIM_FAILED")
            await self._safe_reply(message, SERVICE_UNAVAILABLE_TEXT)
            return None
        if not claimed:
            self._log(logging.INFO, "duplicate_dropped", message, "", result="duplicate")
            return None

        conversation = binding_conversation_key(
            self._binding_id,
            message.channel,
            message.chat_id,
            message.sender_id,
        )
        if self._allowed_sender_ids and message.sender_id not in self._allowed_sender_ids:
            return _PreparedMessage(
                source=message,
                conversation_key=conversation,
                kind=_WorkKind.DIRECT,
                direct_content=DEMO_ONLY_TEXT,
            )
        if message.message_type != "text":
            return _PreparedMessage(
                source=message,
                conversation_key=conversation,
                kind=_WorkKind.DIRECT,
                direct_content=TEXT_ONLY_TEXT,
            )

        question = message.text.strip()
        if not question:
            return _PreparedMessage(
                source=message,
                conversation_key=conversation,
                kind=_WorkKind.EMPTY,
            )
        if len(question) > self._max_question_chars:
            return _PreparedMessage(
                source=message,
                conversation_key=conversation,
                kind=_WorkKind.DIRECT,
                direct_content=QUESTION_TOO_LONG_TEXT,
            )
        if question == "/reset":
            return _PreparedMessage(
                source=message,
                conversation_key=conversation,
                kind=_WorkKind.RESET,
            )

        action_ids = self._new_action_ids()
        record = _ExecutionRecord(
            source=message,
            conversation_key=conversation,
            question=question,
            action_ids=action_ids,
            capabilities=self._capabilities,
        )
        self._register_actions(record)
        try:
            record.reply_session = await self._channel.begin_reply(
                message,
                max_content_chars=self._max_answer_chars,
                context=ReplyContext(
                    status=ReplyStatus.QUEUED,
                    queue_position=queue_position,
                    actions=action_ids,
                    capabilities=self._capabilities,
                ),
            )
            record.reply_message_id = record.reply_session.reply_message_id
        except Exception:
            self._drop_actions(record)
            self._log(logging.ERROR, "reply_failed", message, "REPLY_BEGIN_FAILURE")
            await self._mark_executed(message)
            await self._safe_reply(message, SERVICE_UNAVAILABLE_TEXT)
            return None

        if action_ids.regenerate or action_ids.retry:
            self._latest_execution_by_conversation[conversation] = message.execution_id
        else:
            self._latest_execution_by_conversation.pop(conversation, None)

        return _PreparedMessage(
            source=message,
            conversation_key=conversation,
            kind=_WorkKind.QUESTION,
            record=record,
        )

    async def _run_question(self, record: _ExecutionRecord) -> None:
        reply_session = record.reply_session
        if reply_session is None:
            return
        if record.cancel_requested:
            await self._finalize_cancel(record)
            return

        try:
            await reply_session.set_status(ReplyStatus.RUNNING)
        except Exception:
            self._log(logging.ERROR, "reply_failed", record.source, "REPLY_STATUS_FAILURE")
            await self._mark_executed(record.source)
            await self._fail_reply_session(record, "REPLY_STATUS_FAILURE")
            return
        record.status = ReplyStatus.RUNNING
        if record.cancel_requested:
            await self._finalize_cancel(record)
            return

        started_at = time.monotonic()
        task = asyncio.create_task(
            self._consume_execution(record, started_at=started_at),
            name=f"channel-execute-{_short_hash(record.source.execution_id)}",
        )
        record.execution_task = task
        if record.cancel_requested:
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            if not record.cancel_requested:
                raise
            await self._finalize_cancel(record)
        finally:
            record.execution_task = None

    async def _consume_execution(
        self,
        record: _ExecutionRecord,
        *,
        started_at: float,
    ) -> None:
        reply_session = record.reply_session
        if reply_session is None:
            return
        message = record.source
        try:
            terminal_seen = False
            async for event in self._executor.stream(
                question=record.question,
                event_id=message.execution_id,
                conversation_key=record.conversation_key,
                provider=message.channel,
                subject=message.sender_id,
                conversation=message.chat_id,
                operation=message.operation,
            ):
                if terminal_seen:
                    raise AgentExecutionError("CHANNEL_EXECUTION_INVALID_STREAM")
                if isinstance(event, MessageDeltaEvent):
                    try:
                        await reply_session.append(event.content)
                    except Exception:
                        self._log(
                            logging.ERROR,
                            "reply_failed",
                            message,
                            "REPLY_APPEND_FAILURE",
                        )
                        await self._mark_executed(message)
                        await self._fail_reply_session(record, "REPLY_APPEND_FAILURE")
                        return
                    continue
                if isinstance(event, ExecutionFailedEvent):
                    terminal_seen = True
                    error_code = f"CHANNEL_EXECUTION_{event.error_code}"
                    self._log(logging.ERROR, "execution_failed", message, error_code)
                    await self._mark_executed(message)
                    await self._fail_reply_session(record, error_code)
                    continue
                if isinstance(event, MessageCompletedEvent):
                    terminal_seen = True
                    await self._complete_reply(
                        record,
                        session_id=event.session_id,
                        authoritative_content=event.content,
                        started_at=started_at,
                    )
            if terminal_seen:
                return
            raise AgentExecutionError("CHANNEL_EXECUTION_INCOMPLETE")
        except asyncio.CancelledError:
            raise
        except AgentExecutionError as exc:
            self._log(logging.ERROR, "execution_failed", message, exc.code)
            await self._mark_executed(message)
            await self._fail_reply_session(record, exc.code)
        except Exception:
            self._log(
                logging.ERROR,
                "execution_failed",
                message,
                "CHANNEL_EXECUTION_FAILURE",
            )
            await self._mark_executed(message)
            await self._fail_reply_session(record, "CHANNEL_EXECUTION_FAILURE")

    async def _complete_reply(
        self,
        record: _ExecutionRecord,
        *,
        session_id: str,
        authoritative_content: str | None,
        started_at: float,
    ) -> None:
        message = record.source
        elapsed_ms = round((time.monotonic() - started_at) * 1000)
        reply_session = record.reply_session
        if reply_session is None:
            return
        # Receiving the trusted terminal event is the cancellation barrier.
        # Delivery may still fail and downgrade to ERROR below, but a late
        # stop callback must not cancel final card replacement mid-flight.
        record.status = ReplyStatus.FINAL
        try:
            if authoritative_content is not None:
                await reply_session.replace(authoritative_content)
            await reply_session.complete()
            await self._state_store.mark_replied(message.execution_id)
        except Exception:
            self._log(logging.ERROR, "reply_failed", message, "REPLY_OR_STATE_FAILURE")
            await self._mark_executed(message)
            if reply_session.state is ReplySessionState.OPEN:
                await self._fail_reply_session(record, "REPLY_OR_STATE_FAILURE")
            elif reply_session.state is ReplySessionState.COMPLETED:
                record.status = ReplyStatus.FINAL
            else:
                record.status = ReplyStatus.ERROR
            return
        self._log(
            logging.INFO,
            "execution_completed",
            message,
            "",
            result="ok",
            execution_ms=elapsed_ms,
            session_id=session_id,
        )

    async def _fail_reply_session(
        self,
        record: _ExecutionRecord,
        error_code: str,
    ) -> None:
        reply_session = record.reply_session
        if reply_session is None:
            return
        record.status = ReplyStatus.ERROR
        try:
            await reply_session.fail(error_code)
        except Exception:
            self._log(logging.ERROR, "reply_failed", record.source, "REPLY_FAILED")

    async def _finalize_cancel(self, record: _ExecutionRecord) -> None:
        if record.status is ReplyStatus.CANCELLED:
            return
        reply_session = record.reply_session
        if reply_session is None:
            return
        if reply_session.state is ReplySessionState.OPEN:
            try:
                await reply_session.cancel()
                await self._state_store.mark_replied(record.source.execution_id)
            except Exception:
                self._log(
                    logging.ERROR,
                    "reply_failed",
                    record.source,
                    "REPLY_CANCEL_FAILURE",
                )
                await self._mark_executed(record.source)
                return
        record.status = ReplyStatus.CANCELLED
        self._log(
            logging.INFO,
            "execution_cancelled",
            record.source,
            "",
            result="ok",
        )

    async def _run_reset(self, prepared: _PreparedMessage) -> None:
        try:
            await self._executor.reset(conversation_key=prepared.conversation_key)
        except Exception:
            await self._mark_executed(prepared.source)
            await self._safe_reply(prepared.source, SERVICE_UNAVAILABLE_TEXT)
            return
        self._latest_execution_by_conversation.pop(prepared.conversation_key, None)
        await self._reply_and_complete(prepared.source, SESSION_RESET_TEXT)

    async def _reply_queue_rejected(
        self,
        message: IncomingMessage,
        *,
        reason: str,
    ) -> None:
        try:
            claimed = await self._state_store.claim_message(message.execution_id)
        except Exception:
            self._log(logging.ERROR, "state_failed", message, "REDIS_CLAIM_FAILED")
            return
        if not claimed:
            return
        await self._reply_and_complete(message, QUEUE_BUSY_TEXT)
        self._log(logging.INFO, "queue_rejected", message, reason, result="rejected")

    async def _acknowledge_feedback(
        self,
        record: _ExecutionRecord,
        *,
        helpful: bool,
    ) -> None:
        reply_session = record.reply_session
        if reply_session is not None:
            await reply_session.acknowledge_feedback(helpful=helpful)
        LOGGER.info(
            "channel_event=feedback_recorded binding_id_hash=%s message_id_hash=%s helpful=%s result=ok error_code=",
            _short_hash(self._binding_id),
            _short_hash(record.source.execution_id),
            str(helpful).lower(),
        )

    def _new_action_ids(self) -> ReplyActionIds:
        return ReplyActionIds(
            cancel=secrets.token_urlsafe(24) if self._capabilities.cancel_queued or self._capabilities.cancel_running else "",
            regenerate=secrets.token_urlsafe(24) if self._capabilities.regenerate else "",
            retry=secrets.token_urlsafe(24) if self._capabilities.retry else "",
            helpful=secrets.token_urlsafe(24) if self._capabilities.feedback else "",
            unhelpful=secrets.token_urlsafe(24) if self._capabilities.feedback else "",
        )

    def _register_actions(self, record: _ExecutionRecord) -> None:
        self._cleanup_actions()
        expires_at = time.monotonic() + _ACTION_TTL_SECONDS
        for kind, action_id in (
            (ReplyActionKind.CANCEL, record.action_ids.cancel),
            (ReplyActionKind.REGENERATE, record.action_ids.regenerate),
            (ReplyActionKind.RETRY, record.action_ids.retry),
            (ReplyActionKind.HELPFUL, record.action_ids.helpful),
            (ReplyActionKind.UNHELPFUL, record.action_ids.unhelpful),
        ):
            if not action_id:
                continue
            self._actions[action_id] = _RegisteredAction(
                record=record,
                kind=kind,
                expires_at=expires_at,
            )

    def _cleanup_actions(self) -> None:
        now = time.monotonic()
        stale = [action_id for action_id, registered in self._actions.items() if registered.expires_at <= now]
        for action_id in stale:
            self._actions.pop(action_id, None)
        overflow = len(self._actions) - _MAX_ACTIONS
        if overflow > 0:
            oldest = sorted(
                self._actions.items(),
                key=lambda item: item[1].expires_at,
            )[:overflow]
            for action_id, _registered in oldest:
                self._actions.pop(action_id, None)
        live_execution_ids = {registered.record.source.execution_id for registered in self._actions.values()}
        stale_conversations = [conversation for conversation, execution_id in self._latest_execution_by_conversation.items() if execution_id not in live_execution_ids]
        for conversation in stale_conversations:
            self._latest_execution_by_conversation.pop(conversation, None)

    def _drop_actions(self, record: _ExecutionRecord) -> None:
        for action_id in (
            record.action_ids.cancel,
            record.action_ids.regenerate,
            record.action_ids.retry,
            record.action_ids.helpful,
            record.action_ids.unhelpful,
        ):
            self._actions.pop(action_id, None)

    def _mark_feedback_actions_used(self, record: _ExecutionRecord) -> None:
        for action_id in (record.action_ids.helpful, record.action_ids.unhelpful):
            registered = self._actions.get(action_id)
            if registered is not None:
                registered.used = True

    @staticmethod
    def _regenerated_message(
        source: IncomingMessage,
        action: ChannelAction,
        *,
        replace_completed: bool,
    ) -> IncomingMessage:
        return IncomingMessage(
            channel=source.channel,
            account_id=source.account_id,
            chat_id=source.chat_id,
            chat_type=source.chat_type,
            message_id=source.message_id,
            sender_id=source.sender_id,
            content=source.content,
            message_type=source.message_type,
            sender_type=source.sender_type,
            event_id=action.event_id,
            request_id=f"action:{action.event_id}",
            operation="regenerate" if replace_completed else source.operation,
        )

    def _spawn_background(
        self,
        coroutine: Awaitable[None],
        *,
        name: str,
    ) -> None:
        task = asyncio.create_task(coroutine, name=name)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_task_done)

    def _background_task_done(self, task: asyncio.Task[None]) -> None:
        self._background_tasks.discard(task)
        if task.cancelled():
            return
        try:
            task.result()
        except Exception:
            LOGGER.error(
                "channel_event=background_action_failed binding_id_hash=%s result=failed error_code=BACKGROUND_ACTION_FAILURE",
                _short_hash(self._binding_id),
            )

    async def _reply_and_complete(self, message: IncomingMessage, content: str) -> None:
        try:
            await self._channel.send(
                OutgoingMessage(
                    chat_id=message.chat_id,
                    content=content,
                    reply_to_message_id=message.message_id,
                )
            )
            await self._state_store.mark_replied(message.execution_id)
        except Exception:
            self._log(logging.ERROR, "reply_failed", message, "REPLY_OR_STATE_FAILURE")
            await self._mark_executed(message)

    async def _safe_reply(self, message: IncomingMessage, content: str) -> None:
        try:
            await self._channel.send(
                OutgoingMessage(
                    chat_id=message.chat_id,
                    content=content,
                    reply_to_message_id=message.message_id,
                )
            )
        except Exception:
            self._log(logging.ERROR, "reply_failed", message, "REPLY_FAILED")

    async def _mark_replied(self, message: IncomingMessage) -> None:
        try:
            await self._state_store.mark_replied(message.execution_id)
        except Exception:
            self._log(logging.ERROR, "state_failed", message, "REDIS_MARK_REPLIED")

    async def _mark_executed(self, message: IncomingMessage) -> None:
        try:
            await self._state_store.mark_executed(message.execution_id)
        except Exception:
            self._log(logging.ERROR, "state_failed", message, "REDIS_MARK_EXECUTED")

    def _log(
        self,
        level: int,
        event: str,
        message: IncomingMessage,
        error_code: str,
        *,
        result: str = "failed",
        execution_ms: int | None = None,
        session_id: str = "",
    ) -> None:
        LOGGER.log(
            level,
            "channel_event=%s binding_id_hash=%s message_id_hash=%s sender_id_hash=%s chat_id_hash=%s session_id_hash=%s execution_ms=%s result=%s error_code=%s",
            event,
            _short_hash(self._binding_id),
            _short_hash(message.execution_id),
            _short_hash(message.sender_id),
            _short_hash(message.chat_id),
            _short_hash(session_id) if session_id else "",
            execution_ms if execution_ms is not None else "",
            result,
            error_code,
        )
