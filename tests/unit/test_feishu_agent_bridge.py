from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable

import httpx
import pytest

from api.channels.agent_bridge import (
    DEMO_ONLY_TEXT,
    SERVICE_UNAVAILABLE_TEXT,
    SESSION_RESET_TEXT,
    AgentExecutionError,
    AgentReply,
    FeishuAgentBridge,
    MultiRAGAgentClient,
)
from api.channels.core.base import Channel, IncomingMessage, OutgoingMessage


class FakeChannel(Channel):
    channel_id = "feishu"
    account_id = "test-account"

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[OutgoingMessage] = []

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def send(self, message: OutgoingMessage) -> None:
        self.sent.append(message)


class FailingChannel(FakeChannel):
    def __init__(self) -> None:
        super().__init__()
        self.attempts = 0

    async def send(self, message: OutgoingMessage) -> None:
        del message
        self.attempts += 1
        raise RuntimeError("ambiguous delivery failure")


class FakeStateStore:
    def __init__(self) -> None:
        self.claimed: set[str] = set()
        self.status: dict[str, str] = {}
        self.sessions: dict[str, str] = {}

    async def claim_message(self, message_id: str) -> bool:
        if message_id in self.claimed:
            return False
        self.claimed.add(message_id)
        self.status[message_id] = "processing"
        return True

    async def mark_replied(self, message_id: str) -> None:
        self.status[message_id] = "replied"

    async def mark_executed(self, message_id: str) -> None:
        self.status[message_id] = "executed"

    async def mark_failed(self, message_id: str) -> None:
        self.status[message_id] = "failed"

    async def get_session(self, conversation: str) -> str | None:
        return self.sessions.get(conversation)

    async def put_session(self, conversation: str, session_id: str, *, ttl_seconds: int | None = None) -> None:
        del ttl_seconds
        self.sessions[conversation] = session_id

    async def reset_session(self, conversation: str) -> None:
        self.sessions.pop(conversation, None)


class MarkRepliedFailingStateStore(FakeStateStore):
    async def mark_replied(self, message_id: str) -> None:
        del message_id
        raise RuntimeError("redis unavailable after delivery")


class SlowFirstClaimStateStore(FakeStateStore):
    async def claim_message(self, message_id: str) -> bool:
        if message_id == "message-first":
            await asyncio.sleep(0.02)
        return await super().claim_message(message_id)


class FakeExecutor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    async def ask(self, *, question: str, session_id: str | None) -> AgentReply:
        self.calls.append((question, session_id))
        return AgentReply(content=f"answer:{question}", session_id=session_id or "session-1")


class FailingExecutor(FakeExecutor):
    async def ask(self, *, question: str, session_id: str | None) -> AgentReply:
        self.calls.append((question, session_id))
        raise AgentExecutionError("AGENT_TIMEOUT")


class ConcurrentExecutor:
    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0

    async def ask(self, *, question: str, session_id: str | None) -> AgentReply:
        del session_id
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0.02)
        self.active -= 1
        return AgentReply(content=question, session_id=f"session-{question}")


def _incoming(
    message_id: str,
    content: str = "hello",
    *,
    sender_id: str = "ou-user",
    chat_id: str = "oc-chat",
    chat_type: str = "p2p",
    message_type: str = "text",
) -> IncomingMessage:
    return IncomingMessage(
        channel="feishu",
        account_id="test-account",
        chat_id=chat_id,
        message_id=message_id,
        sender_id=sender_id,
        content=content,
        message_type=message_type,
        chat_type=chat_type,
        sender_type="user",
    )


def _bridge(
    *,
    channel: FakeChannel,
    state: FakeStateStore,
    executor: FakeExecutor | ConcurrentExecutor,
    allowed_open_ids: set[str] | None = None,
) -> FeishuAgentBridge:
    return FeishuAgentBridge(
        channel=channel,
        executor=executor,
        state_store=state,
        app_id="cli-app",
        agent_id="agent-1",
        release_marker="demo-v1",
        allowed_open_ids=allowed_open_ids or set(),
        max_question_chars=100,
    )


@pytest.mark.asyncio
async def test_agent_client_aggregates_sse_without_reasoning_or_identity_fields() -> None:
    captured: dict[str, object] = {}
    frames = [
        {"event": "message", "session_id": "session-1", "data": {"content": "", "start_to_think": True}},
        {"event": "message", "session_id": "session-1", "data": {"content": "private reasoning"}},
        {"event": "message", "session_id": "session-1", "data": {"content": "", "end_to_think": True}},
        {"event": "message", "session_id": "session-1", "data": {"content": "Hello "}},
        {"event": "message", "session_id": "session-1", "data": {"content": "world<think>hidden</think>"}},
        {"event": "message_end", "session_id": "session-1", "data": {}},
    ]
    sse = "".join(f"data:{json.dumps(frame)}\n\n" for frame in frames) + "data: [DONE]\n\n"

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        captured["authorization"] = request.headers.get("Authorization")
        return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = MultiRAGAgentClient(
            base_url="http://multirag.local",
            agent_id="agent-1",
            api_token="secret-token",
            client=http_client,
        )
        reply = await client.ask(question="question", session_id=None)

    assert reply == AgentReply(content="Hello world", session_id="session-1")
    assert captured["body"] == {"question": "question", "stream": True, "release": True}
    assert captured["authorization"] == "Bearer secret-token"
    assert not {"user_id", "custom_header", "inputs", "metadata"} & set(captured["body"])


