"""Transport policy the binding bridge applies before invoking a target."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

import pytest

from api.channels.agent_bridge import (
    DEMO_ONLY_TEXT,
    QUESTION_TOO_LONG_TEXT,
    SERVICE_UNAVAILABLE_TEXT,
    SESSION_RESET_TEXT,
    TEXT_ONLY_TEXT,
    AgentExecutionError,
)
from api.channels.binding_bridge import BindingBridge
from api.channels.core.base import Channel, IncomingMessage, OutgoingMessage, ReplySessionState
from api.channels.execution_events import ExecutionFailedEvent, MessageCompletedEvent, MessageDeltaEvent
from api.channels.feishu.reply import FeishuProgressiveReplySession
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


class _CardFailureChannel(_Channel):
    def __init__(self) -> None:
        super().__init__()
        self.fallbacks: list[tuple[str, str, str, str]] = []

    async def begin_reply(
        self,
        source: IncomingMessage,
        *,
        max_content_chars: int,
    ) -> FeishuProgressiveReplySession:
        return await FeishuProgressiveReplySession.begin(
            transport=self,
            source=source,
            max_content_chars=max_content_chars,
        )

    async def add_typing_reaction(self, message_id: str) -> str:
        del message_id
        return "reaction-1"

    async def remove_reaction(self, message_id: str, reaction_id: str) -> None:
        del message_id, reaction_id

    async def create_streaming_card(self, card_json: str) -> str:
        del card_json
        return "card-1"

    async def reply_card(
        self,
        message_id: str,
        card_id: str,
        *,
        delivery_uuid: str,
    ) -> str:
        del message_id, card_id, delivery_uuid
        return "reply-1"

    async def update_card_text(
        self,
        card_id: str,
        content: str,
        *,
        sequence: int,
        delivery_uuid: str,
    ) -> None:
        del card_id, content, sequence, delivery_uuid
        raise RuntimeError("CardKit unavailable")

    async def finish_streaming_card(
        self,
        card_id: str,
        *,
        sequence: int,
        delivery_uuid: str,
    ) -> None:
        del card_id, sequence, delivery_uuid

    async def reply_content(
        self,
        message_id: str,
        content: str,
        *,
        message_type: str,
        delivery_uuid: str,
    ) -> None:
        self.fallbacks.append((message_id, content, message_type, delivery_uuid))


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
    def __init__(
        self,
        events: list[MessageDeltaEvent | MessageCompletedEvent | ExecutionFailedEvent] | None = None,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self.resets: list[str] = []
        self.events = (
            events
            if events is not None
            else [
                MessageDeltaEvent(content="managed ", session_id="server-session"),
                MessageDeltaEvent(content="answer", session_id="server-session"),
                MessageCompletedEvent(session_id="server-session"),
            ]
        )

    async def stream(
        self,
        **kwargs: Any,
    ) -> AsyncIterator[MessageDeltaEvent | MessageCompletedEvent | ExecutionFailedEvent]:
        self.calls.append(kwargs)
        for event in self.events:
            yield event

    async def reset(self, *, conversation_key: str) -> None:
        self.resets.append(conversation_key)


class _FailingExecutor(_Executor):
    async def stream(
        self,
        **kwargs: Any,
    ) -> AsyncIterator[MessageDeltaEvent | MessageCompletedEvent | ExecutionFailedEvent]:
        self.calls.append(kwargs)
        yield MessageDeltaEvent(content="partial answer", session_id="server-session")
        raise AgentExecutionError("CHANNEL_EXECUTION_TIMEOUT")


def _message(
    *,
    message_id: str = "message-1",
    content: str = "hello",
    sender_id: str = "ou-user",
    chat_id: str = "oc-chat",
    chat_type: str = "p2p",
    sender_type: str = "user",
    message_type: str = "text",
) -> IncomingMessage:
    return IncomingMessage(
        channel="feishu",
        account_id="account-1",
        chat_id=chat_id,
        chat_type=chat_type,
        message_id=message_id,
        sender_id=sender_id,
        content=content,
        message_type=message_type,
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
        max_answer_chars=4000,
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


@pytest.mark.parametrize(
    ("message", "allowed_sender_ids", "expected_text"),
    [
        (_message(sender_id="blocked-user"), {"allowed-user"}, DEMO_ONLY_TEXT),
        (_message(message_type="image"), set(), TEXT_ONLY_TEXT),
        (_message(content="x" * 101), set(), QUESTION_TOO_LONG_TEXT),
    ],
    ids=["sender-allowlist", "text-only", "question-limit"],
)
@pytest.mark.asyncio
async def test_existing_input_guards_still_finish_without_starting_execution(
    message: IncomingMessage,
    allowed_sender_ids: set[str],
    expected_text: str,
) -> None:
    channel = _Channel()
    state = _StateStore()
    executor = _Executor()

    await _bridge(
        channel=channel,
        state=state,
        executor=executor,
        allowed_sender_ids=allowed_sender_ids,
    ).handle_message(message)

    assert executor.calls == []
    assert channel.sent == [
        OutgoingMessage(
            chat_id=message.chat_id,
            content=expected_text,
            reply_to_message_id=message.message_id,
        )
    ]
    assert state.status == {message.message_id: "replied"}


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


@pytest.mark.asyncio
async def test_execution_failed_after_partial_deltas_delivers_only_the_safe_failure() -> None:
    channel = _Channel()
    state = _StateStore()
    executor = _Executor(
        [
            MessageDeltaEvent(content="partial answer", session_id="server-session"),
            ExecutionFailedEvent(error_code="TARGET_EXECUTION_FAILED", session_id="server-session"),
        ]
    )

    await _bridge(channel=channel, state=state, executor=executor).handle_message(_message())

    assert channel.sent == [
        OutgoingMessage(
            chat_id="oc-chat",
            content=SERVICE_UNAVAILABLE_TEXT,
            reply_to_message_id="message-1",
        )
    ]
    assert state.status == {"message-1": "executed"}


@pytest.mark.asyncio
async def test_cardkit_delivery_failure_falls_back_without_reexecuting_agent() -> None:
    channel = _CardFailureChannel()
    state = _StateStore()
    executor = _Executor(
        [
            MessageDeltaEvent(content="完整", session_id="server-session"),
            MessageDeltaEvent(content="回答", session_id="server-session"),
            MessageCompletedEvent(session_id="server-session"),
        ]
    )

    await _bridge(channel=channel, state=state, executor=executor).handle_message(_message())

    assert len(executor.calls) == 1
    assert len(channel.fallbacks) == 1
    assert channel.fallbacks[0][0] == "message-1"
    assert "完整回答" in channel.fallbacks[0][1]
    assert channel.fallbacks[0][2] == "post"
    assert state.status == {"message-1": "replied"}


@pytest.mark.asyncio
async def test_bridge_buffers_split_reasoning_markers_without_leaking_them() -> None:
    channel = _Channel()
    state = _StateStore()
    executor = _Executor(
        [
            MessageDeltaEvent(content="visible<thi", session_id="server-session"),
            MessageDeltaEvent(content="nk>private reasoning</th", session_id="server-session"),
            MessageDeltaEvent(content="ink> answer", session_id="server-session"),
            MessageCompletedEvent(session_id="server-session"),
        ]
    )

    await _bridge(channel=channel, state=state, executor=executor).handle_message(_message())

    assert channel.sent == [
        OutgoingMessage(
            chat_id="oc-chat",
            content="visible answer",
            reply_to_message_id="message-1",
        )
    ]
    assert "private reasoning" not in channel.sent[0].content


class _ControlledReplySession:
    def __init__(self, *, fail_operation: str) -> None:
        self.state = ReplySessionState.OPEN
        self.fail_operation = fail_operation
        self.appended: list[str] = []
        self.complete_calls = 0
        self.fail_calls = 0

    async def append(self, content: str) -> None:
        if self.fail_operation == "append":
            raise RuntimeError("append failed")
        self.appended.append(content)

    async def complete(self) -> None:
        self.complete_calls += 1
        self.state = ReplySessionState.COMPLETED
        if self.fail_operation == "complete":
            raise RuntimeError("complete failed")

    async def fail(self, error_code: str) -> None:
        del error_code
        self.fail_calls += 1
        self.state = ReplySessionState.FAILED
        if self.fail_operation == "fail":
            raise RuntimeError("fail failed")


class _ReplySessionChannel(_Channel):
    def __init__(self, session: _ControlledReplySession) -> None:
        super().__init__()
        self.session = session
        self.begin_calls = 0

    async def begin_reply(
        self,
        source: IncomingMessage,
        *,
        max_content_chars: int,
    ) -> _ControlledReplySession:
        del source, max_content_chars
        self.begin_calls += 1
        return self.session


@pytest.mark.parametrize(
    ("session_failure", "events", "expected_complete_calls", "expected_fail_calls"),
    [
        (
            "append",
            [MessageDeltaEvent(content="answer"), MessageCompletedEvent(session_id="session-server")],
            0,
            1,
        ),
        (
            "complete",
            [MessageDeltaEvent(content="answer"), MessageCompletedEvent(session_id="session-server")],
            1,
            0,
        ),
        (
            "fail",
            [ExecutionFailedEvent(error_code="TARGET_EXECUTION_FAILED")],
            0,
            1,
        ),
    ],
)
@pytest.mark.asyncio
async def test_reply_session_failures_are_tombstoned_without_duplicate_delivery(
    session_failure: str,
    events: list[MessageDeltaEvent | MessageCompletedEvent | ExecutionFailedEvent],
    expected_complete_calls: int,
    expected_fail_calls: int,
) -> None:
    session = _ControlledReplySession(fail_operation=session_failure)
    channel = _ReplySessionChannel(session)
    state = _StateStore()

    await _bridge(channel=channel, state=state, executor=_Executor(events)).handle_message(_message())

    assert channel.begin_calls == 1
    assert channel.sent == []
    assert session.complete_calls == expected_complete_calls
    assert session.fail_calls == expected_fail_calls
    assert state.status == {"message-1": "executed"}


class _SerialExecutor(_Executor):
    def __init__(self) -> None:
        super().__init__()
        self.active = 0
        self.max_active = 0

    async def stream(
        self,
        **kwargs: Any,
    ) -> AsyncIterator[MessageDeltaEvent | MessageCompletedEvent | ExecutionFailedEvent]:
        self.calls.append(kwargs)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0)
            question = str(kwargs["question"])
            yield MessageDeltaEvent(content=question, session_id=f"session-{question}")
            await asyncio.sleep(0)
            yield MessageCompletedEvent(session_id=f"session-{question}")
        finally:
            self.active -= 1


@pytest.mark.asyncio
async def test_same_conversation_streams_remain_strictly_serial() -> None:
    channel = _Channel()
    state = _StateStore()
    executor = _SerialExecutor()
    bridge = _bridge(channel=channel, state=state, executor=executor)

    await asyncio.gather(
        bridge.handle_message(_message(message_id="message-1", content="first")),
        bridge.handle_message(_message(message_id="message-2", content="second")),
    )

    assert executor.max_active == 1
    assert [call["question"] for call in executor.calls] == ["first", "second"]
    assert [message.content for message in channel.sent] == ["first", "second"]
    assert state.status == {"message-1": "replied", "message-2": "replied"}
