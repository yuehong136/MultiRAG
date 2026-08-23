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
import json
import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
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
_FORM_VALUE_MAX_FIELDS = 32
_FORM_VALUE_MAX_BYTES = 32_768
_FORM_VALUE_KEY_MAX_CHARS = 64
_FORM_VALUE_TEXT_MAX_CHARS = 4_096
_FORM_VALUE_LIST_MAX_ITEMS = 32
_FORM_ACTION_TEXT_MAX_CHARS = 255
_FORM_ACTION_MAX_REVISION = 2**63 - 1

type ChannelFormFieldValue = str | bool | tuple[str, ...]


def _short_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True, slots=True, init=False)
class IncomingIdentityIdentifier:
    """One bounded external identifier extracted by a Channel adapter."""

    kind: str
    value: str = field(repr=False)

    def __init__(self, kind: Any, value: Any) -> None:
        # ``Any`` is intentional at this untrusted transport boundary. Validate
        # before assigning so runtime type instrumentation cannot turn malformed
        # SDK data into a different exception class.
        if type(kind) is not str or type(value) is not str:
            raise ValueError("incoming identity identifier is invalid")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "value", value)
        self.__post_init__()

    def __post_init__(self) -> None:
        if type(self.kind) is not str or type(self.value) is not str:
            raise ValueError("incoming identity identifier is invalid")
        if (
            not self.kind
            or len(self.kind) > 64
            or not self.kind.strip()
            or self.kind != self.kind.strip()
            or not self.value
            or len(self.value) > 255
            or not self.value.strip()
            or self.value != self.value.strip()
        ):
            raise ValueError("incoming identity identifier is invalid")


@dataclass(frozen=True, slots=True, init=False)
class IncomingIdentityAssertion:
    """Provider identity material normalized by a Channel but not yet trusted."""

    provider: str
    provider_tenant_key: str | None = field(default=None, repr=False)
    identifiers: tuple[IncomingIdentityIdentifier, ...] = field(default=(), repr=False)

    def __init__(
        self,
        provider: Any,
        provider_tenant_key: Any = None,
        identifiers: Any = (),
    ) -> None:
        if (
            type(provider) is not str
            or (provider_tenant_key is not None and type(provider_tenant_key) is not str)
            or type(identifiers) is not tuple
            or any(type(identifier) is not IncomingIdentityIdentifier for identifier in identifiers)
        ):
            raise ValueError("incoming identity assertion is invalid")
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "provider_tenant_key", provider_tenant_key)
        object.__setattr__(self, "identifiers", identifiers)
        self.__post_init__()

    def __post_init__(self) -> None:
        if (
            type(self.provider) is not str
            or (self.provider_tenant_key is not None and type(self.provider_tenant_key) is not str)
            or type(self.identifiers) is not tuple
            or any(type(identifier) is not IncomingIdentityIdentifier for identifier in self.identifiers)
        ):
            raise ValueError("incoming identity assertion is invalid")
        if (
            not self.provider
            or len(self.provider) > 64
            or not self.provider.strip()
            or self.provider != self.provider.strip()
            or (
                self.provider_tenant_key is not None
                and (not self.provider_tenant_key or len(self.provider_tenant_key) > 255 or not self.provider_tenant_key.strip() or self.provider_tenant_key != self.provider_tenant_key.strip())
            )
            or not 1 <= len(self.identifiers) <= 8
        ):
            raise ValueError("incoming identity assertion is invalid")
        kinds = [identifier.kind for identifier in self.identifiers]
        if len(kinds) != len(set(kinds)):
            raise ValueError("incoming identity identifier kinds must be unique")


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
    identity: IncomingIdentityAssertion | None = None
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
        identity: IncomingIdentityAssertion | None = None,
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
        self.identity = identity
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
    AWAITING_INPUT = "awaiting_input"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ReplyStatus(StrEnum):
    """User-visible lifecycle of one source message's reply."""

    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_INPUT = "awaiting_input"
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


@dataclass(frozen=True, slots=True, init=False)
class ChannelFormAction:
    """Bounded native-form submission with untrusted provider identity.

    This contract is intentionally separate from ``ChannelAction``. Low-risk
    reply controls keep their smaller handler, while a form submission must be
    persisted and resolved into a trusted Principal by the application before
    any tool can resume.
    """

    action_id: str = field(repr=False)
    nonce: str = field(repr=False)
    revision: int
    action: Literal["accept", "decline", "cancel"]
    form_value: Mapping[str, ChannelFormFieldValue] = field(repr=False)
    identity: IncomingIdentityAssertion = field(repr=False)
    chat_id: str = field(repr=False)
    message_id: str = field(repr=False)
    event_id: str = field(repr=False)

    def __init__(
        self,
        *,
        action_id: Any,
        nonce: Any,
        revision: Any,
        action: Any = "accept",
        form_value: Any,
        identity: Any,
        chat_id: Any,
        message_id: Any,
        event_id: Any,
    ) -> None:
        if type(revision) is not int or not 1 <= revision <= _FORM_ACTION_MAX_REVISION:
            raise ValueError("channel form action revision is invalid")
        if type(identity) is not IncomingIdentityAssertion:
            raise ValueError("channel form action identity is invalid")
        if action not in {"accept", "decline", "cancel"}:
            raise ValueError("channel form action response is invalid")
        for name, value in (
            ("action_id", action_id),
            ("nonce", nonce),
            ("chat_id", chat_id),
            ("message_id", message_id),
            ("event_id", event_id),
        ):
            if not _is_bounded_form_action_text(value):
                raise ValueError(f"channel form action {name} is invalid")

        object.__setattr__(self, "action_id", action_id)
        object.__setattr__(self, "nonce", nonce)
        object.__setattr__(self, "revision", revision)
        object.__setattr__(self, "action", action)
        object.__setattr__(self, "form_value", _normalize_channel_form_value(form_value))
        object.__setattr__(self, "identity", identity)
        object.__setattr__(self, "chat_id", chat_id)
        object.__setattr__(self, "message_id", message_id)
        object.__setattr__(self, "event_id", event_id)


