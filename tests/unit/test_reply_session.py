"""Contract tests for the provider-neutral buffered reply lifecycle."""

from __future__ import annotations

import pytest

from api.channels.core.base import (
    Channel,
    IncomingMessage,
    OutgoingMessage,
    ReplySession,
    ReplySessionState,
    ReplySessionStateError,
)
from api.channels.core.reply import SERVICE_UNAVAILABLE_TEXT, truncate_answer


class _Channel(Channel):
    channel_id = "plain"
    account_id = "account-1"

    def __init__(self, *, send_failure: bool = False) -> None:
        super().__init__()
        self.sent: list[OutgoingMessage] = []
        self.send_calls = 0
        self._send_failure = send_failure

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def send(self, message: OutgoingMessage) -> None:
        self.send_calls += 1
        if self._send_failure:
            raise RuntimeError("provider send failed")
        self.sent.append(message)


def _source() -> IncomingMessage:
    return IncomingMessage(
        channel="plain",
        account_id="account-1",
        chat_id="chat-1",
        chat_type="p2p",
        message_id="message-1",
        sender_id="sender-1",
        content="question",
        sender_type="user",
    )


@pytest.mark.asyncio
async def test_buffered_reply_session_preserves_append_order_and_sends_once_on_complete() -> None:
    channel = _Channel()
    session = await channel.begin_reply(_source(), max_content_chars=100)

    assert isinstance(session, ReplySession)
    assert session.state is ReplySessionState.OPEN
    await session.append("first ")
    await session.append("second")
    assert channel.sent == []

    await session.complete()

    assert session.state is ReplySessionState.COMPLETED
    assert channel.sent == [
        OutgoingMessage(
            chat_id="chat-1",
            content="first second",
            reply_to_message_id="message-1",
        )
    ]


@pytest.mark.asyncio
async def test_buffered_reply_session_replaces_deltas_with_authoritative_snapshot() -> None:
    channel = _Channel()
    session = await channel.begin_reply(_source(), max_content_chars=100)

    await session.append("raw answer")
    await session.replace("raw ##0$$ answer")
    await session.complete()

    assert channel.sent == [
        OutgoingMessage(
            chat_id="chat-1",
            content="raw ##0$$ answer",
            reply_to_message_id="message-1",
        )
    ]


@pytest.mark.asyncio
async def test_buffered_reply_session_applies_the_limit_across_multiple_appends() -> None:
    channel = _Channel()
    session = await channel.begin_reply(_source(), max_content_chars=50)
    answer = "a" * 30 + "b" * 30

    await session.append(answer[:20])
    await session.append(answer[20:40])
    await session.append(answer[40:])
    await session.complete()

    assert channel.sent[0].content == truncate_answer(answer, 50)


@pytest.mark.parametrize("operation", ["append", "replace", "complete", "fail"])
@pytest.mark.asyncio
async def test_buffered_reply_session_rejects_every_transition_after_complete(operation: str) -> None:
    channel = _Channel()
    session = await channel.begin_reply(_source(), max_content_chars=100)
    await session.append("answer")
    await session.complete()

    with pytest.raises(ReplySessionStateError):
        if operation == "append":
            await session.append("late")
        elif operation == "replace":
            await session.replace("late")
        elif operation == "complete":
            await session.complete()
        else:
            await session.fail("LATE_FAILURE")

    assert channel.send_calls == 1


@pytest.mark.asyncio
async def test_buffered_reply_session_failure_discards_partial_content_and_is_terminal() -> None:
    channel = _Channel()
    session = await channel.begin_reply(_source(), max_content_chars=100)
    await session.append("partial answer that must not be delivered")

    await session.fail("TARGET_EXECUTION_FAILED")

    assert session.state is ReplySessionState.FAILED
    assert channel.sent == [
        OutgoingMessage(
            chat_id="chat-1",
            content=SERVICE_UNAVAILABLE_TEXT,
            reply_to_message_id="message-1",
        )
    ]
    with pytest.raises(ReplySessionStateError):
        await session.append("late")
    with pytest.raises(ReplySessionStateError):
        await session.fail("DOUBLE_FAILURE")
    with pytest.raises(ReplySessionStateError):
        await session.complete()
    assert channel.send_calls == 1


@pytest.mark.parametrize(
    ("operation", "expected_state"),
    [("complete", ReplySessionState.COMPLETED), ("fail", ReplySessionState.FAILED)],
)
@pytest.mark.asyncio
async def test_buffered_reply_session_send_failure_propagates_and_keeps_a_terminal_state(
    operation: str,
    expected_state: ReplySessionState,
) -> None:
    channel = _Channel(send_failure=True)
    session = await channel.begin_reply(_source(), max_content_chars=100)
    await session.append("answer")

    with pytest.raises(RuntimeError, match="provider send failed"):
        if operation == "complete":
            await session.complete()
        else:
            await session.fail("TARGET_EXECUTION_FAILED")

    assert session.state is expected_state
    assert channel.send_calls == 1
    with pytest.raises(ReplySessionStateError):
        await session.append("late")