@pytest.mark.asyncio
async def test_default_agent_client_ignores_environment_proxy_settings() -> None:
    client = MultiRAGAgentClient(
        base_url="http://127.0.0.1:8123",
        agent_id="agent-1",
        api_token="secret-token",
    )

    try:
        assert client._client._trust_env is False
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_agent_client_rejects_incomplete_sse() -> None:
    response_body = 'data:{"event":"message","session_id":"s1","data":{"content":"partial"}}\n\n'

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=response_body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = MultiRAGAgentClient(
            base_url="http://multirag.local",
            agent_id="agent-1",
            api_token="secret-token",
            client=http_client,
        )
        with pytest.raises(AgentExecutionError, match="AGENT_SSE_INCOMPLETE"):
            await client.ask(question="question", session_id=None)


@pytest.mark.asyncio
async def test_agent_client_enforces_wall_clock_timeout() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.05)
        return httpx.Response(200, text="data: [DONE]\n\n")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = MultiRAGAgentClient(
            base_url="http://multirag.local",
            agent_id="agent-1",
            api_token="secret-token",
            total_timeout_seconds=0.01,
            client=http_client,
        )
        with pytest.raises(AgentExecutionError, match="AGENT_TIMEOUT"):
            await client.ask(question="question", session_id=None)


