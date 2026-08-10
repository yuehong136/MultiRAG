#
#  Copyright 2024 The InfiniFlow Authors. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#

from __future__ import annotations

import hashlib
import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar, Literal, Protocol, runtime_checkable

from api.channel_capabilities import EffectiveReplyCapabilities

LOGGER = logging.getLogger(__name__)
_DEFAULT_REPLY_CAPABILITIES = EffectiveReplyCapabilities(
    progressive_reply=True,
    cancel_queued=True,
    cancel_running=True,
    regenerate=True,
    retry=True,
    feedback=True,
)


def _short_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


@dataclass(slots=True, init=False)
class IncomingMessage:
    """Normalized transport data delivered to the application handler.

    Identity is deliberately transport data at this layer. A trusted
    application service must resolve ``sender_id`` into a Principal before an
    Agent, LLM, or tool is invoked.
    """

    # Keep the upstream field order through ``raw``. MultiRAG-only metadata is
    # keyword-only in ``__init__`` so an upstream positional constructor keeps
    # exactly the same meaning when ported into this repository.
    channel: str
    account_id: str
    chat_id: str
    chat_type: str
    message_id: str
    sender_id: str
    content: str
    raw: Any = None
    message_type: str = "text"
    sender_type: str = ""
    event_id: str = ""
    create_time: str = ""
    request_id: str = ""
    operation: Literal["message", "regenerate"] = "message"

    def __init__(
        self,
        channel: str,
        account_id: str,
        chat_id: str,
        chat_type: str,
        message_id: str,
        sender_id: str,
        text: str | None = None,
        raw: Any = None,
        *,
        content: str | None = None,
        message_type: str = "text",
        sender_type: str = "",
        event_id: str = "",
        create_time: str = "",
        request_id: str = "",
        operation: Literal["message", "regenerate"] = "message",
    ) -> None:
        """Accept the MultiRAG ``text`` contract and local ``content`` alias."""

        if text is not None and content is not None and text != content:
            raise ValueError("content and text cannot contain different values")
        value = content if content is not None else text
        if value is None:
            raise ValueError("content is required")

        self.channel = channel
        self.account_id = account_id
        self.chat_id = chat_id
        self.chat_type = chat_type
        self.message_id = message_id
        self.sender_id = sender_id
        self.content = value
        self.raw = raw
        self.message_type = message_type
        self.sender_type = sender_type
        self.event_id = event_id
        self.create_time = create_time
        self.request_id = request_id
        self.operation = operation

    @property
    def text(self) -> str:
        """Compatibility alias used by the upstream channel contract."""

        return self.content

    @property
    def execution_id(self) -> str:
        """Stable idempotency key for this execution attempt.

        Ordinary provider messages keep using their message ID. Internally
        generated attempts (for example a card's "regenerate" action) retain
        the original source message for reply threading while carrying a fresh
        request ID for execution and delivery idempotency.
        """

        return self.request_id or self.message_id


@dataclass(slots=True, init=False)
class OutgoingMessage:
    chat_id: str
    content: str
    reply_to_message_id: str | None = None

    def __init__(
        self,
        chat_id: str,
        content: str | None = None,
        reply_to_message_id: str | None = None,
        *,
        text: str | None = None,
    ) -> None:
        """Create an outbound message with modern ``content`` or upstream ``text``.

        Keeping the keyword alias lets upstream channel/application code be
        ported independently while exposing one canonical stored value.
        """

        if content is not None and text is not None and content != text:
            raise ValueError("content and text cannot contain different values")
        value = content if content is not None else text
        if value is None:
            raise ValueError("content is required")
        self.chat_id = chat_id
        self.content = value
        self.reply_to_message_id = reply_to_message_id

    @property
    def text(self) -> str:
        """Compatibility alias used by the upstream channel contract."""

        return self.content


MessageHandler = Callable[[IncomingMessage], Awaitable[None]]


class ReplySessionState(StrEnum):
    """Provider-neutral reply lifecycle states."""

    OPEN = "open"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ReplyStatus(StrEnum):
    """User-visible lifecycle of one source message's reply."""

    QUEUED = "queued"
    RUNNING = "running"
    FINAL = "final"
    ERROR = "error"
    CANCELLED = "cancelled"


