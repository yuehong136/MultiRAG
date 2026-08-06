"""DingTalk Stream transport for one bot identity.

DingTalk's own stream client is not a dependency here and does not need to be:
the protocol is a POST to ``connections/open`` for an endpoint and a ticket,
then a websocket, then an ack per callback. `aiohttp` is already a dependency,
which is also the route the upstream project took.

Replies go to the ``sessionWebhook`` carried by the incoming message rather
than to a chat id, so this transport can only answer conversations it has
heard from -- the same constraint DingTalk itself imposes on stream bots.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import aiohttp

from api.channels.core.base import Channel, IncomingMessage, OutgoingMessage

LOGGER = logging.getLogger(__name__)

_API_BASE = "https://api.dingtalk.com"
_OPEN_CONNECTION_PATH = "/v1.0/gateway/connections/open"
# The only topic this bot subscribes to. Widening it means widening what
# `_handle_payload` is prepared to parse, so it is not a configuration knob.
_BOT_MESSAGE_TOPIC = "/v1.0/im/bot/messages/get"
_WS_HEARTBEAT_SECONDS = 30
_RECONNECT_BACKOFF_SECONDS = 3
_RECONNECT_BACKOFF_CEILING_SECONDS = 30
_REPLY_TIMEOUT_SECONDS = 10


def _short_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _first_str(source: dict[str, Any], *keys: str) -> str:
    """First non-empty string among several spellings of one field.

    DingTalk's callback payload is not consistent about where a field lives --
    a conversation id arrives as ``conversationId`` or ``openConversationId``
    depending on the chat type, and the envelope repeats some of the body.
    """

    for key in keys:
        value = source.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


@dataclass(frozen=True, slots=True)
class DingTalkAccount:
    account_id: str
    client_id: str
    client_secret: str = field(repr=False)


class DingTalkChannel(Channel):
    """DingTalk stream transport for one application account."""

    channel_id = "dingtalk"

    def __init__(self, account: DingTalkAccount) -> None:
        super().__init__()
        self.account = account
        self.account_id = account.account_id
        self._stream_task: asyncio.Task[None] | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._session: aiohttp.ClientSession | None = None
        self._stopping = False
        # chat id -> the webhook that message arrived with. DingTalk scopes
        # these per conversation and expires them, so this is a cache of where
        # a reply may be sent, not a directory of conversations.
        self._session_webhooks: dict[str, str] = {}

    @property
    def is_running(self) -> bool:
        return bool(self._stream_task and not self._stream_task.done() and not self._stopping)

    async def start(self) -> None:
        if self.is_running:
            return
        self._stopping = False
        LOGGER.info(
            "channel_event=stream_starting channel=%s account_id_hash=%s result=pending",
            self.channel_id,
            _short_hash(self.account_id),
        )
        self._stream_task = asyncio.create_task(self._run_stream(), name=f"dingtalk-stream-{self.account_id}")

    async def stop(self) -> None:
        self._stopping = True
        websocket, self._ws = self._ws, None
        if websocket is not None and not websocket.closed:
            with _suppressed("websocket_close_failed", self.account_id):
                await websocket.close()
        task, self._stream_task = self._stream_task, None
        if task is not None and not task.done():
            task.cancel()
            with _suppressed("stream_task_cancel_failed", self.account_id):
                await task
        session, self._session = self._session, None
        if session is not None and not session.closed:
            with _suppressed("session_close_failed", self.account_id):
                await session.close()
        self._session_webhooks.clear()

    async def send(self, message: OutgoingMessage) -> None:
        webhook = self._session_webhooks.get(message.chat_id)
        if not webhook:
            # Not an error worth failing the binding over: a stream bot can only
            # answer where it was spoken to, and a restart empties this cache.
            LOGGER.warning(
                "channel_event=reply_dropped channel=%s account_id_hash=%s chat_id_hash=%s result=dropped error_code=SESSION_WEBHOOK_UNKNOWN",
                self.channel_id,
                _short_hash(self.account_id),
                _short_hash(message.chat_id),
            )
            return

        session = await self._ensure_session()
        payload = {"msgtype": "markdown", "markdown": {"title": "MultiRAG", "text": message.content}}
        try:
            async with session.post(webhook, json=payload, timeout=aiohttp.ClientTimeout(total=_REPLY_TIMEOUT_SECONDS)) as response:
                if response.status >= 400:
                    # The body can quote the request, so it is counted, not logged.
                    LOGGER.error(
                        "channel_event=reply_failed channel=%s account_id_hash=%s chat_id_hash=%s status=%s result=failed error_code=REPLY_REJECTED",
                        self.channel_id,
                        _short_hash(self.account_id),
                        _short_hash(message.chat_id),
                        response.status,
                    )
        except Exception:
            LOGGER.error(
                "channel_event=reply_failed channel=%s account_id_hash=%s chat_id_hash=%s result=failed error_code=REPLY_TRANSPORT_FAILURE",
                self.channel_id,
                _short_hash(self.account_id),
                _short_hash(message.chat_id),
            )

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def _run_stream(self) -> None:
        backoff = _RECONNECT_BACKOFF_SECONDS
        while not self._stopping:
            try:
                # A ticket authorises one connection, and a reconnect gets a new
                # one; the webhook cache is dropped with it because the old
                # entries belong to a session DingTalk has already forgotten.
                self._session_webhooks.clear()
                endpoint, ticket = await self._open_connection()
                session = await self._ensure_session()
                websocket = await self._connect(session, endpoint, ticket)
                self._ws = websocket
                LOGGER.info(
                    "channel_event=stream_connected channel=%s account_id_hash=%s result=ok",
                    self.channel_id,
                    _short_hash(self.account_id),
                )
                await self._consume(websocket)
                backoff = _RECONNECT_BACKOFF_SECONDS
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.error(
                    "channel_event=stream_failed channel=%s account_id_hash=%s result=failed error_code=STREAM_CONNECT_FAILED",
                    self.channel_id,
                    _short_hash(self.account_id),
                )
            finally:
                websocket, self._ws = self._ws, None
                if websocket is not None and not websocket.closed:
                    with _suppressed("websocket_close_failed", self.account_id):
                        await websocket.close()

            if not self._stopping:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _RECONNECT_BACKOFF_CEILING_SECONDS)

    async def _consume(self, websocket: aiohttp.ClientWebSocketResponse) -> None:
        async for frame in websocket:
            if self._stopping:
                return
            if frame.type is aiohttp.WSMsgType.TEXT:
                raw = frame.data
            elif frame.type is aiohttp.WSMsgType.BINARY:
                raw = frame.data.decode("utf-8", "ignore")
            else:
                return
            try:
                await self._handle_payload(raw)
            except Exception:
                # One malformed callback must not drop the connection; the
                # runtime monitor only reacts to the transport actually stopping.
                LOGGER.warning(
                    "channel_event=payload_rejected channel=%s account_id_hash=%s result=dropped error_code=PAYLOAD_MALFORMED",
                    self.channel_id,
                    _short_hash(self.account_id),
                )

    async def _open_connection(self) -> tuple[str, str]:
        session = await self._ensure_session()
        payload = {
            "clientId": self.account.client_id,
            "clientSecret": self.account.client_secret,
            "subscriptions": [{"type": "CALLBACK", "topic": _BOT_MESSAGE_TOPIC}],
        }
        async with session.post(
            f"{_API_BASE}{_OPEN_CONNECTION_PATH}",
            json=payload,
            headers={"Accept": "application/json"},
            timeout=aiohttp.ClientTimeout(total=_REPLY_TIMEOUT_SECONDS),
        ) as response:
            body = await response.text()
            if response.status != 200:
                # The request body held the client secret, and DingTalk echoes
                # request context on failure, so the body never reaches a log.
                raise RuntimeError(f"connections/open returned {response.status}")
        try:
            data = json.loads(body)
        except json.JSONDecodeError as error:
            raise RuntimeError("connections/open returned a non-JSON body") from error
        if not isinstance(data, dict):
            raise RuntimeError("connections/open returned a non-object body")

        endpoint = _first_str(data, "endpoint")
        ticket = _first_str(data, "ticket")
        if not endpoint or not ticket:
            raise RuntimeError("connections/open response is missing endpoint or ticket")
        return endpoint, ticket

    async def _connect(
        self,
        session: aiohttp.ClientSession,
        endpoint: str,
        ticket: str,
    ) -> aiohttp.ClientWebSocketResponse:
        """Open the websocket, ticket in the query string.

        A header fallback follows because the endpoint DingTalk hands back has
        been observed already carrying its own query, and this is the one part
        of the protocol we cannot exercise without a live tenant.
        """

        attempts: tuple[tuple[str, str, dict[str, str]], ...] = (
            ("query", _with_ticket(endpoint, ticket), {}),
            ("header", endpoint, {"ticket": ticket}),
        )
        last_error: Exception | None = None
        for mode, url, headers in attempts:
            try:
                return await session.ws_connect(url, heartbeat=_WS_HEARTBEAT_SECONDS, headers=headers or None)
            except Exception as error:
                last_error = error
                LOGGER.warning(
                    "channel_event=stream_connect_retry channel=%s account_id_hash=%s mode=%s result=failed",
                    self.channel_id,
                    _short_hash(self.account_id),
                    mode,
                )
        raise last_error if last_error is not None else RuntimeError("websocket connect failed")

    async def _handle_payload(self, raw: str) -> None:
        if not raw:
            return
        envelope = json.loads(raw)
        if not isinstance(envelope, dict):
            return

        headers = envelope.get("headers")
        headers = headers if isinstance(headers, dict) else {}
        body: Any = envelope.get("data", envelope)
        if isinstance(body, str):
            body = json.loads(body)
        if not isinstance(body, dict):
            return

        chat_id = _first_str(body, "conversationId", "openConversationId", "chatId")
        sender_id = _first_str(body, "senderStaffId", "senderId", "userId")
        content = _extract_text(body)
        webhook = _first_str(body, "sessionWebhook") or _first_str(envelope, "sessionWebhook")
        if webhook and chat_id:
            self._session_webhooks[chat_id] = webhook

        # Ack before dispatching, and regardless of whether we will answer:
        # an unacked callback is redelivered, which for a message we chose to
        # ignore means an endless redelivery loop rather than a lost reply.
        await self._ack(_first_str(headers, "messageId") or _first_str(body, "messageId"))

        if not chat_id or not sender_id or not content.strip():
            return

        await self._dispatch(
            IncomingMessage(
                channel=self.channel_id,
                account_id=self.account_id,
                chat_id=chat_id,
                # "1" is a one-to-one chat, "2" a group. Normalised to the same
                # vocabulary the bridge's policy check already speaks.
                chat_type="p2p" if _first_str(body, "conversationType") == "1" else "group",
                message_id=_first_str(body, "msgId"),
                sender_id=sender_id,
                content=content,
                raw=body,
                # Stream callbacks on this topic are user messages; DingTalk
                # does not echo the bot's own replies back to it.
                sender_type="user",
            )
        )

    async def _ack(self, message_id: str) -> None:
        websocket = self._ws
        if not message_id or websocket is None or websocket.closed:
            return
        with _suppressed("callback_ack_failed", self.account_id):
            await websocket.send_json({"messageId": message_id, "response": {"success": True}})


def _extract_text(body: dict[str, Any]) -> str:
    """Pull the user's text out of the shapes this topic delivers."""

    text = body.get("text")
    if isinstance(text, dict):
        content = text.get("content")
        if isinstance(content, str):
            return content
    if isinstance(text, str):
        return text
    content = body.get("content")
    if isinstance(content, dict):
        for key in ("text", "content", "recognition"):
            value = content.get(key)
            if isinstance(value, str) and value.strip():
                return value
    if isinstance(content, str):
        return content
    return ""


def _with_ticket(endpoint: str, ticket: str) -> str:
    parts = urlsplit(endpoint)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.setdefault("ticket", ticket)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


class _suppressed:
    """Log and swallow a shutdown-path failure, never masking cancellation."""

    def __init__(self, event: str, account_id: str) -> None:
        self._event = event
        self._account_id = account_id

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: type[BaseException] | None, *_rest: object) -> bool:
        if exc_type is None or issubclass(exc_type, asyncio.CancelledError):
            return False
        LOGGER.warning(
            "channel_event=%s channel=dingtalk account_id_hash=%s result=dropped",
            self._event,
            _short_hash(self._account_id),
        )
        return True
