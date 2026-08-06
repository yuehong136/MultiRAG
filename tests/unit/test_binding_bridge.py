"""Transport policy the binding bridge applies before invoking a target."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from api.channels.agent_bridge import SERVICE_UNAVAILABLE_TEXT, SESSION_RESET_TEXT, AgentExecutionError, AgentReply
from api.channels.binding_bridge import BindingBridge
from api.channels.core.base import Channel, IncomingMessage, OutgoingMessage
from api.channels.state_store import binding_conversation_key


class _Channel(Channel):
    channel_id = "feishu"
    account_id = "account-1"

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[OutgoingMessage] = []

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def send(self, message: OutgoingMessage) -> None:
        self.sent.append(message)


class _StateStore:
    def __init__(self) -> None:
        self.claimed: set[str] = set()
        self.status: dict[str, str] = {}

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
        del conversation
        return None

    async def put_session(self, conversation: str, session_id: str, *, ttl_seconds: int | None = None) -> None:
        del conversation, session_id, ttl_seconds

    async def reset_session(self, conversation: str) -> None:
        del conversation


class _Executor:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.resets: list[str] = []

    async def ask(self, **kwargs: Any) -> AgentReply:
        self.calls.append(kwargs)
        return AgentReply(content="managed answer", session_id="server-session")

    async def reset(self, *, conversation_key: str) -> None:
        self.resets.append(conversation_key)


class _FailingExecutor(_Executor):
    async def ask(self, **kwargs: Any) -> AgentReply:
        self.calls.append(kwargs)
        raise AgentExecutionError("CHANNEL_EXECUTION_TIMEOUT")


def _message(
    *,
    message_id: str = "message-1",
    content: str = "hello",
    sender_id: str = "ou-user",
    chat_id: str = "oc-chat",
    chat_type: str = "p2p",
    sender_type: str = "user",
) -> IncomingMessage:
    return IncomingMessage(
        channel="feishu",
        account_id="account-1",
        chat_id=chat_id,
        chat_type=chat_type,
        message_id=message_id,
        sender_id=sender_id,
        content=content,
        message_type="text",
        sender_type=sender_type,
    )


def _bridge(
    *,
    channel: _Channel,
    state: _StateStore,
    executor: _Executor,
    binding_id: str = "binding-1",
    allowed_sender_ids: set[str] | None = None,
    private_chat_only: bool = True,
) -> BindingBridge:
    return BindingBridge(
        channel=channel,
        executor=executor,
        state_store=state,
        binding_id=binding_id,
        allowed_sender_ids=allowed_sender_ids or set(),
        max_question_chars=100,
        private_chat_only=private_chat_only,
    )


@pytest.mark.asyncio
async def test_bridge_passes_only_transport_command_fields_to_binding_executor() -> None:
    channel = _Channel()
    state = _StateStore()
    executor = _Executor()
    bridge = _bridge(channel=channel, state=state, executor=executor)

    await bridge.handle_message(_message())

    expected_conversation_key = binding_conversation_key("binding-1", "feishu", "oc-chat", "ou-user")
    assert executor.calls == [
        {
            "question": "hello",
            "event_id": "message-1",
            "conversation_key": expected_conversation_key,
            "provider": "feishu",
            "subject": "ou-user",
            "conversation": "oc-chat",
        }
    ]
    assert (
        not {
            "tenant_id",
            "target_id",
            "target_type",
            "revision_id",
            "target_revision_id",
            "session_id",
            "release",
            "permissions",
        }
        & executor.calls[0].keys()
    )
    assert "binding-1" not in expected_conversation_key
    assert "oc-chat" not in expected_conversation_key
    assert channel.sent == [OutgoingMessage(chat_id="oc-chat", content="managed answer", reply_to_message_id="message-1")]
    assert state.status == {"message-1": "replied"}


@pytest.mark.asyncio
async def test_duplicate_message_never_reaches_binding_executor_twice() -> None:
    channel = _Channel()
    state = _StateStore()
    executor = _Executor()
    bridge = _bridge(channel=channel, state=state, executor=executor)
    message = _message()

    await bridge.handle_message(message)
    await bridge.handle_message(message)

    assert len(executor.calls) == 1
    assert len(channel.sent) == 1


@pytest.mark.asyncio
async def test_reset_uses_only_opaque_server_conversation_key() -> None:
    channel = _Channel()
    state = _StateStore()
    executor = _Executor()
    bridge = _bridge(channel=channel, state=state, executor=executor)

    await bridge.handle_message(_message(content="/reset"))

    expected = binding_conversation_key("binding-1", "feishu", "oc-chat", "ou-user")
    assert executor.calls == []
    assert executor.resets == [expected]
    assert channel.sent[0].content == SESSION_RESET_TEXT
    assert state.status == {"message-1": "replied"}


@pytest.mark.asyncio
async def test_execution_error_is_tombstoned_and_logs_no_raw_message_or_identity(
    caplog: pytest.LogCaptureFixture,
) -> None:
    binding_id = "binding-sensitive-raw"
    message_id = "message-sensitive-raw"
    sender_id = "sender-sensitive-raw"
    chat_id = "chat-sensitive-raw"
    question = "question-sensitive-raw"
    channel = _Channel()
    state = _StateStore()
    executor = _FailingExecutor()
    bridge = _bridge(
        channel=channel,
        state=state,
        executor=executor,
        binding_id=binding_id,
        allowed_sender_ids={sender_id},
    )
    message = _message(
        message_id=message_id,
        content=question,
        sender_id=sender_id,
        chat_id=chat_id,
    )

    caplog.set_level(logging.INFO, logger="api.channels.binding_bridge")
    await bridge.handle_message(message)

    assert state.status == {message_id: "executed"}
    assert channel.sent == [OutgoingMessage(chat_id=chat_id, content=SERVICE_UNAVAILABLE_TEXT, reply_to_message_id=message_id)]
    assert "CHANNEL_EXECUTION_TIMEOUT" in caplog.text
    for sensitive in (binding_id, message_id, sender_id, chat_id, question, "server-session"):
        assert sensitive not in caplog.text
        assert sensitive not in repr(bridge)


@pytest.mark.asyncio
async def test_private_chat_only_policy_decides_whether_group_messages_are_served() -> None:
    """The admin toggle used to be decoration; the runner now honours it."""

    group = _message(chat_type="group")

    ignored = _Executor()
    await _bridge(channel=_Channel(), state=_StateStore(), executor=ignored).handle_message(group)
    assert ignored.calls == []

    served = _Executor()
    await _bridge(
        channel=_Channel(),
        state=_StateStore(),
        executor=served,
        private_chat_only=False,
    ).handle_message(group)
    assert len(served.calls) == 1

    # Widening the chat scope must not widen anything else: a non-user sender
    # (a bot echo, a system notice) is still refused, or two bots could loop.
    echoed = _Executor()
    await _bridge(
        channel=_Channel(),
        state=_StateStore(),
        executor=echoed,
        private_chat_only=False,
    ).handle_message(_message(chat_type="group", sender_type="bot"))
    assert echoed.calls == []


@pytest.mark.asyncio
async def test_a_provider_without_group_support_ignores_group_traffic_regardless_of_policy() -> None:
    """CHN-O4: two independent gates, and the narrower one wins.

    The worker computes ``private_chat_only`` as policy OR-ed with the
    provider's declared inability to carry group chat, so an admin can only
    widen down to what the transport can actually do. Without that, turning the
    toggle off on a private-chat-only provider would have the bot read group
    messages it has no way to answer.
    """

    from api.channel_providers import provider_spec

    capabilities = provider_spec("feishu").capabilities
    resolved = False or not capabilities.group_chat

    executor = _Executor()
    await _bridge(
        channel=_Channel(),
        state=_StateStore(),
        executor=executor,
        private_chat_only=resolved,
    ).handle_message(_message(chat_type="group"))

    # Feishu declares no group support today, so the resolved gate stays shut
    # even though the policy asked for it to open.
    assert capabilities.group_chat is False
    assert resolved is True
    assert executor.calls == []