class ReplyActionKind(StrEnum):
    """Low-risk actions supported by a completed or active reply."""

    CANCEL = "cancel"
    REGENERATE = "regenerate"
    RETRY = "retry"
    HELPFUL = "helpful"
    UNHELPFUL = "unhelpful"


@dataclass(frozen=True, slots=True)
class ReplyActionIds:
    """Opaque server-generated IDs rendered into provider controls."""

    cancel: str = ""
    regenerate: str = ""
    helpful: str = ""
    unhelpful: str = ""
    retry: str = ""


@dataclass(frozen=True, slots=True)
class ReplyContext:
    """Initial lifecycle state and controls for a reply session."""

    status: ReplyStatus = ReplyStatus.RUNNING
    queue_position: int = 0
    actions: ReplyActionIds = ReplyActionIds()
    capabilities: EffectiveReplyCapabilities = _DEFAULT_REPLY_CAPABILITIES


@dataclass(frozen=True, slots=True)
class ChannelAction:
    """Provider-normalized card interaction with no trusted business data."""

    action_id: str
    operator_id: str
    chat_id: str
    message_id: str
    event_id: str


@dataclass(frozen=True, slots=True)
class ChannelActionResponse:
    """Small synchronous acknowledgement returned to the provider."""

    toast_type: str
    content: str


class ReplySessionStateError(RuntimeError):
    """Raised when a reply lifecycle attempts an illegal transition."""


@runtime_checkable
class ReplySession(Protocol):
    """Append-only reply lifecycle implemented by each outbound provider."""

    @property
    def state(self) -> ReplySessionState: ...

    @property
    def reply_message_id(self) -> str: ...

    async def append(self, content: str) -> None: ...

    async def set_status(
        self,
        status: ReplyStatus,
        *,
        queue_position: int = 0,
    ) -> None: ...

    async def complete(self) -> None: ...

    async def fail(self, error_code: str) -> None: ...

    async def cancel(self) -> None: ...

    async def acknowledge_feedback(self, *, helpful: bool) -> None: ...


ActionHandler = Callable[[ChannelAction], Awaitable[ChannelActionResponse]]


class Channel(ABC):
    """One configured bot identity on one messaging platform."""

    channel_id: ClassVar[str]
    account_id: str

    def __init__(self) -> None:
        self._handler: MessageHandler | None = None
        self._action_handler: ActionHandler | None = None

    def set_message_handler(self, handler: MessageHandler) -> None:
        self._handler = handler

    def set_action_handler(self, handler: ActionHandler) -> None:
        """Register the application callback for provider card actions."""

        self._action_handler = handler

    async def _dispatch(self, message: IncomingMessage) -> None:
        if self._handler is None:
            return
        try:
            await self._handler(message)
        except Exception:  # framework boundary: one bad message must not kill the channel
            LOGGER.error(
                "channel_event=dispatch_failed channel=%s account_id_hash=%s message_id_hash=%s result=failed error_code=HANDLER_FAILURE",
                self.channel_id,
                _short_hash(self.account_id),
                _short_hash(message.message_id),
            )

    async def _dispatch_action(self, action: ChannelAction) -> ChannelActionResponse:
        if self._action_handler is None:
            return ChannelActionResponse(toast_type="warning", content="该操作当前不可用。")
        try:
            return await self._action_handler(action)
        except Exception:  # callback boundary: acknowledge safely within the provider deadline
            LOGGER.error(
                "channel_event=action_dispatch_failed channel=%s account_id_hash=%s action_id_hash=%s result=failed error_code=ACTION_HANDLER_FAILURE",
                self.channel_id,
                _short_hash(self.account_id),
                _short_hash(action.action_id),
            )
            return ChannelActionResponse(toast_type="error", content="操作失败，请稍后再试。")

    async def begin_reply(
        self,
        source: IncomingMessage,
        *,
        max_content_chars: int,
        context: ReplyContext | None = None,
    ) -> ReplySession:
        """Begin a buffered text reply unless a provider offers progression."""

        from api.channels.reply_session import BufferedReplySession

        return BufferedReplySession(
            channel=self,
            source=source,
            max_content_chars=max_content_chars,
            context=context or ReplyContext(),
        )

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def send(self, message: OutgoingMessage) -> None: ...