@dataclass(frozen=True, slots=True)
class ChannelActionResponse:
    """Small synchronous acknowledgement returned to the provider."""

    toast_type: str
    content: str


class ReplySessionStateError(RuntimeError):
    """Raised when a reply lifecycle attempts an illegal transition."""


@runtime_checkable
class ReplySession(Protocol):
    """Provider-neutral reply lifecycle implemented by each outbound provider.

    Incremental content is appended while a target runs. A target may publish
    one authoritative terminal snapshot when its final projection adds
    citations or other decorations that cannot be represented as an append.
    """

    @property
    def state(self) -> ReplySessionState: ...

    @property
    def reply_message_id(self) -> str: ...

    async def append(self, content: str) -> None: ...

    async def replace(self, content: str) -> None: ...

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


@runtime_checkable
class InteractionReplySession(ReplySession, Protocol):
    """Reply session that can stop progression before a native input form."""

    async def pause_for_interaction(self) -> str:
        """Finish streaming and return the mutable provider reply message ID."""

        ...


ActionHandler = Callable[[ChannelAction], Awaitable[ChannelActionResponse]]
FormActionHandler = Callable[[ChannelFormAction], Awaitable[ChannelActionResponse]]


class Channel(ABC):
    """One configured bot identity on one messaging platform."""

    channel_id: ClassVar[str]
    account_id: str

    def __init__(self) -> None:
        self._handler: MessageHandler | None = None
        self._action_handler: ActionHandler | None = None
        self._form_action_handler: FormActionHandler | None = None

    def set_message_handler(self, handler: MessageHandler) -> None:
        self._handler = handler

    def set_action_handler(self, handler: ActionHandler) -> None:
        """Register the application callback for provider card actions."""

        self._action_handler = handler

    def set_form_action_handler(self, handler: FormActionHandler) -> None:
        """Register the persistence-only callback for native form submits."""

        self._form_action_handler = handler

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

    async def _dispatch_form_action(
        self,
        action: ChannelFormAction,
    ) -> ChannelActionResponse:
        if self._form_action_handler is None:
            return ChannelActionResponse(toast_type="warning", content="该表单当前不可用。")
        try:
            return await self._form_action_handler(action)
        except Exception:  # callback boundary: persist/claim failures must still ACK safely
            LOGGER.error(
                "channel_event=form_action_dispatch_failed channel=%s account_id_hash=%s action_id_hash=%s result=failed error_code=FORM_ACTION_HANDLER_FAILURE",
                self.channel_id,
                _short_hash(self.account_id),
                _short_hash(action.action_id),
            )
            return ChannelActionResponse(toast_type="error", content="提交失败，请稍后再试。")

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


def _is_bounded_form_action_text(value: object) -> bool:
    return type(value) is str and 0 < len(value) <= _FORM_ACTION_TEXT_MAX_CHARS and value == value.strip() and all(ord(character) >= 0x20 for character in value)


def _normalize_channel_form_value(
    form_value: object,
) -> Mapping[str, ChannelFormFieldValue]:
    if type(form_value) is not dict or len(form_value) > _FORM_VALUE_MAX_FIELDS:
        raise ValueError("channel form value is invalid")

    frozen: dict[str, ChannelFormFieldValue] = {}
    serializable: dict[str, str | bool | list[str]] = {}
    for key, value in form_value.items():
        if type(key) is not str or not 0 < len(key) <= _FORM_VALUE_KEY_MAX_CHARS or key != key.strip() or any(ord(character) < 0x20 for character in key):
            raise ValueError("channel form value field name is invalid")
        if type(value) is str:
            if len(value) > _FORM_VALUE_TEXT_MAX_CHARS:
                raise ValueError("channel form value text is too large")
            frozen[key] = value
            serializable[key] = value
        elif type(value) is bool:
            frozen[key] = value
            serializable[key] = value
        elif type(value) is list:
            if len(value) > _FORM_VALUE_LIST_MAX_ITEMS or any(type(item) is not str or len(item) > _FORM_VALUE_TEXT_MAX_CHARS for item in value):
                raise ValueError("channel form value selection is invalid")
            frozen[key] = tuple(value)
            serializable[key] = list(value)
        else:
            raise ValueError("channel form value field type is invalid")

    payload = json.dumps(
        serializable,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    if len(payload) > _FORM_VALUE_MAX_BYTES:
        raise ValueError("channel form value is too large")
    return MappingProxyType(frozen)