@pytest.mark.asyncio
async def test_agent_preflight_checks_ping_and_agent_ownership() -> None:
    paths: list[str] = []
    authorizations: list[str | None] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        authorizations.append(request.headers.get("Authorization"))
        if request.url.path.endswith("/system/ping"):
            return httpx.Response(200, text="pong")
        return httpx.Response(200, json={"code": 0, "data": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = MultiRAGAgentClient(
            base_url="http://multirag.local",
            agent_id="agent-1",
            api_token="secret-token",
            client=http_client,
        )
        await client.preflight()

    assert paths == ["/api/v1/system/ping", "/api/v1/agents/agent-1/sessions"]
    assert authorizations == [None, "Bearer secret-token"]


@pytest.mark.asyncio
async def test_bridge_reuses_session_and_deduplicates_message() -> None:
    channel = FakeChannel()
    state = FakeStateStore()
    executor = FakeExecutor()
    bridge = _bridge(channel=channel, state=state, executor=executor)

    await bridge.handle_message(_incoming("message-1", "first"))
    await bridge.handle_message(_incoming("message-2", "second"))
    await bridge.handle_message(_incoming("message-2", "second"))

    assert executor.calls == [("first", None), ("second", "session-1")]
    assert [message.content for message in channel.sent] == ["answer:first", "answer:second"]
    assert state.status == {"message-1": "replied", "message-2": "replied"}


@pytest.mark.asyncio
async def test_bridge_reset_and_allowlist_do_not_call_agent() -> None:
    channel = FakeChannel()
    state = FakeStateStore()
    executor = FakeExecutor()
    bridge = _bridge(channel=channel, state=state, executor=executor, allowed_open_ids={"ou-allowed"})

    await bridge.handle_message(_incoming("message-denied", sender_id="ou-denied"))
    await bridge.handle_message(_incoming("message-reset", "/reset", sender_id="ou-allowed"))

    assert executor.calls == []
    assert [message.content for message in channel.sent] == [DEMO_ONLY_TEXT, SESSION_RESET_TEXT]


@pytest.mark.asyncio
async def test_bridge_serializes_same_conversation_but_not_different_users() -> None:
    channel = FakeChannel()
    state = FakeStateStore()
    same_user_executor = ConcurrentExecutor()
    same_user_bridge = _bridge(channel=channel, state=state, executor=same_user_executor)

    await asyncio.gather(
        same_user_bridge.handle_message(_incoming("message-1", "first")),
        same_user_bridge.handle_message(_incoming("message-2", "second")),
    )
    assert same_user_executor.max_active == 1

    different_user_executor = ConcurrentExecutor()
    different_user_bridge = _bridge(channel=channel, state=FakeStateStore(), executor=different_user_executor)
    await asyncio.gather(
        different_user_bridge.handle_message(_incoming("message-3", "third", sender_id="ou-one")),
        different_user_bridge.handle_message(_incoming("message-4", "fourth", sender_id="ou-two")),
    )
    assert different_user_executor.max_active == 2


async def test_bridge_lock_prevents_slow_first_claim_from_reordering_messages() -> None:
    channel = FakeChannel()
    state = SlowFirstClaimStateStore()
    executor = FakeExecutor()
    bridge = _bridge(channel=channel, state=state, executor=executor)

    await asyncio.gather(
        bridge.handle_message(_incoming("message-first", "first")),
        bridge.handle_message(_incoming("message-second", "second")),
    )

    assert [question for question, _session_id in executor.calls] == ["first", "second"]


async def test_agent_failure_uses_full_window_execution_tombstone() -> None:
    channel = FakeChannel()
    state = FakeStateStore()
    executor = FailingExecutor()
    bridge = _bridge(channel=channel, state=state, executor=executor)

    await bridge.handle_message(_incoming("message-ambiguous"))

    assert state.status["message-ambiguous"] == "executed"
    assert [message.content for message in channel.sent] == [SERVICE_UNAVAILABLE_TEXT]


@pytest.mark.asyncio
async def test_bridge_returns_safe_error_when_state_claim_fails() -> None:
    channel = FakeChannel()
    state = FakeStateStore()
    executor = FakeExecutor()
    bridge = _bridge(channel=channel, state=state, executor=executor)

    async def fail_claim(_message_id: str) -> bool:
        raise RuntimeError("redis endpoint and credential must not reach the user")

    state.claim_message = fail_claim  # type: ignore[method-assign]
    await bridge.handle_message(_incoming("message-1"))

    assert executor.calls == []
    assert [message.content for message in channel.sent] == [SERVICE_UNAVAILABLE_TEXT]


@pytest.mark.asyncio
async def test_bridge_does_not_retry_ambiguous_reply_failure() -> None:
    channel = FailingChannel()
    state = FakeStateStore()
    executor = FakeExecutor()
    bridge = _bridge(channel=channel, state=state, executor=executor)

    await bridge.handle_message(_incoming("message-1"))

    assert channel.attempts == 1
    assert state.status["message-1"] == "executed"


@pytest.mark.asyncio
async def test_pre_agent_notice_delivery_failure_uses_full_dedupe_window() -> None:
    channel = FailingChannel()
    state = FakeStateStore()
    executor = FakeExecutor()
    bridge = _bridge(
        channel=channel,
        state=state,
        executor=executor,
        allowed_open_ids={"ou-allowed"},
    )

    await bridge.handle_message(_incoming("message-denied", sender_id="ou-denied"))

    assert executor.calls == []
    assert channel.attempts == 1
    assert state.status["message-denied"] == "executed"


@pytest.mark.asyncio
async def test_bridge_does_not_send_second_reply_when_mark_replied_fails() -> None:
    channel = FakeChannel()
    state = MarkRepliedFailingStateStore()
    executor = FakeExecutor()
    bridge = _bridge(channel=channel, state=state, executor=executor)

    await bridge.handle_message(_incoming("message-1"))

    assert [message.content for message in channel.sent] == ["answer:hello"]
    assert state.status["message-1"] == "executed"


async def test_bridge_logs_exclude_raw_identity_question_and_answer(caplog: pytest.LogCaptureFixture) -> None:
    channel = FakeChannel()
    state = FakeStateStore()
    executor = FakeExecutor()
    bridge = _bridge(channel=channel, state=state, executor=executor)
    message = _incoming(
        "raw-message-id-private",
        "raw-question-private",
        sender_id="raw-open-id-private",
        chat_id="raw-chat-id-private",
    )
    caplog.set_level(logging.INFO, logger="api.channels.agent_bridge")

    await bridge.handle_message(message)

    rendered = caplog.text
    for private_value in (
        message.message_id,
        message.sender_id,
        message.chat_id,
        message.content,
        "answer:raw-question-private",
    ):
        assert private_value not in rendered
    assert "message_id_hash=" in rendered
    assert "sender_id_hash=" in rendered
    assert "chat_id_hash=" in rendered
    assert "agent_total_ms=" in rendered


@pytest.mark.parametrize(
    ("mutate", "expected_calls", "expected_replies"),
    [
        (lambda message: _incoming(message.message_id, chat_type="group"), 0, 0),
        (lambda message: _incoming(message.message_id, message_type="image"), 0, 1),
        (lambda message: _incoming(message.message_id, content="   "), 0, 0),
        (lambda message: _incoming(message.message_id, content="x" * 101), 0, 1),
    ],
)
async def test_bridge_filters_unsupported_inputs(
    mutate: Callable[[IncomingMessage], IncomingMessage],
    expected_calls: int,
    expected_replies: int,
) -> None:
    channel = FakeChannel()
    state = FakeStateStore()
    executor = FakeExecutor()
    bridge = _bridge(channel=channel, state=state, executor=executor)

    await bridge.handle_message(mutate(_incoming("message-1")))

    assert len(executor.calls) == expected_calls
    assert len(channel.sent) == expected_replies
