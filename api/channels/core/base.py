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
from typing import Any, ClassVar

LOGGER = logging.getLogger(__name__)


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
    ) -> None:
        """Accept the RAGFlow ``text`` contract and local ``content`` alias."""

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

    @property
    def text(self) -> str:
        """Compatibility alias used by the upstream channel contract."""

        return self.content


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


class Channel(ABC):
    """One configured bot identity on one messaging platform."""

    channel_id: ClassVar[str]
    account_id: str

    def __init__(self) -> None:
        self._handler: MessageHandler | None = None

    def set_message_handler(self, handler: MessageHandler) -> None:
        self._handler = handler

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

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def send(self, message: OutgoingMessage) -> None: ...
