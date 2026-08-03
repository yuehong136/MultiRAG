from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Protocol, runtime_checkable
from urllib.parse import quote

import httpx

from api.channels.core.base import Channel, IncomingMessage, OutgoingMessage
from api.channels.state_store import ChannelStateStore, conversation_key

LOGGER = logging.getLogger(__name__)

SERVICE_UNAVAILABLE_TEXT = "服务暂时不可用，请稍后再试。"
TEXT_ONLY_TEXT = "当前演示仅支持文字消息。"
QUESTION_TOO_LONG_TEXT = "问题过长，请缩短后重试。"
DEMO_ONLY_TEXT = "当前机器人仅用于定向演示。"
SESSION_RESET_TEXT = "会话已重置，下一个问题将开始新会话。"
ANSWER_TRUNCATED_SUFFIX = "\n\n（回答过长，演示版已截断）"

_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", flags=re.IGNORECASE | re.DOTALL)
_UNCLOSED_THINK_RE = re.compile(r"<think>.*$", flags=re.IGNORECASE | re.DOTALL)


class AgentExecutionError(RuntimeError):
    """A safe, classified failure from the published-Agent boundary."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class AgentReply:
    content: str
    session_id: str


@runtime_checkable
class AgentExecutor(Protocol):
    async def ask(self, *, question: str, session_id: str | None) -> AgentReply: ...


def _short_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _strip_reasoning(text: str) -> str:
    without_blocks = _THINK_BLOCK_RE.sub("", text)
    without_unclosed = _UNCLOSED_THINK_RE.sub("", without_blocks)
    return without_unclosed.replace("</think>", "").strip()


def _truncate_answer(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    if max_chars <= len(ANSWER_TRUNCATED_SUFFIX):
        return ANSWER_TRUNCATED_SUFFIX[:max_chars]
    prefix_length = max_chars - len(ANSWER_TRUNCATED_SUFFIX)
    return text[:prefix_length].rstrip() + ANSWER_TRUNCATED_SUFFIX


class MultiRAGAgentClient:
    """Calls one fixed, published MultiRAG Agent through its native SSE API."""

    def __init__(
        self,
        *,
        base_url: str,
        agent_id: str,
        api_token: str,
        connect_timeout_seconds: float = 5,
        total_timeout_seconds: float = 120,
        max_answer_chars: int = 4000,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._agent_id = agent_id
        self._max_answer_chars = max_answer_chars
        self._total_timeout_seconds = total_timeout_seconds
        self._owns_client = client is None
        self._base_url = base_url.rstrip("/")
        endpoint_agent_id = quote(agent_id, safe="")
        self._endpoint = f"{self._base_url}/api/v1/agents/{endpoint_agent_id}/completions"
        self._sessions_endpoint = f"{self._base_url}/api/v1/agents/{endpoint_agent_id}/sessions"
        self._headers = {
            "Accept": "text/event-stream",
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
        }
        timeout = httpx.Timeout(
            connect=connect_timeout_seconds,
            read=total_timeout_seconds,
            write=connect_timeout_seconds,
            pool=connect_timeout_seconds,
        )
        self._client = client or httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            # The Agent API is a fixed internal origin. Do not inherit desktop
            # HTTP(S)_PROXY settings, which can route loopback requests and
            # their Bearer token through an unintended proxy.
            trust_env=False,
        )

    @property
    def agent_id(self) -> str:
        return self._agent_id

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def preflight(self) -> None:
        """Validate API reachability plus token ownership without executing the Agent."""

        try:
            # The ping endpoint is public and must not receive credentials.
            ping_response = await self._client.get(f"{self._base_url}/api/v1/system/ping")
            if ping_response.status_code != httpx.codes.OK or ping_response.text.strip().strip('"') != "pong":
                raise AgentExecutionError("AGENT_PREFLIGHT_PING")

            agent_response = await self._client.get(
                self._sessions_endpoint,
                params={"page": 1, "page_size": 1, "dsl": "false"},
                headers=self._headers,
            )
            if agent_response.status_code != httpx.codes.OK:
                raise AgentExecutionError(f"AGENT_PREFLIGHT_HTTP_{agent_response.status_code}")
            try:
                payload = agent_response.json()
            except ValueError as exc:
                raise AgentExecutionError("AGENT_PREFLIGHT_INVALID_JSON") from exc
            if not isinstance(payload, dict) or payload.get("code", payload.get("retcode")) != 0:
                raise AgentExecutionError("AGENT_PREFLIGHT_AUTH_OR_OWNERSHIP")
        except AgentExecutionError:
            raise
        except httpx.TimeoutException as exc:
            raise AgentExecutionError("AGENT_PREFLIGHT_TIMEOUT") from exc
        except httpx.HTTPError as exc:
            raise AgentExecutionError("AGENT_PREFLIGHT_TRANSPORT") from exc

    async def ask(self, *, question: str, session_id: str | None) -> AgentReply:
        request_body: dict[str, object] = {
            "question": question,
            "stream": True,
            "release": True,
        }
        if session_id:
            request_body["session_id"] = session_id

        try:
            async with asyncio.timeout(self._total_timeout_seconds):
                async with self._client.stream(
                    "POST",
                    self._endpoint,
                    json=request_body,
                    headers=self._headers,
                ) as response:
                    if response.status_code != httpx.codes.OK:
                        raise AgentExecutionError(f"AGENT_HTTP_{response.status_code}")
                    return await self._consume_sse(response)
        except AgentExecutionError:
            raise
        except TimeoutError as exc:
            raise AgentExecutionError("AGENT_TIMEOUT") from exc
        except httpx.TimeoutException as exc:
            raise AgentExecutionError("AGENT_TIMEOUT") from exc
        except httpx.HTTPError as exc:
            raise AgentExecutionError("AGENT_TRANSPORT") from exc

    async def _consume_sse(self, response: httpx.Response) -> AgentReply:
        content_parts: list[str] = []
        response_session_id = ""
        in_reasoning = False
        saw_message_end = False
        saw_done = False

        async for line in response.aiter_lines():
            if not line.startswith("data:"):
                continue
            payload_text = line[5:].strip()
            if not payload_text:
                continue
            if payload_text == "[DONE]":
                saw_done = True
                break
            try:
                payload = json.loads(payload_text)
            except json.JSONDecodeError as exc:
                raise AgentExecutionError("AGENT_SSE_INVALID_JSON") from exc
            if not isinstance(payload, dict):
                raise AgentExecutionError("AGENT_SSE_INVALID_PAYLOAD")

            raw_session_id = payload.get("session_id")
            if isinstance(raw_session_id, str) and raw_session_id:
                response_session_id = raw_session_id

            event = payload.get("event")
            data = payload.get("data")
            if event == "error":
                raise AgentExecutionError("AGENT_SSE_ERROR")
            if event == "message_end":
                saw_message_end = True
                continue
            if event != "message" or not isinstance(data, dict):
                continue

            if data.get("start_to_think") is True:
                in_reasoning = True
                continue
            if data.get("end_to_think") is True:
                in_reasoning = False
                continue
            chunk = data.get("content")
            if not in_reasoning and isinstance(chunk, str):
                content_parts.append(chunk)

        if not saw_message_end or not saw_done:
            raise AgentExecutionError("AGENT_SSE_INCOMPLETE")
        if not response_session_id:
            raise AgentExecutionError("AGENT_SESSION_MISSING")

        content = _strip_reasoning("".join(content_parts))
        if not content or content.startswith("**ERROR**:"):
            raise AgentExecutionError("AGENT_EMPTY_RESULT")
        return AgentReply(
            content=_truncate_answer(content, self._max_answer_chars),
            session_id=response_session_id,
        )


class FeishuAgentBridge:
    """Business handler for the demo Feishu transport.

    External identity is used only for server-side isolation and allowlisting. It
    is deliberately never sent to the Agent request or interpolated into a
    prompt.
    """

    def __init__(
        self,
        *,
        channel: Channel,
        executor: AgentExecutor,
        state_store: ChannelStateStore,
        app_id: str,
        agent_id: str,
        release_marker: str,
        allowed_open_ids: set[str] | frozenset[str],
        max_question_chars: int,
    ) -> None:
        self._channel = channel
        self._executor = executor
        self._state_store = state_store
        self._app_id = app_id
        self._agent_id = agent_id
        self._release_marker = release_marker
        self._allowed_open_ids = frozenset(allowed_open_ids)
        self._max_question_chars = max_question_chars
        self._session_locks: dict[str, asyncio.Lock] = {}

    async def handle_message(self, message: IncomingMessage) -> None:
        if message.chat_type != "p2p":
            return
        if getattr(message, "sender_type", "") != "user":
            return
        if not message.message_id or not message.chat_id or not message.sender_id:
            self._log(logging.WARNING, "event_rejected", message, error_code="MESSAGE_IDENTITY_MISSING")
            return

        session_key = conversation_key(
            self._app_id,
            self._agent_id,
            self._release_marker,
            message.chat_id,
            message.sender_id,
        )
        # Acquire before the first Redis await. asyncio.Lock is fair, so calls
        # submitted in queue order cannot overtake one another during claim.
        lock = self._session_locks.setdefault(session_key, asyncio.Lock())
        async with lock:
            try:
                claimed = await self._state_store.claim_message(message.message_id)
            except Exception:
                self._log(logging.ERROR, "state_failed", message, error_code="REDIS_CLAIM_FAILED")
                await self._safe_reply(message, SERVICE_UNAVAILABLE_TEXT)
                return
            if not claimed:
                self._log(logging.INFO, "duplicate_dropped", message, result="duplicate")
                return

            try:
                await self._handle_claimed_message(message, session_key)
            except Exception:
                self._log(logging.ERROR, "bridge_failed", message, error_code="UNEXPECTED_BRIDGE_FAILURE")
                # The failure escaped a multi-stage handler, so its execution
                # point is unknown. Preserve the full dedupe window rather than
                # risk replaying Agent or delivery side effects.
                await self._mark_executed(message)
                await self._safe_reply(message, SERVICE_UNAVAILABLE_TEXT)

    async def _handle_claimed_message(self, message: IncomingMessage, session_key: str) -> None:
        if self._allowed_open_ids and message.sender_id not in self._allowed_open_ids:
            await self._reply_and_complete(message, DEMO_ONLY_TEXT)
            return
        if message.message_type != "text":
            await self._reply_and_complete(message, TEXT_ONLY_TEXT)
            return

        question = message.text.strip()
        if not question:
            await self._state_store.mark_replied(message.message_id)
            return
        if len(question) > self._max_question_chars:
            await self._reply_and_complete(message, QUESTION_TOO_LONG_TEXT)
            return

        if question == "/reset":
            await self._state_store.reset_session(session_key)
            await self._reply_and_complete(message, SESSION_RESET_TEXT)
            self._log(logging.INFO, "session_reset", message, result="ok")
            return

        try:
            existing_session_id = await self._state_store.get_session(session_key)
        except Exception:
            self._log(logging.ERROR, "state_failed", message, error_code="REDIS_SESSION_READ_FAILED")
            await self._mark_failed(message)
            await self._safe_reply(message, SERVICE_UNAVAILABLE_TEXT)
            return

        started_at = time.monotonic()
        try:
            reply = await self._executor.ask(question=question, session_id=existing_session_id)
        except AgentExecutionError as exc:
            self._log(
                logging.ERROR,
                "agent_failed",
                message,
                error_code=exc.code,
                agent_total_ms=round((time.monotonic() - started_at) * 1000),
            )
            await self._mark_executed(message)
            await self._safe_reply(message, SERVICE_UNAVAILABLE_TEXT)
            return
        except Exception:
            self._log(
                logging.ERROR,
                "agent_failed",
                message,
                error_code="AGENT_EXECUTION_FAILURE",
                agent_total_ms=round((time.monotonic() - started_at) * 1000),
            )
            await self._mark_executed(message)
            await self._safe_reply(message, SERVICE_UNAVAILABLE_TEXT)
            return
        agent_total_ms = round((time.monotonic() - started_at) * 1000)

        try:
            await self._state_store.put_session(session_key, reply.session_id)
        except Exception:
            self._log(
                logging.ERROR,
                "state_failed",
                message,
                error_code="REDIS_SESSION_WRITE_FAILED",
                agent_total_ms=agent_total_ms,
            )
            await self._mark_executed(message)
            await self._safe_reply(message, SERVICE_UNAVAILABLE_TEXT)
            return

        try:
            await self._channel.send(
                OutgoingMessage(
                    chat_id=message.chat_id,
                    content=reply.content,
                    reply_to_message_id=message.message_id,
                )
            )
        except Exception:
            # The delivery outcome may be ambiguous after a transport failure.
            # Keep a full-window execution tombstone and never invoke the Agent
            # again for this message ID.
            self._log(
                logging.ERROR,
                "reply_failed",
                message,
                error_code="REPLY_FAILED",
                agent_total_ms=agent_total_ms,
            )
            await self._mark_executed(message)
            return

        try:
            await self._state_store.mark_replied(message.message_id)
        except Exception:
            # The user already has the answer. Preserve the one-reply invariant
            # even if the preferred status write failed.
            self._log(
                logging.ERROR,
                "state_failed",
                message,
                error_code="REDIS_MARK_REPLIED",
                agent_total_ms=agent_total_ms,
            )
            await self._mark_executed(message)
            return

        self._log(
            logging.INFO,
            "agent_completed",
            message,
            result="ok",
            agent_total_ms=agent_total_ms,
            session_id=reply.session_id,
        )

    async def _reply_and_complete(self, message: IncomingMessage, text: str) -> None:
        try:
            await self._channel.send(
                OutgoingMessage(
                    chat_id=message.chat_id,
                    content=text,
                    reply_to_message_id=message.message_id,
                )
            )
            await self._state_store.mark_replied(message.message_id)
        except Exception:
            self._log(logging.ERROR, "reply_failed", message, error_code="REPLY_OR_STATE_FAILURE")
            # Sending may already have succeeded before the state write failed.
            await self._mark_executed(message)

    async def _safe_reply(self, message: IncomingMessage, text: str) -> None:
        try:
            await self._channel.send(
                OutgoingMessage(
                    chat_id=message.chat_id,
                    content=text,
                    reply_to_message_id=message.message_id,
                )
            )
        except Exception:
            self._log(logging.ERROR, "reply_failed", message, error_code="REPLY_FAILED")

    async def _mark_failed(self, message: IncomingMessage) -> None:
        try:
            await self._state_store.mark_failed(message.message_id)
        except Exception:
            self._log(logging.ERROR, "state_failed", message, error_code="REDIS_MARK_FAILED")

    async def _mark_executed(self, message: IncomingMessage) -> None:
        try:
            await self._state_store.mark_executed(message.message_id)
        except Exception:
            self._log(logging.ERROR, "state_failed", message, error_code="REDIS_MARK_EXECUTED")

    def _log(
        self,
        level: int,
        event: str,
        message: IncomingMessage,
        *,
        result: str = "failed",
        error_code: str = "",
        agent_total_ms: int | None = None,
        session_id: str = "",
    ) -> None:
        LOGGER.log(
            level,
            "channel_event=%s trace_id=%s message_id_hash=%s sender_id_hash=%s chat_id_hash=%s agent_id=%s release_marker=%s session_id_hash=%s agent_total_ms=%s result=%s error_code=%s",
            event,
            _short_hash(message.message_id),
            _short_hash(message.message_id),
            _short_hash(message.sender_id),
            _short_hash(message.chat_id),
            self._agent_id,
            self._release_marker,
            _short_hash(session_id) if session_id else "",
            agent_total_ms if agent_total_ms is not None else "",
            result,
            error_code,
        )
