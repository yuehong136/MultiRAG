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
from importlib import import_module
from typing import Any, Protocol, runtime_checkable

from ..core.base import Channel, IncomingMessage, OutgoingMessage
from ..core.registry import ChannelConfig, register_channel

LOGGER = logging.getLogger(__name__)


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
        callback: Any,
    ) -> Any: ...

    def reply_message(self, client: Any, message_id: str, content: str) -> Any: ...

    def create_message(self, client: Any, chat_id: str, content: str) -> Any: ...

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
        except ImportError as error:
            raise FeishuDependencyError("Feishu channel requires the optional 'lark-oapi' dependency") from error

        self._create_message_request = im_api.CreateMessageRequest
        self._create_message_body = im_api.CreateMessageRequestBody
        self._reply_message_request = im_api.ReplyMessageRequest
        self._reply_message_body = im_api.ReplyMessageRequestBody

    def _domain(self, domain: str) -> Any:
        return self._lark.LARK_DOMAIN if domain == "lark" else self._lark.FEISHU_DOMAIN

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

    def build_ws_client(self, account: FeishuAccount, callback: Any) -> Any:
        handler = self._lark.EventDispatcherHandler.builder("", "").register_p2_im_message_receive_v1(callback).build()
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

    def reply_message(self, client: Any, message_id: str, content: str) -> Any:
        request = self._reply_message_request.builder().message_id(message_id).request_body(self._reply_message_body.builder().content(content).msg_type("text").build()).build()
        return client.im.v1.message.reply(request)

    def create_message(self, client: Any, chat_id: str, content: str) -> Any:
        request = (
            self._create_message_request.builder().receive_id_type("chat_id").request_body(self._create_message_body.builder().receive_id(chat_id).content(content).msg_type("text").build()).build()
        )
        return client.im.v1.message.create(request)

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
    ) -> None:
        super().__init__()
        if start_timeout_seconds <= 0:
            raise ValueError("start_timeout_seconds must be positive")
        if stop_timeout_seconds <= 0:
            raise ValueError("stop_timeout_seconds must be positive")

        self.account = account
        self.account_id = account.account_id
        self._sdk = sdk or _LarkOapiSDK()
        self._start_timeout_seconds = start_timeout_seconds
        self._stop_timeout_seconds = stop_timeout_seconds
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ws_loop: asyncio.AbstractEventLoop | None = None
        self._ws_client: Any = None
        self._ws_thread: threading.Thread | None = None
        self._thread_ready = threading.Event()
        self._thread_finished = threading.Event()
        self._stopping = threading.Event()
        self._rest = self._sdk.build_rest_client(account)

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

    def _on_message_receive(self, data: Any) -> None:
        """SDK callback: normalize and enqueue; never await business handling."""

        try:
            incoming = self._normalize(data)
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
        sender_id = _first_non_empty(
            getattr(sender_ids, "open_id", None),
            getattr(sender_ids, "union_id", None),
            getattr(sender_ids, "user_id", None),
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

        header = getattr(data, "header", None)
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
            sender_id=sender_id,
            content=content,
            message_type=message_type,
            chat_type=_string(getattr(message, "chat_type", None)),
            sender_type=_string(getattr(sender, "sender_type", None)),
            event_id=event_id,
            create_time=create_time,
            # Do not retain or enqueue the complete SDK event. It can contain
            # identity and message metadata beyond the normalized contract.
            raw=None,
        )


def _build(account_id: str, config: ChannelConfig) -> Channel:
    app_id = config.get("app_id")
    app_secret = config.get("app_secret")
    if not app_id or not app_secret:
        raise ValueError(f"feishu account '{account_id}' is missing app_id or app_secret")
    return FeishuChannel(
        FeishuAccount(
            account_id=account_id,
            app_id=str(app_id),
            app_secret=str(app_secret),
            domain=str(config.get("domain", "feishu")),
        )
    )


register_channel("feishu", _build)
