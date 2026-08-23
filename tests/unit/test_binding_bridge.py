"""Transport policy the binding bridge applies before invoking a target."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import pytest

from api.channel_capabilities import EffectiveReplyCapabilities
from api.channels.agent_bridge import (
    DEMO_ONLY_TEXT,
    QUESTION_TOO_LONG_TEXT,
    SERVICE_UNAVAILABLE_TEXT,
    SESSION_RESET_TEXT,
    TEXT_ONLY_TEXT,
    AgentExecutionError,
)
from api.channels.binding_bridge import BindingBridge
from api.channels.core.base import (
    Channel,
    ChannelAction,
    ChannelFormAction,
    IncomingIdentityAssertion,
    IncomingIdentityIdentifier,
    IncomingMessage,
    OutgoingMessage,
    ReplyContext,
    ReplySessionState,
    ReplyStatus,
)
from api.channels.execution_events import (
    BindingExecutionEvent,
    ExecutionFailedEvent,
    InteractionRequiredEvent,
    MessageCompletedEvent,
    MessageDeltaEvent,
)
from api.channels.feishu.reply import FeishuProgressiveReplySession
from api.channels.interaction_models import ClaimedInteractionDelivery
from api.channels.state_store import binding_conversation_key
from api.channels.telemetry import (
    NOOP_CHANNEL_TELEMETRY,
    ChannelMessageOutcome,
    ChannelMetric,
    ChannelReason,
    ChannelResult,
    ChannelTelemetry,
    InMemoryChannelTelemetry,
)

_FULL_REPLY_CAPABILITIES = EffectiveReplyCapabilities(
    progressive_reply=True,
    cancel_queued=True,
    cancel_running=True,
    regenerate=True,
    retry=True,
    feedback=True,
)


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


class _SendFailureChannel(_Channel):
    async def send(self, message: OutgoingMessage) -> None:
        del message
        raise RuntimeError("provider send failed")


class _CardFailureChannel(_Channel):
    def __init__(self) -> None:
        super().__init__()
        self.fallbacks: list[tuple[str, str, str, str]] = []

    async def begin_reply(
        self,
        source: IncomingMessage,
        *,
        max_content_chars: int,
        context: ReplyContext | None = None,
    ) -> FeishuProgressiveReplySession:
        return await FeishuProgressiveReplySession.begin(
            transport=self,
            source=source,
            max_content_chars=max_content_chars,
            context=context,
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

    async def batch_update_card(
        self,
        card_id: str,
        actions: list[dict[str, object]],
        *,
        sequence: int,
        delivery_uuid: str,
    ) -> None:
        del card_id, actions, sequence, delivery_uuid

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


class _MarkRepliedFailureState(_StateStore):
    async def mark_replied(self, message_id: str) -> None:
        del message_id
        raise RuntimeError("state write failed")


class _Executor:
    def __init__(
        self,
        events: list[BindingExecutionEvent] | None = None,
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
    ) -> AsyncIterator[BindingExecutionEvent]:
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


class _ResetFailingExecutor(_Executor):
    async def reset(self, *, conversation_key: str) -> None:
        del conversation_key
        raise AgentExecutionError("CHANNEL_RESET_FAILED")


def _message(
    *,
    message_id: str = "message-1",
    content: str = "hello",
    sender_id: str = "ou-user",
    chat_id: str = "oc-chat",
    chat_type: str = "p2p",
    sender_type: str = "user",
    message_type: str = "text",
    operation: Literal["message", "regenerate"] = "message",
    identity: IncomingIdentityAssertion | None = None,
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
        operation=operation,
        identity=identity,
    )


def _bridge(
    *,
    channel: _Channel,
    state: _StateStore,
    executor: _Executor,
    binding_id: str = "binding-1",
    allowed_sender_ids: set[str] | None = None,
    private_chat_only: bool = True,
    capabilities: EffectiveReplyCapabilities | None = None,
    interaction_client: Any | None = None,
    interaction_presenter: Any | None = None,
    telemetry: ChannelTelemetry = NOOP_CHANNEL_TELEMETRY,
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
        capabilities=capabilities or _FULL_REPLY_CAPABILITIES,
        interaction_client=interaction_client,
        interaction_presenter=interaction_presenter,
        telemetry=telemetry,
        provider_name="feishu",
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
            "identity": None,
            "operation": "message",
            "presentation_ref": None,
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
async def test_bridge_records_execution_and_first_visible_content_without_identity_labels() -> None:
    telemetry = InMemoryChannelTelemetry()
    bridge = _bridge(
        channel=_Channel(),
        state=_StateStore(),
        executor=_Executor(),
        telemetry=telemetry,
    )

    outcome = await bridge.handle_message(_message())

    assert outcome.result is ChannelResult.OK
    events = telemetry.snapshot().recent_events
    assert sum(event.metric is ChannelMetric.EXECUTION_DURATION_SECONDS for event in events) == 1
    assert sum(event.metric is ChannelMetric.FIRST_CONTENT_SECONDS for event in events) == 1
    assert sum(event.metric is ChannelMetric.DELIVERY_TOTAL for event in events) == 1
    assert "ou-user" not in repr(events)
    assert "oc-chat" not in repr(events)
    assert "message-1" not in repr(events)


@pytest.mark.asyncio
async def test_bridge_duplicate_returns_closed_drop_outcome() -> None:
    state = _StateStore()
    bridge = _bridge(channel=_Channel(), state=state, executor=_Executor())

    assert (await bridge.handle_message(_message())).result is ChannelResult.OK
    duplicate = await bridge.handle_message(_message())

    assert duplicate.result is ChannelResult.DUPLICATE
    assert duplicate.reason is ChannelReason.DUPLICATE


@pytest.mark.asyncio
async def test_direct_reply_send_failure_is_not_reported_as_ok() -> None:
    bridge = _bridge(
        channel=_SendFailureChannel(),
        state=_StateStore(),
        executor=_Executor(),
    )

    outcome = await bridge.handle_message(_message(message_type="image"))

    assert outcome == ChannelMessageOutcome(
        ChannelResult.FAILED,
        ChannelReason.DELIVERY_FAILURE,
    )


@pytest.mark.asyncio
async def test_empty_message_state_failure_is_not_reported_as_ok() -> None:
    bridge = _bridge(
        channel=_Channel(),
        state=_MarkRepliedFailureState(),
        executor=_Executor(),
    )

    outcome = await bridge.handle_message(_message(content="   "))

    assert outcome == ChannelMessageOutcome(
        ChannelResult.FAILED,
        ChannelReason.STATE_FAILURE,
    )


@pytest.mark.asyncio
async def test_completed_reply_state_failure_is_not_reported_as_delivery_failure() -> None:
    channel = _Channel()
    bridge = _bridge(
        channel=channel,
        state=_MarkRepliedFailureState(),
        executor=_Executor(),
    )

    outcome = await bridge.handle_message(_message())

    assert outcome == ChannelMessageOutcome(
        ChannelResult.FAILED,
        ChannelReason.STATE_FAILURE,
    )
    assert channel.sent == [
        OutgoingMessage(
            chat_id="oc-chat",
            content="managed answer",
            reply_to_message_id="message-1",
        )
    ]


@pytest.mark.asyncio
async def test_reset_failure_is_not_reported_as_ok() -> None:
    bridge = _bridge(
        channel=_Channel(),
        state=_StateStore(),
        executor=_ResetFailingExecutor(),
    )

    outcome = await bridge.handle_message(_message(content="/reset"))

    assert outcome == ChannelMessageOutcome(
        ChannelResult.FAILED,
        ChannelReason.EXECUTION_FAILURE,
    )


def test_bridge_policy_rejections_have_closed_reasons() -> None:
    bridge = _bridge(channel=_Channel(), state=_StateStore(), executor=_Executor())

    assert bridge.message_rejection_reason(_message(chat_type="group")) is ChannelReason.PRIVATE_CHAT_REQUIRED
    assert bridge.message_rejection_reason(_message(sender_type="bot")) is ChannelReason.USER_SENDER_REQUIRED
    assert bridge.message_rejection_reason(_message(sender_id="")) is ChannelReason.MESSAGE_IDENTITY_MISSING


@pytest.mark.asyncio
async def test_bridge_collects_existing_shutdown_finalized_event_in_telemetry() -> None:
    telemetry = InMemoryChannelTelemetry()
    state = _StateStore()
    bridge = _bridge(
        channel=_Channel(),
        state=state,
        executor=_Executor(),
        telemetry=telemetry,
    )
    message = _message(message_id="queued-for-shutdown")
    bridge.message_queued(message, queue_position=1)
    for _ in range(100):
        if state.status.get(message.message_id) == "processing":
            break
        await asyncio.sleep(0)

    await bridge.close()

    events = telemetry.snapshot().recent_events
    shutdown = [event for event in events if event.metric is ChannelMetric.SHUTDOWN_TOTAL]
    assert len(shutdown) == 1
    assert shutdown[0].labels.result is ChannelResult.OK
    assert any(event.metric is ChannelMetric.SHUTDOWN_REPLIES_TOTAL for event in events)


@pytest.mark.asyncio
async def test_bridge_passes_structured_identity_to_binding_executor() -> None:
    identity = IncomingIdentityAssertion(
        provider="feishu",
        provider_tenant_key="tenant-sensitive",
        identifiers=(
            IncomingIdentityIdentifier(kind="open_id", value="open-sensitive"),
            IncomingIdentityIdentifier(kind="user_id", value="user-sensitive"),
            IncomingIdentityIdentifier(kind="union_id", value="union-sensitive"),
        ),
    )
    executor = _Executor()

    await _bridge(
        channel=_Channel(),
        state=_StateStore(),
        executor=executor,
    ).handle_message(_message(identity=identity))

    assert executor.calls[0]["identity"] is identity


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
        self.replaced: list[str] = []
        self.operations: list[str] = []
        self.complete_calls = 0
        self.fail_calls = 0

    @property
    def reply_message_id(self) -> str:
        return ""

    async def append(self, content: str) -> None:
        self.operations.append("append")
        if self.fail_operation == "append":
            raise RuntimeError("append failed")
        self.appended.append(content)

    async def replace(self, content: str) -> None:
        self.operations.append("replace")
        if self.fail_operation == "replace":
            raise RuntimeError("replace failed")
        self.replaced.append(content)

    async def set_status(
        self,
        status: ReplyStatus,
        *,
        queue_position: int = 0,
    ) -> None:
        del status, queue_position

    async def complete(self) -> None:
        self.operations.append("complete")
        self.complete_calls += 1
        self.state = ReplySessionState.COMPLETED
        if self.fail_operation == "complete":
            raise RuntimeError("complete failed")

    async def fail(self, error_code: str) -> None:
        del error_code
        self.operations.append("fail")
        self.fail_calls += 1
        self.state = ReplySessionState.FAILED
        if self.fail_operation == "fail":
            raise RuntimeError("fail failed")

    async def cancel(self) -> None:
        self.state = ReplySessionState.CANCELLED

    async def acknowledge_feedback(self, *, helpful: bool) -> None:
        del helpful


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
        context: ReplyContext | None = None,
    ) -> _ControlledReplySession:
        del source, max_content_chars, context
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
            "replace",
            [
                MessageDeltaEvent(content="raw answer"),
                MessageCompletedEvent(
                    session_id="session-server",
                    content="authoritative answer",
                ),
            ],
            0,
            1,
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


@pytest.mark.asyncio
async def test_terminal_snapshot_replaces_deltas_before_reply_completion() -> None:
    session = _ControlledReplySession(fail_operation="")
    channel = _ReplySessionChannel(session)
    state = _StateStore()
    executor = _Executor(
        [
            MessageDeltaEvent(content="raw answer"),
            MessageCompletedEvent(
                session_id="session-server",
                content="raw ##0$$ answer",
            ),
        ]
    )

    await _bridge(channel=channel, state=state, executor=executor).handle_message(_message())

    assert session.appended == ["raw answer"]
    assert session.replaced == ["raw ##0$$ answer"]
    assert session.operations == ["append", "replace", "complete"]
    assert state.status == {"message-1": "replied"}


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
async def test_bridge_does_not_add_a_second_conversation_lock() -> None:
    channel = _Channel()
    state = _StateStore()
    executor = _SerialExecutor()
    bridge = _bridge(channel=channel, state=state, executor=executor)

    await asyncio.gather(
        bridge.handle_message(_message(message_id="message-1", content="first")),
        bridge.handle_message(_message(message_id="message-2", content="second")),
    )

    # Conversation ordering belongs to ChannelWorker. Direct bridge calls are
    # independent, which prevents the old worker-lock + bridge-lock layering
    # from returning when the lifecycle is changed.
    assert executor.max_active == 2
    assert [call["question"] for call in executor.calls] == ["first", "second"]
    assert [message.content for message in channel.sent] == ["first", "second"]
    assert state.status == {"message-1": "replied", "message-2": "replied"}


class _LifecycleReplySession:
    def __init__(self) -> None:
        self.state = ReplySessionState.OPEN
        self.statuses: list[ReplyStatus] = []
        self.appended: list[str] = []
        self.replaced: list[str] = []
        self.feedback: list[bool] = []
        self.cancel_calls = 0

    @property
    def reply_message_id(self) -> str:
        return "reply-message-1"

    async def append(self, content: str) -> None:
        self.appended.append(content)

    async def replace(self, content: str) -> None:
        self.replaced.append(content)

    async def set_status(
        self,
        status: ReplyStatus,
        *,
        queue_position: int = 0,
    ) -> None:
        del queue_position
        self.statuses.append(status)

    async def complete(self) -> None:
        self.state = ReplySessionState.COMPLETED

    async def fail(self, error_code: str) -> None:
        del error_code
        self.state = ReplySessionState.FAILED

    async def cancel(self) -> None:
        self.cancel_calls += 1
        self.state = ReplySessionState.CANCELLED

    async def acknowledge_feedback(self, *, helpful: bool) -> None:
        self.feedback.append(helpful)


class _LifecycleChannel(_Channel):
    def __init__(self) -> None:
        super().__init__()
        self.contexts: list[ReplyContext] = []
        self.sessions: list[_LifecycleReplySession] = []

    async def begin_reply(
        self,
        source: IncomingMessage,
        *,
        max_content_chars: int,
        context: ReplyContext | None = None,
    ) -> _LifecycleReplySession:
        del source, max_content_chars
        assert context is not None
        session = _LifecycleReplySession()
        self.contexts.append(context)
        self.sessions.append(session)
        return session


class _InteractionReplySession(_LifecycleReplySession):
    def __init__(self) -> None:
        super().__init__()
        self.pause_calls = 0

    async def pause_for_interaction(self) -> str:
        self.pause_calls += 1
        self.state = ReplySessionState.AWAITING_INPUT
        return self.reply_message_id


class _InteractionChannel(_LifecycleChannel):
    async def begin_reply(
        self,
        source: IncomingMessage,
        *,
        max_content_chars: int,
        context: ReplyContext | None = None,
    ) -> _InteractionReplySession:
        del source, max_content_chars
        assert context is not None
        session = _InteractionReplySession()
        self.contexts.append(context)
        self.sessions.append(session)
        return session


def _interaction_delivery(
    *,
    revision: int,
    kind: Literal["form", "terminal"] = "form",
) -> ClaimedInteractionDelivery:
    common: dict[str, object] = {
        "delivery_id": f"delivery-{kind}-{revision}",
        "delivery_token": f"delivery-token-{kind}-{revision}",
        "action_id": "interaction-1",
        "revision": revision,
        "kind": kind,
        "presentation_ref": "reply-message-1",
        "expires_at": (datetime(2026, 8, 24, 12, tzinfo=UTC) + timedelta(minutes=5)).isoformat(),
        "safe_error_code": None,
    }
    if kind == "form":
        common.update(
            action_nonce=f"action-nonce-{revision:04d}",
            projection={
                "message": "Please provide the missing value.",
                "fields": [
                    {
                        "name": f"field-{revision}",
                        "kind": "text",
                        "label": "Name",
                        "required": True,
                        "options": [],
                        "min_length": 0,
                        "max_length": 100,
                        "minimum": None,
                        "maximum": None,
                    }
                ],
            },
        )
    else:
        common.update(
            action_nonce=None,
            projection={
                "state": "completed",
                "message": "Request completed.",
            },
        )
    return ClaimedInteractionDelivery.model_validate(common)


class _InteractionClient:
    def __init__(
        self,
        deliveries: dict[
            tuple[str | None, int | None],
            list[ClaimedInteractionDelivery],
        ]
        | None = None,
    ) -> None:
        self.deliveries = deliveries or {}
        self.claims: list[tuple[str, str | None, int | None]] = []
        self.acks: list[tuple[str, str, bool, str | None]] = []
        self.callbacks: list[ChannelFormAction] = []

    async def claim_interaction_delivery(
        self,
        *,
        owner: str,
        action_id: str | None = None,
        revision: int | None = None,
    ) -> ClaimedInteractionDelivery | None:
        self.claims.append((owner, action_id, revision))
        queued = self.deliveries.get((action_id, revision), [])
        return queued.pop(0) if queued else None

    async def acknowledge_interaction_delivery(
        self,
        *,
        delivery: ClaimedInteractionDelivery,
        owner: str,
        success: bool,
        safe_error_code: str | None = None,
    ) -> None:
        self.acks.append(
            (delivery.delivery_id, owner, success, safe_error_code),
        )

    async def receive_interaction_callback(
        self,
        action: ChannelFormAction,
    ) -> Literal["accepted", "duplicate"]:
        self.callbacks.append(action)
        return "accepted"


class _InteractionPresenter:
    def __init__(
        self,
        *,
        stop_event: asyncio.Event | None = None,
        stop_after: int = 0,
    ) -> None:
        self.deliveries: list[ClaimedInteractionDelivery] = []
        self.stop_event = stop_event
        self.stop_after = stop_after

    async def present(self, delivery: ClaimedInteractionDelivery) -> None:
        self.deliveries.append(delivery)
        if self.stop_event is not None and self.stop_after and len(self.deliveries) >= self.stop_after:
            self.stop_event.set()


def _form_action(*, revision: int = 1) -> ChannelFormAction:
    return ChannelFormAction(
        action_id="interaction-1",
        nonce=f"action-nonce-{revision:04d}",
        revision=revision,
        action="accept",
        form_value={f"field-{revision}": "Ada"},
        identity=IncomingIdentityAssertion(
            provider="feishu",
            provider_tenant_key="tenant-key",
            identifiers=(IncomingIdentityIdentifier(kind="open_id", value="ou-user"),),
        ),
        chat_id="oc-chat",
        message_id="reply-message-1",
        event_id=f"callback-{revision}",
    )


@pytest.mark.asyncio
async def test_interaction_pause_replaces_original_reply_card_then_acks_delivery() -> None:
    delivery = _interaction_delivery(revision=1)
    client = _InteractionClient({("interaction-1", 1): [delivery]})
    presenter = _InteractionPresenter()
    channel = _InteractionChannel()
    state = _StateStore()
    executor = _Executor(
        [
            InteractionRequiredEvent(
                action_id="interaction-1",
                revision=1,
                expires_at=datetime(2026, 8, 24, 12, tzinfo=UTC),
            )
        ]
    )
    bridge = _bridge(
        channel=channel,
        state=state,
        executor=executor,
        interaction_client=client,
        interaction_presenter=presenter,
    )

    await bridge.handle_message(_message())

    session = channel.sessions[0]
    assert isinstance(session, _InteractionReplySession)
    assert session.pause_calls == 1
    assert session.state is ReplySessionState.AWAITING_INPUT
    assert executor.calls[0]["presentation_ref"] == "reply-message-1"
    assert presenter.deliveries == [delivery]
    assert presenter.deliveries[0].presentation_ref == "reply-message-1"
    assert len(client.claims) == 1
    owner, action_id, revision = client.claims[0]
    assert owner
    assert (action_id, revision) == ("interaction-1", 1)
    assert client.acks == [(delivery.delivery_id, owner, True, None)]
    assert state.status == {"message-1": "replied"}


@pytest.mark.asyncio
async def test_form_callback_only_writes_durable_receipt_and_never_executes_mcp() -> None:
    client = _InteractionClient()
    presenter = _InteractionPresenter()
    executor = _Executor()
    bridge = _bridge(
        channel=_InteractionChannel(),
        state=_StateStore(),
        executor=executor,
        interaction_client=client,
        interaction_presenter=presenter,
    )
    action = _form_action()

    response = await bridge.handle_form_action(action)

    assert (response.toast_type, response.content) == (
        "success",
        "提交已收到，正在处理。",
    )
    assert client.callbacks == [action]
    assert client.claims == []
    assert presenter.deliveries == []
    assert executor.calls == []


@pytest.mark.asyncio
async def test_interaction_poll_delivers_next_round_then_terminal_page() -> None:
    next_round = _interaction_delivery(revision=2)
    terminal = _interaction_delivery(revision=2, kind="terminal")
    client = _InteractionClient(
        {("interaction-1", 2): [next_round, terminal]},
    )
    stop_event = asyncio.Event()
    presenter = _InteractionPresenter(stop_event=stop_event, stop_after=2)
    bridge = _bridge(
        channel=_InteractionChannel(),
        state=_StateStore(),
        executor=_Executor(),
        interaction_client=client,
        interaction_presenter=presenter,
    )
    await bridge.handle_form_action(_form_action(revision=1))

    await asyncio.wait_for(
        bridge.run_interaction_deliveries(stop_event),
        timeout=1,
    )

    assert presenter.deliveries == [next_round, terminal]
    claim_scopes = [(action_id, revision) for _, action_id, revision in client.claims]
    assert claim_scopes == [
        ("interaction-1", 1),
        ("interaction-1", 2),
        ("interaction-1", 2),
    ]
    assert [(delivery_id, success, code) for delivery_id, _, success, code in client.acks] == [
        (next_round.delivery_id, True, None),
        (terminal.delivery_id, True, None),
    ]


class _BlockingExecutor(_Executor):
    def __init__(self) -> None:
        super().__init__([])
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def stream(
        self,
        **kwargs: Any,
    ) -> AsyncIterator[MessageDeltaEvent | MessageCompletedEvent | ExecutionFailedEvent]:
        self.calls.append(kwargs)
        try:
            yield MessageDeltaEvent(content="partial", session_id="session-1")
            self.started.set()
            await self.release.wait()
            yield MessageCompletedEvent(session_id="session-1")
        finally:
            self.cancelled.set()


def _action(action_id: str, *, event_id: str = "action-event-1") -> ChannelAction:
    return ChannelAction(
        action_id=action_id,
        operator_id="ou-user",
        chat_id="oc-chat",
        message_id="reply-message-1",
        event_id=event_id,
    )


@pytest.mark.asyncio
async def test_prepared_followup_owns_queued_running_and_final_lifecycle() -> None:
    channel = _LifecycleChannel()
    state = _StateStore()
    bridge = _bridge(channel=channel, state=state, executor=_Executor())
    message = _message()

    bridge.message_queued(message, queue_position=2)
    await bridge.handle_message(message)

    context = channel.contexts[0]
    assert context.status is ReplyStatus.QUEUED
    assert context.queue_position == 2
    assert all(
        action_id and "ou-user" not in action_id and "oc-chat" not in action_id
        for action_id in (
            context.actions.cancel,
            context.actions.regenerate,
            context.actions.retry,
            context.actions.helpful,
            context.actions.unhelpful,
        )
    )
    assert channel.sessions[0].statuses == [ReplyStatus.RUNNING]
    assert channel.sessions[0].state is ReplySessionState.COMPLETED
    assert state.status == {"message-1": "replied"}


@pytest.mark.asyncio
async def test_cancel_action_stops_active_stream_once_and_tombstones_reply() -> None:
    channel = _LifecycleChannel()
    state = _StateStore()
    executor = _BlockingExecutor()
    bridge = _bridge(channel=channel, state=state, executor=executor)
    message = _message()

    bridge.message_queued(message, queue_position=0)
    running = asyncio.create_task(bridge.handle_message(message))
    await asyncio.wait_for(executor.started.wait(), timeout=1)
    cancel_id = channel.contexts[0].actions.cancel

    first = await bridge.handle_action(_action(cancel_id))
    duplicate = await bridge.handle_action(_action(cancel_id))
    await asyncio.wait_for(running, timeout=1)

    assert first.toast_type == "success"
    assert duplicate.content == "该操作已经处理。"
    assert executor.cancelled.is_set()
    assert channel.sessions[0].cancel_calls == 1
    assert channel.sessions[0].state is ReplySessionState.CANCELLED
    assert state.status == {"message-1": "replied"}


@pytest.mark.asyncio
async def test_cancelling_execution_and_its_caller_does_not_swallow_caller_cancellation() -> None:
    channel = _LifecycleChannel()
    state = _StateStore()
    executor = _BlockingExecutor()
    bridge = _bridge(channel=channel, state=state, executor=executor)
    message = _message()

    bridge.message_queued(message, queue_position=0)
    running = asyncio.create_task(bridge.handle_message(message))
    await asyncio.wait_for(executor.started.wait(), timeout=1)
    record = bridge._live_records[message.execution_id]
    execution = record.execution_task
    assert execution is not None

    # Reproduce the worker shutdown race in one event-loop turn: Bridge.close
    # cancels the child execution, then ChannelWorker.close cancels the
    # consumer awaiting handle_message. Python 3.12 otherwise lets the child
    # cancellation mask and consume the caller's own cancellation request.
    record.cancel_requested = True
    execution.cancel()
    running.cancel()

    with pytest.raises(asyncio.CancelledError):
        await running

    assert channel.sessions[0].state is ReplySessionState.CANCELLED
    assert state.status == {"message-1": "replied"}
    await bridge.close()


@pytest.mark.asyncio
async def test_regenerate_reenters_scheduler_with_fresh_execution_identity() -> None:
    channel = _LifecycleChannel()
    bridge = _bridge(channel=channel, state=_StateStore(), executor=_Executor())
    scheduled: list[IncomingMessage] = []
    identity = IncomingIdentityAssertion(
        provider="feishu",
        provider_tenant_key="tenant-sensitive",
        identifiers=(IncomingIdentityIdentifier(kind="open_id", value="open-sensitive"),),
    )

    async def schedule(message: IncomingMessage) -> None:
        scheduled.append(message)

    bridge.set_message_scheduler(schedule)
    await bridge.handle_message(_message(content="original question", identity=identity))
    regenerate_id = channel.contexts[0].actions.regenerate

    response = await bridge.handle_action(_action(regenerate_id, event_id="regenerate-event-1"))
    await asyncio.sleep(0)

    assert response.content == "已加入当前会话队列。"
    assert len(scheduled) == 1
    regenerated = scheduled[0]
    assert regenerated.message_id == "message-1"
    assert regenerated.event_id == "regenerate-event-1"
    assert regenerated.execution_id == "action:regenerate-event-1"
    assert regenerated.content == "original question"
    assert regenerated.identity is identity
    assert regenerated.operation == "regenerate"


@pytest.mark.asyncio
async def test_regenerate_rejects_a_superseded_card() -> None:
    channel = _LifecycleChannel()
    bridge = _bridge(channel=channel, state=_StateStore(), executor=_Executor())
    scheduled: list[IncomingMessage] = []

    async def schedule(message: IncomingMessage) -> None:
        scheduled.append(message)

    bridge.set_message_scheduler(schedule)
    await bridge.handle_message(_message(message_id="message-1", content="first"))
    old_regenerate_id = channel.contexts[0].actions.regenerate
    await bridge.handle_message(_message(message_id="message-2", content="follow-up"))

    response = await bridge.handle_action(_action(old_regenerate_id, event_id="old-card-event"))

    assert response.toast_type == "warning"
    assert response.content == "只能重新生成当前会话的最新回答。"
    assert scheduled == []


@pytest.mark.parametrize("failed_operation", ["message", "regenerate"], ids=["ordinary-message", "failed-regenerate"])
@pytest.mark.asyncio
async def test_retry_after_failed_execution_preserves_original_operation(
    failed_operation: Literal["message", "regenerate"],
) -> None:
    channel = _LifecycleChannel()
    executor = _Executor([ExecutionFailedEvent(error_code="TARGET_EXECUTION_FAILED")])
    bridge = _bridge(channel=channel, state=_StateStore(), executor=executor)
    scheduled: list[IncomingMessage] = []

    async def schedule(message: IncomingMessage) -> None:
        scheduled.append(message)

    bridge.set_message_scheduler(schedule)
    original = _message(content="failed question", operation=failed_operation)
    await bridge.handle_message(original)
    retry_id = channel.contexts[0].actions.retry

    response = await bridge.handle_action(_action(retry_id, event_id="retry-event"))
    assert any(task.get_name().startswith("channel-retry-") for task in asyncio.all_tasks())
    await asyncio.sleep(0)

    assert response.toast_type == "success"
    assert len(scheduled) == 1
    assert scheduled[0].operation == failed_operation
    assert scheduled[0].event_id == "retry-event"
    assert scheduled[0].execution_id == "action:retry-event"
    assert scheduled[0].message_id == original.message_id
    assert scheduled[0].content == original.content


@pytest.mark.asyncio
async def test_retry_reports_when_the_scheduler_is_unavailable() -> None:
    channel = _LifecycleChannel()
    executor = _Executor([ExecutionFailedEvent(error_code="TARGET_EXECUTION_FAILED")])
    bridge = _bridge(channel=channel, state=_StateStore(), executor=executor)

    await bridge.handle_message(_message(content="failed question"))
    retry_id = channel.contexts[0].actions.retry

    response = await bridge.handle_action(_action(retry_id, event_id="retry-event"))

    assert response.toast_type == "error"
    assert response.content == "重试暂时不可用。"


@pytest.mark.asyncio
async def test_retry_rejects_a_superseded_failed_request() -> None:
    channel = _LifecycleChannel()
    executor = _Executor([ExecutionFailedEvent(error_code="TARGET_EXECUTION_FAILED")])
    bridge = _bridge(channel=channel, state=_StateStore(), executor=executor)
    scheduled: list[IncomingMessage] = []

    async def schedule(message: IncomingMessage) -> None:
        scheduled.append(message)

    bridge.set_message_scheduler(schedule)
    await bridge.handle_message(_message(message_id="message-1", content="first failure"))
    old_retry_id = channel.contexts[0].actions.retry
    await bridge.handle_message(_message(message_id="message-2", content="latest failure"))

    response = await bridge.handle_action(_action(old_retry_id, event_id="old-retry-event"))

    assert response.toast_type == "warning"
    assert response.content == "只能重试当前会话的最新请求。"
    assert scheduled == []


@pytest.mark.asyncio
async def test_bridge_issues_and_registers_only_negotiated_actions() -> None:
    channel = _LifecycleChannel()
    capabilities = EffectiveReplyCapabilities(retry=True)
    executor = _Executor([ExecutionFailedEvent(error_code="TARGET_EXECUTION_FAILED")])
    bridge = _bridge(
        channel=channel,
        state=_StateStore(),
        executor=executor,
        capabilities=capabilities,
    )

    await bridge.handle_message(_message(content="failed question"))

    context = channel.contexts[0]
    assert context.capabilities == capabilities
    assert context.actions.retry
    assert context.actions.cancel == ""
    assert context.actions.regenerate == ""
    assert context.actions.helpful == ""
    assert context.actions.unhelpful == ""
    assert set(bridge._actions) == {context.actions.retry}

    forged = await bridge.handle_action(_action("forged-disabled-action"))
    assert forged.content == "操作已过期，请使用最新卡片。"


@pytest.mark.asyncio
async def test_feedback_is_operator_bound_and_idempotent() -> None:
    channel = _LifecycleChannel()
    bridge = _bridge(channel=channel, state=_StateStore(), executor=_Executor())
    await bridge.handle_message(_message())
    actions = channel.contexts[0].actions

    denied = await bridge.handle_action(
        ChannelAction(
            action_id=actions.helpful,
            operator_id="another-user",
            chat_id="oc-chat",
            message_id="reply-message-1",
            event_id="feedback-denied",
        )
    )
    wrong_card = await bridge.handle_action(
        ChannelAction(
            action_id=actions.helpful,
            operator_id="ou-user",
            chat_id="oc-chat",
            message_id="another-card",
            event_id="feedback-wrong-card",
        )
    )
    accepted = await bridge.handle_action(_action(actions.helpful))
    duplicate = await bridge.handle_action(_action(actions.unhelpful))
    await asyncio.sleep(0)

    assert denied.toast_type == "error"
    assert wrong_card.toast_type == "error"
    assert accepted.content == "感谢反馈。"
    assert duplicate.content == "该操作已经处理。"
    assert channel.sessions[0].feedback == [True]
