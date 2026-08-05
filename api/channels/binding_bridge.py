"""Feishu message bridge for trusted server-side Channel bindings."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from api.channels.agent_bridge import (
    DEMO_ONLY_TEXT,
    QUESTION_TOO_LONG_TEXT,
    SERVICE_UNAVAILABLE_TEXT,
    SESSION_RESET_TEXT,
    TEXT_ONLY_TEXT,
    AgentExecutionError,
    AgentReply,
)
from api.channels.core.base import Channel, IncomingMessage, OutgoingMessage
from api.channels.state_store import ChannelStateStore, binding_conversation_key

LOGGER = logging.getLogger(__name__)


@runtime_checkable
class BindingExecutor(Protocol):
    async def ask(
        self,
        *,
        question: str,
        event_id: str,
        conversation_key: str,
        provider: str,
        subject: str,
        conversation: str,
    ) -> AgentReply: ...

    async def reset(self, *, conversation_key: str) -> None: ...


def _short_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


@dataclass(slots=True)
class _ConversationLock:
    lock: asyncio.Lock
    users: int = 0


class FeishuBindingBridge:
    """Apply transport policy, then invoke a fixed trusted binding."""

    def __init__(
        self,
        *,
        channel: Channel,
        executor: BindingExecutor,
        state_store: ChannelStateStore,
        binding_id: str,
        allowed_open_ids: set[str] | frozenset[str],
        max_question_chars: int,
        private_chat_only: bool = True,
    ) -> None:
        self._channel = channel
        self._executor = executor
        self._state_store = state_store
        self._binding_id = binding_id
        self._allowed_open_ids = frozenset(allowed_open_ids)
        self._max_question_chars = max_question_chars
        self._private_chat_only = private_chat_only
        self._locks: dict[str, _ConversationLock] = {}

    async def handle_message(self, message: IncomingMessage) -> None:
        # The admin toggle behind this was collected by the form, validated by
        # the service and stored in the binding row, and then never reached a
        # runner: the filter below was unconditional, so the switch was
        # decoration. Defaults to the old behaviour, which is also the
        # fail-safe one -- see ``RuntimeBindingConfig.private_chat_only``.
        if self._private_chat_only and message.chat_type != "p2p":
            return
        if getattr(message, "sender_type", "") != "user":
            return
        if not message.message_id or not message.chat_id or not message.sender_id:
            self._log(logging.WARNING, "event_rejected", message, "MESSAGE_IDENTITY_MISSING")
            return

        conversation = binding_conversation_key(
            self._binding_id,
            message.channel,
            message.chat_id,
            message.sender_id,
        )
        entry = self._locks.setdefault(conversation, _ConversationLock(asyncio.Lock()))
        entry.users += 1
        try:
            async with entry.lock:
                await self._handle_serialized(message, conversation)
        finally:
            entry.users -= 1
            if entry.users == 0 and self._locks.get(conversation) is entry:
                self._locks.pop(conversation, None)

    async def _handle_serialized(self, message: IncomingMessage, conversation_key: str) -> None:
        try:
            claimed = await self._state_store.claim_message(message.message_id)
        except Exception:
            self._log(logging.ERROR, "state_failed", message, "REDIS_CLAIM_FAILED")
            await self._safe_reply(message, SERVICE_UNAVAILABLE_TEXT)
            return
        if not claimed:
            self._log(logging.INFO, "duplicate_dropped", message, "", result="duplicate")
            return

        if self._allowed_open_ids and message.sender_id not in self._allowed_open_ids:
            await self._reply_and_complete(message, DEMO_ONLY_TEXT)
            return
        if message.message_type != "text":
            await self._reply_and_complete(message, TEXT_ONLY_TEXT)
            return
        question = message.text.strip()
        if not question:
            await self._mark_replied(message)
            return
        if len(question) > self._max_question_chars:
            await self._reply_and_complete(message, QUESTION_TOO_LONG_TEXT)
            return
        if question == "/reset":
            try:
                await self._executor.reset(conversation_key=conversation_key)
            except Exception:
                await self._mark_executed(message)
                await self._safe_reply(message, SERVICE_UNAVAILABLE_TEXT)
                return
            await self._reply_and_complete(message, SESSION_RESET_TEXT)
            return

        started_at = time.monotonic()
        try:
            reply = await self._executor.ask(
                question=question,
                event_id=message.message_id,
                conversation_key=conversation_key,
                provider=message.channel,
                subject=message.sender_id,
                conversation=message.chat_id,
            )
        except AgentExecutionError as exc:
            self._log(logging.ERROR, "execution_failed", message, exc.code)
            await self._mark_executed(message)
            await self._safe_reply(message, SERVICE_UNAVAILABLE_TEXT)
            return
        except Exception:
            self._log(logging.ERROR, "execution_failed", message, "CHANNEL_EXECUTION_FAILURE")
            await self._mark_executed(message)
            await self._safe_reply(message, SERVICE_UNAVAILABLE_TEXT)
            return

        elapsed_ms = round((time.monotonic() - started_at) * 1000)
        try:
            await self._channel.send(
                OutgoingMessage(
                    chat_id=message.chat_id,
                    content=reply.content,
                    reply_to_message_id=message.message_id,
                )
            )
            await self._state_store.mark_replied(message.message_id)
        except Exception:
            self._log(logging.ERROR, "reply_failed", message, "REPLY_OR_STATE_FAILURE")
            await self._mark_executed(message)
            return
        self._log(
            logging.INFO,
            "execution_completed",
            message,
            "",
            result="ok",
            execution_ms=elapsed_ms,
            session_id=reply.session_id,
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
            await self._state_store.mark_replied(message.message_id)
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
            await self._state_store.mark_replied(message.message_id)
        except Exception:
            self._log(logging.ERROR, "state_failed", message, "REDIS_MARK_REPLIED")

    async def _mark_executed(self, message: IncomingMessage) -> None:
        try:
            await self._state_store.mark_executed(message.message_id)
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
            _short_hash(message.message_id),
            _short_hash(message.sender_id),
            _short_hash(message.chat_id),
            _short_hash(session_id) if session_id else "",
            execution_ms if execution_ms is not None else "",
            result,
            error_code,
        )
