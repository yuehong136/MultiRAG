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

import asyncio
import hashlib
import inspect
import json
import logging
import threading
import time
import warnings
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import import_module
from typing import Any, Protocol, runtime_checkable

from ..core.base import (
    Channel,
    ChannelAction,
    ChannelActionResponse,
    ChannelFormAction,
    IncomingIdentityAssertion,
    IncomingIdentityIdentifier,
    IncomingMessage,
    OutgoingMessage,
    ReplyContext,
    ReplySession,
)
from ..identity_events import (
    ChannelIdentityEvent,
    ChannelIdentityIdentifier,
    ChannelIdentitySubject,
    IdentityEventHandler,
)
from .reply import CARD_ANSWER_ELEMENT_ID, FeishuProgressiveReplySession

LOGGER = logging.getLogger(__name__)
_IDENTITY_EVENT_TIMEOUT_SECONDS = 2.5


class FeishuDependencyError(RuntimeError):
    """Raised when the optional Feishu SDK is unavailable."""


class FeishuSendError(RuntimeError):
    """Raised when Feishu rejects an outbound message."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"Feishu send failed with code {code}")


@dataclass(frozen=True, slots=True)
class FeishuAccount:
    account_id: str
    app_id: str
    app_secret: str = field(repr=False)
    domain: str = "feishu"  # "feishu" or "lark"


@runtime_checkable
class _FeishuSDK(Protocol):
    """Small SDK seam: production uses lark-oapi; tests use a local fake."""

    def build_rest_client(self, account: FeishuAccount) -> Any: ...

    def bind_ws_loop(self, loop: asyncio.AbstractEventLoop) -> None: ...

    def build_ws_client(
        self,
        account: FeishuAccount,
        message_callback: Any,
        action_callback: Any,
        identity_callback: Any | None,
    ) -> Any: ...

    def card_action_response(self, response: ChannelActionResponse) -> Any: ...

    def reply_message(
        self,
        client: Any,
        message_id: str,
        content: str,
        *,
        message_type: str = "text",
        delivery_uuid: str | None = None,
    ) -> Any: ...

    def create_message(self, client: Any, chat_id: str, content: str) -> Any: ...

    def create_card(self, client: Any, card_json: str) -> Any: ...

    def patch_card_message(
        self,
        client: Any,
        message_id: str,
        card_json: str,
    ) -> Any: ...

    def update_card_text(
        self,
        client: Any,
        card_id: str,
        content: str,
        *,
        sequence: int,
        delivery_uuid: str,
    ) -> Any: ...

    def batch_update_card(
        self,
        client: Any,
        card_id: str,
        actions: list[dict[str, object]],
        *,
        sequence: int,
        delivery_uuid: str,
    ) -> Any: ...

    def finish_streaming_card(
        self,
        client: Any,
        card_id: str,
        *,
        sequence: int,
        delivery_uuid: str,
    ) -> Any: ...

    def create_reaction(self, client: Any, message_id: str, emoji_type: str) -> Any: ...

    def delete_reaction(self, client: Any, message_id: str, reaction_id: str) -> Any: ...

    def stop_ws_client(self, client: Any) -> Any: ...

    def response_success(self, response: Any) -> bool: ...

    def response_code(self, response: Any) -> Any: ...


class _LarkOapiSDK:
    """Lazy lark-oapi adapter so importing channels keeps the SDK optional."""

    def __init__(self) -> None:
        try:
            # lark-oapi 1.7.x still imports pkg_resources through its generated
            # protobuf namespace. Keep this known third-party deprecation out
            # of the Channel console without hiding other SDK warnings.
            with warnings.catch_warnings():
                warnings.filterwarnings(
                    "ignore",
                    message=r"pkg_resources is deprecated as an API.*",
                    category=UserWarning,
                    module=r"lark_oapi\.ws\.pb\.google",
                )
                self._lark = import_module("lark_oapi")
                self._ws_module = import_module("lark_oapi.ws.client")
                im_api = import_module("lark_oapi.api.im.v1")
                cardkit_api = import_module("lark_oapi.api.cardkit.v1")
                callback_models = import_module("lark_oapi.event.callback.model.p2_card_action_trigger")
        except ImportError as error:
            raise FeishuDependencyError("Feishu channel requires the optional 'lark-oapi' dependency") from error

        self._create_message_request = im_api.CreateMessageRequest
        self._create_message_body = im_api.CreateMessageRequestBody
        self._reply_message_request = im_api.ReplyMessageRequest
        self._reply_message_body = im_api.ReplyMessageRequestBody
        self._patch_message_request = im_api.PatchMessageRequest
        self._patch_message_body = im_api.PatchMessageRequestBody
        self._create_reaction_request = im_api.CreateMessageReactionRequest
        self._create_reaction_body = im_api.CreateMessageReactionRequestBody
        self._delete_reaction_request = im_api.DeleteMessageReactionRequest
        self._emoji = im_api.Emoji
        self._create_card_request = cardkit_api.CreateCardRequest
        self._create_card_body = cardkit_api.CreateCardRequestBody
        self._batch_update_card_request = cardkit_api.BatchUpdateCardRequest
        self._batch_update_card_body = cardkit_api.BatchUpdateCardRequestBody
        self._content_card_element_request = cardkit_api.ContentCardElementRequest
        self._content_card_element_body = cardkit_api.ContentCardElementRequestBody
        self._settings_card_request = cardkit_api.SettingsCardRequest
        self._settings_card_body = cardkit_api.SettingsCardRequestBody
        self._card_action_response = callback_models.P2CardActionTriggerResponse

    def _domain(self, domain: str) -> Any:
        if domain == "lark":
            return self._lark.LARK_DOMAIN
        if domain == "feishu":
            return self._lark.FEISHU_DOMAIN
        raise ValueError("Feishu account domain must be 'feishu' or 'lark'")

    def _log_level(self) -> Any:
        # SDK INFO logs the complete WebSocket URL, whose query string contains
        # connection credentials. MultiRAG emits its own sanitized lifecycle
        # events instead.
        return getattr(self._lark.LogLevel, "WARNING", None)

    def build_rest_client(self, account: FeishuAccount) -> Any:
        builder = self._lark.Client.builder().app_id(account.app_id).app_secret(account.app_secret).domain(self._domain(account.domain))
        log_level = self._log_level()
        if log_level is not None:
            builder = builder.log_level(log_level)
        return builder.build()

    def bind_ws_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        # lark-oapi 1.x stores this loop at module scope. MultiRAG's first MVP
        # therefore intentionally supports one Feishu account per process.
        self._ws_module.loop = loop

    def build_ws_client(
        self,
        account: FeishuAccount,
        message_callback: Any,
        action_callback: Any,
        identity_callback: Any | None,
    ) -> Any:
        builder = (
            self._lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(message_callback)
            .register_p2_card_action_trigger(action_callback)
            # These subscriptions are transport noise for MultiRAG. Reaction
            # events are generated by our own typing indicator, while the read
            # and P2P-entered events may be enabled in the same Feishu app.
            # Leaving any of them unregistered makes lark-oapi emit a
            # misleading processor-not-found ERROR for a successful action.
            .register_p2_im_message_message_read_v1(self._ignore_transport_event)
            .register_p2_im_message_reaction_created_v1(self._ignore_transport_event)
            .register_p2_im_message_reaction_deleted_v1(self._ignore_transport_event)
            .register_p2_im_chat_access_event_bot_p2p_chat_entered_v1(self._ignore_transport_event)
        )
        if identity_callback is not None:
            builder = (
                builder.register_p2_contact_user_created_v3(identity_callback)
                .register_p2_contact_user_updated_v3(identity_callback)
                .register_p2_contact_user_deleted_v3(identity_callback)
                .register_p2_contact_scope_updated_v3(identity_callback)
            )
        handler = builder.build()
        kwargs: dict[str, Any] = {
            "domain": self._domain(account.domain),
            "event_handler": handler,
            "auto_reconnect": True,
        }
        log_level = self._log_level()
        if log_level is not None:
            kwargs["log_level"] = log_level
        return self._lark.ws.Client(
            account.app_id,
            account.app_secret,
            **kwargs,
        )

    def card_action_response(self, response: ChannelActionResponse) -> Any:
        return self._card_action_response(
            {
                "toast": {
                    "type": response.toast_type,
                    "content": response.content,
                }
            }
        )

    @staticmethod
    def _ignore_transport_event(data: Any) -> None:
        del data

    def reply_message(
        self,
        client: Any,
        message_id: str,
        content: str,
        *,
        message_type: str = "text",
        delivery_uuid: str | None = None,
    ) -> Any:
        body = self._reply_message_body.builder().content(content).msg_type(message_type)
        if delivery_uuid:
            body = body.uuid(delivery_uuid)
        request = self._reply_message_request.builder().message_id(message_id).request_body(body.build()).build()
        return client.im.v1.message.reply(request)

    def create_message(self, client: Any, chat_id: str, content: str) -> Any:
        request = (
            self._create_message_request.builder().receive_id_type("chat_id").request_body(self._create_message_body.builder().receive_id(chat_id).content(content).msg_type("text").build()).build()
        )
        return client.im.v1.message.create(request)

    def create_card(self, client: Any, card_json: str) -> Any:
        request = self._create_card_request.builder().request_body(self._create_card_body.builder().type("card_json").data(card_json).build()).build()
        return client.cardkit.v1.card.create(request)

    def patch_card_message(
        self,
        client: Any,
        message_id: str,
        card_json: str,
    ) -> Any:
        request = self._patch_message_request.builder().message_id(message_id).request_body(self._patch_message_body.builder().content(card_json).build()).build()
        return client.im.v1.message.patch(request)

    def update_card_text(
        self,
        client: Any,
        card_id: str,
        content: str,
        *,
        sequence: int,
        delivery_uuid: str,
    ) -> Any:
        request = (
            self._content_card_element_request.builder()
            .card_id(card_id)
            .element_id(CARD_ANSWER_ELEMENT_ID)
            .request_body(self._content_card_element_body.builder().content(content).sequence(sequence).uuid(delivery_uuid).build())
            .build()
        )
        return client.cardkit.v1.card_element.content(request)

    def batch_update_card(
        self,
        client: Any,
        card_id: str,
        actions: list[dict[str, object]],
        *,
        sequence: int,
        delivery_uuid: str,
    ) -> Any:
        body = self._batch_update_card_body.builder().actions(json.dumps(actions, ensure_ascii=False, separators=(",", ":"))).sequence(sequence).uuid(delivery_uuid).build()
        request = self._batch_update_card_request.builder().card_id(card_id).request_body(body).build()
        return client.cardkit.v1.card.batch_update(request)

    def finish_streaming_card(
        self,
        client: Any,
        card_id: str,
        *,
        sequence: int,
        delivery_uuid: str,
    ) -> Any:
        settings = json.dumps(
            {
                "config": {
                    "streaming_mode": False,
                    "summary": {"content": "回答完成"},
                }
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        request = self._settings_card_request.builder().card_id(card_id).request_body(self._settings_card_body.builder().settings(settings).sequence(sequence).uuid(delivery_uuid).build()).build()
        return client.cardkit.v1.card.settings(request)

    def create_reaction(self, client: Any, message_id: str, emoji_type: str) -> Any:
        reaction = self._emoji.builder().emoji_type(emoji_type).build()
        request = self._create_reaction_request.builder().message_id(message_id).request_body(self._create_reaction_body.builder().reaction_type(reaction).build()).build()
        return client.im.v1.message_reaction.create(request)

    def delete_reaction(self, client: Any, message_id: str, reaction_id: str) -> Any:
        request = self._delete_reaction_request.builder().message_id(message_id).reaction_id(reaction_id).build()
        return client.im.v1.message_reaction.delete(request)

    def stop_ws_client(self, client: Any) -> Any:
        # lark-oapi 1.x has no stable public shutdown API across releases.
        # Its receive coroutine loops while this private flag remains true, so
        # disable reconnect before asking the current connection to close.
        if hasattr(client, "_auto_reconnect"):
            client._auto_reconnect = False
        for attribute in ("stop", "_disconnect", "disconnect"):
            function = getattr(client, attribute, None)
            if callable(function):
                return function()
        return None

    def response_success(self, response: Any) -> bool:
        success = getattr(response, "success", None)
        return bool(success()) if callable(success) else False

    def response_code(self, response: Any) -> Any:
        return getattr(response, "code", "unknown")


def _string(value: Any) -> str:
    return "" if value is None else str(value)


def _short_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _first_non_empty(*values: Any) -> str:
    for value in values:
        normalized = _string(value)
        if normalized:
            return normalized
    return ""


def _optional_identity_text(value: Any) -> str | None:
    """Normalize one SDK identity value without coercing malformed input."""

    if value is None or value == "":
        return None
    if type(value) is not str or not value.strip() or value != value.strip():
        raise ValueError("Feishu identity field is invalid")
    return value


def _required_identity_text(value: Any, *, field_name: str) -> str:
    normalized = _optional_identity_text(value)
    if normalized is None:
        raise ValueError(f"Feishu identity field is missing: {field_name}")
    return normalized


def _directory_event_time(value: Any) -> datetime:
    raw = _required_identity_text(value, field_name="create_time")
    if len(raw) != 13 or not raw.isascii() or not raw.isdigit():
        raise ValueError("Feishu directory event time is invalid")
    milliseconds = int(raw)
    if not 946_684_800_000 <= milliseconds <= 4_102_444_800_000:
        raise ValueError("Feishu directory event time is invalid")
    return datetime.fromtimestamp(milliseconds / 1000, UTC)


def _directory_status(value: Any) -> str:
    fields = (
        "is_activated",
        "is_frozen",
        "is_resigned",
        "is_exited",
        "is_unjoin",
    )
    statuses = {name: getattr(value, name, None) for name in fields}
    if any(item is not None and type(item) is not bool for item in statuses.values()):
        raise ValueError("Feishu directory status is invalid")
    if any(item is None for item in statuses.values()):
        return "unknown"
    inactive = not statuses["is_activated"] or statuses["is_frozen"] or statuses["is_resigned"] or statuses["is_exited"] or statuses["is_unjoin"]
    return "inactive" if inactive else "active"


class FeishuChannel(Channel):
    """Feishu/Lark long-connection transport for one application account."""

    channel_id = "feishu"

    def __init__(
        self,
        account: FeishuAccount,
        *,
        sdk: _FeishuSDK | None = None,
        start_timeout_seconds: float = 30.0,
        stop_timeout_seconds: float = 5.0,
        stream_update_interval_seconds: float = 0.25,
    ) -> None:
        super().__init__()
        if start_timeout_seconds <= 0:
            raise ValueError("start_timeout_seconds must be positive")
        if stop_timeout_seconds <= 0:
            raise ValueError("stop_timeout_seconds must be positive")
        if stream_update_interval_seconds <= 0:
            raise ValueError("stream_update_interval_seconds must be positive")

        self.account = account
        self.account_id = account.account_id
        self._sdk = sdk or _LarkOapiSDK()
        self._start_timeout_seconds = start_timeout_seconds
        self._stop_timeout_seconds = stop_timeout_seconds
        self._stream_update_interval_seconds = stream_update_interval_seconds
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ws_loop: asyncio.AbstractEventLoop | None = None
        self._ws_client: Any = None
        self._ws_thread: threading.Thread | None = None
        self._thread_ready = threading.Event()
        self._thread_finished = threading.Event()
        self._stopping = threading.Event()
        self._rest = self._sdk.build_rest_client(account)
        self._identity_event_handler: IdentityEventHandler | None = None

    def set_identity_event_handler(self, handler: IdentityEventHandler) -> None:
        """Install the optional durable directory-event capability."""

        self._identity_event_handler = handler

    @property
    def is_running(self) -> bool:
        thread = self._ws_thread
        return bool(thread and thread.is_alive() and not self._thread_finished.is_set() and not self._stopping.is_set())

    async def start(self) -> None:
        if self.is_running:
            return
        if self._ws_thread is not None and self._ws_thread.is_alive():
            raise RuntimeError(f"Feishu WebSocket client '{self.account_id}' is still stopping")

        self._loop = asyncio.get_running_loop()
        self._thread_ready.clear()
        self._thread_finished.clear()
        self._stopping.clear()
        account_id_hash = _short_hash(self.account_id)
        LOGGER.info(
            "channel_event=ws_starting channel=feishu account_id_hash=%s result=pending",
            account_id_hash,
        )
        thread = threading.Thread(
            target=self._run_ws,
            name=f"feishu-ws-{self.account_id}",
            daemon=True,
        )
        self._ws_thread = thread
        thread.start()

        ready = await asyncio.to_thread(
            self._thread_ready.wait,
            self._start_timeout_seconds,
        )
        if not ready:
            await self.stop()
            raise RuntimeError(f"Feishu WebSocket client '{self.account_id}' did not initialize in time")
        if self._thread_finished.is_set() and self._ws_client is None:
            raise RuntimeError(f"Feishu WebSocket client '{self.account_id}' failed during startup")

        deadline = time.monotonic() + self._start_timeout_seconds
        while not self._connection_ready():
            if self._thread_finished.is_set():
                raise RuntimeError(f"Feishu WebSocket client '{self.account_id}' failed during startup")
            if time.monotonic() >= deadline:
                await self.stop()
                raise RuntimeError(f"Feishu WebSocket client '{self.account_id}' did not connect in time")
            await asyncio.sleep(0.05)

        LOGGER.info(
            "channel_event=ws_connected channel=feishu account_id_hash=%s result=ok",
            account_id_hash,
        )

    def _connection_ready(self) -> bool:
        client = self._ws_client
        if client is None:
            return False
        # lark-oapi 1.x exposes no public connection-state API. Its private
        # connection object is already required for deterministic shutdown;
        # use it when available, while keeping SDK fakes/future versions usable.
        if hasattr(client, "_conn"):
            return getattr(client, "_conn", None) is not None
        return True

    def _run_ws(self) -> None:
        # lark-oapi captures the current loop while building and starting its WS
        # client. Isolating it avoids re-entering the FastAPI application loop.
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.set_exception_handler(self._handle_loop_exception)
        self._ws_loop = loop
        self._sdk.bind_ws_loop(loop)
        try:
            self._ws_client = self._sdk.build_ws_client(
                self.account,
                self._on_message_receive,
                self._on_card_action,
                (self._on_identity_event if self._identity_event_handler is not None else None),
            )
            self._thread_ready.set()
            # lark-oapi start() blocks for the life of the connection.
            self._ws_client.start()
        except Exception:
            if self._stopping.is_set():
                # asyncio.run_until_complete raises RuntimeError when stop()
                # terminates the SDK loop. That is expected during shutdown.
                LOGGER.debug(
                    "channel_event=ws_thread_stopped channel=feishu account_id_hash=%s result=ok",
                    _short_hash(self.account_id),
                )
            else:
                LOGGER.error(
                    "channel_event=ws_crashed channel=feishu account_id_hash=%s result=failed error_code=FEISHU_WS_CRASHED",
                    _short_hash(self.account_id),
                )
        finally:
            self._thread_ready.set()
            self._cancel_pending_tasks(loop)
            self._ws_client = None
            self._ws_loop = None
            self._thread_finished.set()
            try:
                if not loop.is_closed():
                    loop.close()
            except Exception:
                LOGGER.debug(
                    "channel_event=ws_loop_close channel=feishu account_id_hash=%s result=failed error_code=FEISHU_WS_LOOP_CLOSE_FAILED",
                    _short_hash(self.account_id),
                )

    def _cancel_pending_tasks(self, loop: asyncio.AbstractEventLoop) -> None:
        if loop.is_closed() or loop.is_running():
            return
        try:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        except Exception:
            LOGGER.debug(
                "channel_event=ws_task_cleanup channel=feishu account_id_hash=%s result=failed error_code=FEISHU_WS_TASK_CLEANUP_FAILED",
                _short_hash(self.account_id),
            )

    def _handle_loop_exception(
        self,
        loop: asyncio.AbstractEventLoop,
        context: dict[str, Any],
    ) -> None:
        if self._stopping.is_set():
            return
        exception = context.get("exception")
        if exception is not None and exception.__class__.__name__ == "ConnectionClosedOK":
            return
        LOGGER.error(
            "channel_event=ws_loop_error channel=feishu account_id_hash=%s result=failed error_code=FEISHU_WS_LOOP_ERROR",
            _short_hash(self.account_id),
        )

    async def stop(self) -> None:
        self._stopping.set()
        deadline = time.monotonic() + self._stop_timeout_seconds
        client = self._ws_client
        ws_loop = self._ws_loop

        if client is not None:
            try:
                remaining = max(0.01, deadline - time.monotonic())
                result = await asyncio.wait_for(
                    asyncio.to_thread(self._sdk.stop_ws_client, client),
                    timeout=remaining,
                )
                if inspect.isawaitable(result):
                    remaining = max(0.01, deadline - time.monotonic())
                    if ws_loop is not None and not ws_loop.is_closed():
                        future = asyncio.run_coroutine_threadsafe(result, ws_loop)
                        await asyncio.wait_for(
                            asyncio.wrap_future(future),
                            timeout=remaining,
                        )
                    else:
                        await asyncio.wait_for(result, timeout=remaining)
            except TimeoutError:
                LOGGER.warning(
                    "channel_event=ws_stop channel=feishu account_id_hash=%s result=failed error_code=FEISHU_WS_STOP_TIMEOUT",
                    _short_hash(self.account_id),
                )
            except Exception:
                LOGGER.error(
                    "channel_event=ws_stop channel=feishu account_id_hash=%s result=failed error_code=FEISHU_WS_STOP_FAILED",
                    _short_hash(self.account_id),
                )

        # lark-oapi 1.7.x can finish _disconnect while client.start() remains
        # blocked in its event-loop selector. Stopping the isolated loop is the
        # final, deterministic thread-exit signal.
        if ws_loop is not None and not ws_loop.is_closed():
            try:
                ws_loop.call_soon_threadsafe(ws_loop.stop)
            except RuntimeError:
                LOGGER.debug(
                    "channel_event=ws_loop_stop channel=feishu account_id_hash=%s result=skipped error_code=FEISHU_WS_LOOP_ALREADY_CLOSED",
                    _short_hash(self.account_id),
                )

        thread = self._ws_thread
        if thread is not None and thread.is_alive():
            remaining = max(0.0, deadline - time.monotonic())
            await asyncio.to_thread(thread.join, remaining)
        if thread is not None and thread.is_alive():
            LOGGER.warning(
                "channel_event=ws_thread_stop channel=feishu account_id_hash=%s timeout_seconds=%.1f result=failed error_code=FEISHU_WS_THREAD_STOP_TIMEOUT",
                _short_hash(self.account_id),
                self._stop_timeout_seconds,
            )
        else:
            self._ws_thread = None

        self._loop = None

    async def send(self, message: OutgoingMessage) -> None:
        content = json.dumps({"text": message.content}, ensure_ascii=False)
        if message.reply_to_message_id:
            response = await asyncio.to_thread(
                self._sdk.reply_message,
                self._rest,
                message.reply_to_message_id,
                content,
            )
        else:
            if not message.chat_id:
                raise ValueError("chat_id is required when creating a Feishu message")
            response = await asyncio.to_thread(
                self._sdk.create_message,
                self._rest,
                message.chat_id,
                content,
            )

        if not self._sdk.response_success(response):
            code = _string(self._sdk.response_code(response)) or "unknown"
            LOGGER.error(
                "channel_event=send_failed channel=feishu account_id_hash=%s result=failed error_code=FEISHU_SEND_%s",
                _short_hash(self.account_id),
                code,
            )
            raise FeishuSendError(code)

    async def begin_reply(
        self,
        source: IncomingMessage,
        *,
        max_content_chars: int,
        context: ReplyContext | None = None,
    ) -> ReplySession:
        if context is not None and not context.capabilities.progressive_reply:
            return await super().begin_reply(
                source,
                max_content_chars=max_content_chars,
                context=context,
            )
        return await FeishuProgressiveReplySession.begin(
            transport=self,
            source=source,
            max_content_chars=max_content_chars,
            context=context,
            update_interval_seconds=self._stream_update_interval_seconds,
        )

    async def add_typing_reaction(self, message_id: str) -> str:
        response = await asyncio.to_thread(
            self._sdk.create_reaction,
            self._rest,
            message_id,
            "Typing",
        )
        self._require_success(response)
        return self._require_response_value(response, "reaction_id")

    async def remove_reaction(self, message_id: str, reaction_id: str) -> None:
        response = await asyncio.to_thread(
            self._sdk.delete_reaction,
            self._rest,
            message_id,
            reaction_id,
        )
        self._require_success(response)

    async def create_streaming_card(self, card_json: str) -> str:
        response = await asyncio.to_thread(
            self._sdk.create_card,
            self._rest,
            card_json,
        )
        self._require_success(response)
        return self._require_response_value(response, "card_id")

    async def update_card(self, message_id: str, card_json: str) -> None:
        """Replace a finished interactive message with a complete Card JSON."""

        if not message_id:
            raise ValueError("Feishu reply message ID is required")
        if not card_json:
            raise ValueError("Feishu card JSON is required")
        response = await asyncio.to_thread(
            self._sdk.patch_card_message,
            self._rest,
            message_id,
            card_json,
        )
        self._require_success(response)

    async def reply_card(
        self,
        message_id: str,
        card_id: str,
        *,
        delivery_uuid: str,
    ) -> str:
        content = json.dumps(
            {"type": "card", "data": {"card_id": card_id}},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        response = await asyncio.to_thread(
            self._sdk.reply_message,
            self._rest,
            message_id,
            content,
            message_type="interactive",
            delivery_uuid=delivery_uuid,
        )
        self._require_success(response)
        return self._require_response_value(response, "message_id")

    async def update_card_text(
        self,
        card_id: str,
        content: str,
        *,
        sequence: int,
        delivery_uuid: str,
    ) -> None:
        response = await asyncio.to_thread(
            self._sdk.update_card_text,
            self._rest,
            card_id,
            content,
            sequence=sequence,
            delivery_uuid=delivery_uuid,
        )
        self._require_success(response)

    async def batch_update_card(
        self,
        card_id: str,
        actions: list[dict[str, object]],
        *,
        sequence: int,
        delivery_uuid: str,
    ) -> None:
        response = await asyncio.to_thread(
            self._sdk.batch_update_card,
            self._rest,
            card_id,
            actions,
            sequence=sequence,
            delivery_uuid=delivery_uuid,
        )
        self._require_success(response)

    async def finish_streaming_card(
        self,
        card_id: str,
        *,
        sequence: int,
        delivery_uuid: str,
    ) -> None:
        response = await asyncio.to_thread(
            self._sdk.finish_streaming_card,
            self._rest,
            card_id,
            sequence=sequence,
            delivery_uuid=delivery_uuid,
        )
        self._require_success(response)

    async def reply_content(
        self,
        message_id: str,
        content: str,
        *,
        message_type: str,
        delivery_uuid: str,
    ) -> None:
        response = await asyncio.to_thread(
            self._sdk.reply_message,
            self._rest,
            message_id,
            content,
            message_type=message_type,
            delivery_uuid=delivery_uuid,
        )
        self._require_success(response)

    def _require_success(self, response: Any) -> None:
        if self._sdk.response_success(response):
            return
        code = _string(self._sdk.response_code(response)) or "unknown"
        raise FeishuSendError(code)

    @staticmethod
    def _require_response_value(response: Any, name: str) -> str:
        data = getattr(response, "data", None)
        value = _string(getattr(data, name, None))
        if not value:
            raise FeishuSendError(f"MISSING_{name.upper()}")
        return value

    def _on_message_receive(self, data: Any) -> None:
        """SDK callback: normalize and enqueue; never await business handling."""

        try:
            incoming = self._normalize(data)
            identity = incoming.identity
            identifier_kinds = "none"
            identifier_count = 0
            if identity is not None:
                identifier_kinds = ",".join(identifier.kind for identifier in identity.identifiers)
                identifier_count = len(identity.identifiers)
            LOGGER.info(
                "channel_event=identity_normalized channel=feishu account_id_hash=%s identity_present=%s tenant_key_present=%s identifier_kinds=%s identifier_count=%d result=ok",
                _short_hash(self.account_id),
                str(identity is not None).lower(),
                str(identity is not None and identity.provider_tenant_key is not None).lower(),
                identifier_kinds,
                identifier_count,
            )
            loop = self._loop
            if loop is None or loop.is_closed():
                LOGGER.warning(
                    "channel_event=dispatch_unavailable channel=feishu account_id_hash=%s message_id_hash=%s result=dropped error_code=DISPATCH_LOOP_UNAVAILABLE",
                    _short_hash(self.account_id),
                    _short_hash(incoming.message_id),
                )
                return
            future = asyncio.run_coroutine_threadsafe(self._dispatch(incoming), loop)
            future.add_done_callback(
                lambda completed, message_id=incoming.message_id: self._log_dispatch_result(
                    completed,
                    message_id,
                )
            )
        except Exception:
            LOGGER.error(
                "channel_event=normalize_failed channel=feishu account_id_hash=%s result=dropped error_code=FEISHU_EVENT_INVALID",
                _short_hash(self.account_id),
            )

    def _on_identity_event(self, data: Any) -> None:
        """Normalize and durably receipt a Contact event inside the ACK window."""

        try:
            event = self._normalize_identity_event(data)
            loop = self._loop
            if loop is None or loop.is_closed():
                raise RuntimeError("identity event dispatch loop is unavailable")
            future = asyncio.run_coroutine_threadsafe(
                self._dispatch_identity_event(event),
                loop,
            )
            try:
                future.result(timeout=_IDENTITY_EVENT_TIMEOUT_SECONDS)
            except TimeoutError:
                future.cancel()
                raise
        except Exception:
            LOGGER.error(
                "channel_event=identity_event_receipt channel=feishu account_id_hash=%s result=failed error_code=FEISHU_IDENTITY_EVENT_FAILED",
                _short_hash(self.account_id),
            )
            # lark-oapi logs callback exceptions. Never let a Pydantic error
            # carry the rejected identifier or raw SDK value into that log.
            raise RuntimeError("FEISHU_IDENTITY_EVENT_FAILED") from None

    async def _dispatch_identity_event(self, event: ChannelIdentityEvent) -> None:
        handler = self._identity_event_handler
        if handler is None:
            raise RuntimeError("identity event handler is unavailable")
        await handler(event)

    def _normalize_identity_event(self, data: Any) -> ChannelIdentityEvent:
        header = getattr(data, "header", None)
        event_type = _required_identity_text(
            getattr(header, "event_type", None),
            field_name="event_type",
        )
        event_id = _required_identity_text(
            getattr(header, "event_id", None),
            field_name="event_id",
        )
        observed_tenant_key = _required_identity_text(
            getattr(header, "tenant_key", None),
            field_name="tenant_key",
        )
        observed_app_id = _required_identity_text(
            getattr(header, "app_id", None),
            field_name="app_id",
        )
        if observed_app_id != self.account.app_id:
            raise ValueError("Feishu directory event app does not match account")
        event_at = _directory_event_time(getattr(header, "create_time", None))
        if event_type == "contact.scope.updated_v3":
            return ChannelIdentityEvent.model_validate(
                {
                    "version": 1,
                    "event_type": event_type,
                    "event_id": event_id,
                    "event_at": event_at,
                    "observed_app_id": observed_app_id,
                    "observed_tenant_key": observed_tenant_key,
                    "subject": None,
                }
            )

        event = getattr(data, "event", None)
        subject = getattr(event, "object", None)
        if subject is None:
            raise ValueError("Feishu directory event subject is missing")
        values = {
            "open_id": _optional_identity_text(getattr(subject, "open_id", None)),
            "user_id": _optional_identity_text(getattr(subject, "user_id", None)),
            "union_id": _optional_identity_text(getattr(subject, "union_id", None)),
        }
        if event_type == "contact.user.deleted_v3":
            old_subject = getattr(event, "old_object", None)
            old_open_id = _optional_identity_text(getattr(old_subject, "open_id", None))
            if values["open_id"] is not None and old_open_id is not None and values["open_id"] != old_open_id:
                raise ValueError("Feishu deleted user identity is inconsistent")
            if values["open_id"] is None:
                values["open_id"] = old_open_id
        identifiers = tuple(ChannelIdentityIdentifier(kind=kind, value=value) for kind, value in values.items() if value is not None)
        status_value = "inactive" if event_type == "contact.user.deleted_v3" else _directory_status(getattr(subject, "status", None))
        return ChannelIdentityEvent.model_validate(
            {
                "version": 1,
                "event_type": event_type,
                "event_id": event_id,
                "event_at": event_at,
                "observed_app_id": observed_app_id,
                "observed_tenant_key": observed_tenant_key,
                "subject": ChannelIdentitySubject(
                    identifiers=identifiers,
                    directory_status=status_value,
                ),
            }
        )

    def _on_card_action(self, data: Any) -> Any:
        """Normalize, claim and enqueue a card action within Feishu's deadline."""

        try:
            action = self._normalize_card_action(data)
            loop = self._loop
            if loop is None or loop.is_closed():
                response = ChannelActionResponse(
                    toast_type="error",
                    content="服务正在重启，请稍后再试。",
                )
            else:
                dispatch = self._dispatch_form_action(action) if isinstance(action, ChannelFormAction) else self._dispatch_action(action)
                future = asyncio.run_coroutine_threadsafe(
                    dispatch,
                    loop,
                )
                # Feishu requires callback completion in three seconds. The
                # handler durably receipts the bounded callback through the
                # private API; MCP execution and card writes happen later.
                response = future.result(timeout=2.5)
        except TimeoutError:
            response = ChannelActionResponse(
                toast_type="error",
                content="操作确认超时，请重试。",
            )
        except Exception:
            response = ChannelActionResponse(
                toast_type="error",
                content="无效操作，请刷新后重试。",
            )
        return self._sdk.card_action_response(response)

    def _normalize_card_action(
        self,
        data: Any,
    ) -> ChannelAction | ChannelFormAction:
        event = getattr(data, "event", None)
        action = getattr(event, "action", None)
        value = getattr(action, "value", None)
        if not isinstance(value, dict):
            raise ValueError("Feishu card action has no value")
        if getattr(action, "form_value", None) is not None or {
            "nonce",
            "revision",
        }.issubset(value):
            return self._normalize_form_action(data, value)
        action_id = value.get("action_id")
        if not isinstance(action_id, str) or not action_id:
            raise ValueError("Feishu card action has no opaque action ID")

        operator = getattr(event, "operator", None)
        context = getattr(event, "context", None)
        header = getattr(data, "header", None)
        operator_id = _first_non_empty(
            getattr(operator, "open_id", None),
            getattr(operator, "union_id", None),
            getattr(operator, "user_id", None),
        )
        chat_id = _string(getattr(context, "open_chat_id", None))
        message_id = _string(getattr(context, "open_message_id", None))
        event_id = _string(getattr(header, "event_id", None))
        if not operator_id or not chat_id or not message_id or not event_id:
            raise ValueError("Feishu card action identity is incomplete")
        return ChannelAction(
            action_id=action_id,
            operator_id=operator_id,
            chat_id=chat_id,
            message_id=message_id,
            event_id=event_id,
        )

    def _normalize_form_action(
        self,
        data: Any,
        value: dict[str, Any],
    ) -> ChannelFormAction:
        event = getattr(data, "event", None)
        action = getattr(event, "action", None)
        if getattr(action, "tag", None) != "button":
            raise ValueError("Feishu form action is not a submit button")
        if set(value) not in (
            {"action_id", "nonce", "revision"},
            {"action_id", "nonce", "revision", "action"},
        ):
            raise ValueError("Feishu form action metadata is invalid")
        interaction_action = value.get("action", "accept")
        if interaction_action not in {"accept", "decline", "cancel"}:
            raise ValueError("Feishu form action response is invalid")

        operator = getattr(event, "operator", None)
        context = getattr(event, "context", None)
        header = getattr(data, "header", None)
        provider_tenant_key = _required_identity_text(
            getattr(header, "tenant_key", None),
            field_name="tenant_key",
        )
        operator_tenant_key = _required_identity_text(
            getattr(operator, "tenant_key", None),
            field_name="operator.tenant_key",
        )
        if operator_tenant_key != provider_tenant_key:
            raise ValueError("Feishu form operator tenant does not match event tenant")
        event_app_id = _required_identity_text(
            getattr(header, "app_id", None),
            field_name="app_id",
        )
        if event_app_id != self.account.app_id:
            raise ValueError("Feishu form event app does not match configured account")

        identifiers = [
            IncomingIdentityIdentifier(
                kind="open_id",
                value=_required_identity_text(
                    getattr(operator, "open_id", None),
                    field_name="operator.open_id",
                ),
            )
        ]
        for kind in ("user_id", "union_id"):
            identifier = _optional_identity_text(getattr(operator, kind, None))
            if identifier is not None:
                identifiers.append(
                    IncomingIdentityIdentifier(kind=kind, value=identifier),
                )
        identity = IncomingIdentityAssertion(
            provider=self.channel_id,
            provider_tenant_key=provider_tenant_key,
            identifiers=tuple(identifiers),
        )
        return ChannelFormAction(
            action_id=value.get("action_id"),
            nonce=value.get("nonce"),
            revision=value.get("revision"),
            action=interaction_action,
            form_value=getattr(action, "form_value", None) or {},
            identity=identity,
            chat_id=_required_identity_text(
                getattr(context, "open_chat_id", None),
                field_name="open_chat_id",
            ),
            message_id=_required_identity_text(
                getattr(context, "open_message_id", None),
                field_name="open_message_id",
            ),
            event_id=_required_identity_text(
                getattr(header, "event_id", None),
                field_name="event_id",
            ),
        )

    def _log_dispatch_result(self, future: Any, message_id: str) -> None:
        try:
            future.result()
        except Exception:
            LOGGER.error(
                "channel_event=dispatch_failed channel=feishu account_id_hash=%s message_id_hash=%s result=failed error_code=DISPATCH_FAILURE",
                _short_hash(self.account_id),
                _short_hash(message_id),
            )

    def _normalize(self, data: Any) -> IncomingMessage:
        event = getattr(data, "event", None)
        message = getattr(event, "message", None)
        if message is None:
            raise ValueError("Feishu message event has no message payload")

        sender = getattr(event, "sender", None)
        sender_ids = getattr(sender, "sender_id", None)
        open_id = _required_identity_text(
            getattr(sender_ids, "open_id", None),
            field_name="open_id",
        )
        user_id = _optional_identity_text(getattr(sender_ids, "user_id", None))
        union_id = _optional_identity_text(getattr(sender_ids, "union_id", None))

        header = getattr(data, "header", None)
        provider_tenant_key = _required_identity_text(
            getattr(header, "tenant_key", None),
            field_name="tenant_key",
        )
        sender_tenant_key = _optional_identity_text(getattr(sender, "tenant_key", None))
        if sender_tenant_key is not None and sender_tenant_key != provider_tenant_key:
            raise ValueError("Feishu sender tenant does not match event tenant")
        event_app_id = _optional_identity_text(getattr(header, "app_id", None))
        if event_app_id is not None and event_app_id != self.account.app_id:
            raise ValueError("Feishu event app does not match configured account")

        identifiers = [IncomingIdentityIdentifier(kind="open_id", value=open_id)]
        if user_id is not None:
            identifiers.append(IncomingIdentityIdentifier(kind="user_id", value=user_id))
        if union_id is not None:
            identifiers.append(IncomingIdentityIdentifier(kind="union_id", value=union_id))
        identity = IncomingIdentityAssertion(
            provider=self.channel_id,
            provider_tenant_key=provider_tenant_key,
            identifiers=tuple(identifiers),
        )

        message_type = _string(getattr(message, "message_type", None))
        raw_content = getattr(message, "content", "") or ""
        content = _string(raw_content)
        if isinstance(raw_content, str):
            try:
                payload = json.loads(raw_content)
            except (json.JSONDecodeError, TypeError):
                payload = None
            if isinstance(payload, dict) and isinstance(payload.get("text"), str):
                content = payload["text"]

        message_id = _string(getattr(message, "message_id", None))
        event_id = _first_non_empty(
            getattr(header, "event_id", None),
            getattr(event, "event_id", None),
            message_id,
        )
        create_time = _first_non_empty(
            getattr(header, "create_time", None),
            getattr(message, "create_time", None),
        )

        return IncomingMessage(
            channel=self.channel_id,
            account_id=self.account_id,
            chat_id=_string(getattr(message, "chat_id", None)),
            message_id=message_id,
            # Keep the legacy subject stable while the structured identity is
            # propagated independently through the execution boundary.
            sender_id=open_id,
            content=content,
            identity=identity,
            message_type=message_type,
            chat_type=_string(getattr(message, "chat_type", None)),
            sender_type=_string(getattr(sender, "sender_type", None)),
            event_id=event_id,
            create_time=create_time,
            # Do not retain or enqueue the complete SDK event. It can contain
            # identity and message metadata beyond the normalized contract.
            raw=None,
        )


# The builder that used to be registered here is gone with the dead registry
# (CHN-P1). It read credentials straight out of a plaintext config dict -- the
# upstream shape this project replaced with an encrypted secret store -- and
# nothing ever called it. Channels are built by
# ``api.channels.feishu.provider.WORKER_PROVIDER.build_managed`` from
# server-owned binding state instead.
