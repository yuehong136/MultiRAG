"""DingTalk transport behaviour that does not need a live tenant.

The websocket handshake cannot be exercised here. Everything after it can:
payload normalisation, the ack, the reply-target cache, and the worker
descriptor that builds the transport out of binding state.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from api.channel_runtime.schemas import RuntimeCredential
from api.channels.dingtalk.channel import DingTalkAccount, DingTalkChannel, _with_ticket
from api.channels.dingtalk.provider import WORKER_PROVIDER
from api.channels.provider import ChannelWorkerError
from common.app_config import AppConfig


def _channel() -> DingTalkChannel:
    return DingTalkChannel(
        DingTalkAccount(
            account_id="account-aaaa",
            client_id="dingaaaaaaaaaaaaaaaa",
            client_secret="secret-aaaa-bbbb-cccc",
        )
    )


def _callback(**overrides: Any) -> str:
    body = {
        "conversationId": "cid-aaaa",
        "conversationType": "1",
        "senderStaffId": "user-aaaa",
        "msgId": "msg-aaaa",
        "text": {"content": "hello"},
        "sessionWebhook": "https://oapi.dingtalk.com/robot/sendBySession?session=aaaa",
    }
    body.update(overrides)
    return json.dumps({"headers": {"messageId": "callback-aaaa"}, "data": json.dumps(body)})


@pytest.mark.asyncio
async def test_a_callback_becomes_one_normalized_message() -> None:
    channel = _channel()
    seen: list[Any] = []
    channel.set_message_handler(lambda message: _record(seen, message))

    await channel._handle_payload(_callback())

    assert len(seen) == 1
    message = seen[0]
    assert message.channel == "dingtalk"
    assert message.chat_id == "cid-aaaa"
    assert message.sender_id == "user-aaaa"
    assert message.content == "hello"
    # Normalised into the vocabulary the bridge's policy check speaks, so the
    # private-chat-only toggle works the same on every provider.
    assert message.chat_type == "p2p"
    assert message.sender_type == "user"


@pytest.mark.asyncio
async def test_a_group_conversation_is_labelled_as_one() -> None:
    channel = _channel()
    seen: list[Any] = []
    channel.set_message_handler(lambda message: _record(seen, message))

    await channel._handle_payload(_callback(conversationType="2"))

    assert seen[0].chat_type == "group"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"text": {"content": "   "}},
        {"senderStaffId": "", "senderId": "", "userId": ""},
        {"conversationId": "", "openConversationId": "", "chatId": ""},
    ],
    ids=["blank-text", "no-sender", "no-conversation"],
)
async def test_an_unanswerable_callback_is_dropped_but_still_acked(overrides: dict[str, Any]) -> None:
    """An unacked callback is redelivered forever.

    Dropping a message we cannot answer without acking it would turn one
    unusable payload into an endless redelivery loop, which is worse than the
    lost reply it was trying to avoid.
    """

    channel = _channel()
    seen: list[Any] = []
    channel.set_message_handler(lambda message: _record(seen, message))
    acked: list[dict[str, Any]] = []
    channel._ws = _FakeWebSocket(acked)

    await channel._handle_payload(_callback(**overrides))

    assert seen == []
    assert acked == [{"messageId": "callback-aaaa", "response": {"success": True}}]


@pytest.mark.asyncio
async def test_the_reply_target_comes_from_the_message_not_from_a_chat_id() -> None:
    """A stream bot can only answer where it was spoken to."""

    channel = _channel()
    channel.set_message_handler(lambda message: _record([], message))

    # Nothing heard yet: a reply has nowhere to go and must not raise.
    await channel.send(_outgoing("cid-aaaa", "answer"))

    await channel._handle_payload(_callback())
    assert channel._session_webhooks["cid-aaaa"].startswith("https://oapi.dingtalk.com/")


@pytest.mark.asyncio
async def test_a_malformed_envelope_never_reaches_the_handler() -> None:
    channel = _channel()
    seen: list[Any] = []
    channel.set_message_handler(lambda message: _record(seen, message))

    await channel._handle_payload("")
    await channel._handle_payload(json.dumps(["not", "an", "object"]))
    await channel._handle_payload(json.dumps({"data": json.dumps(["still", "not"])}))

    assert seen == []


def test_the_ticket_survives_an_endpoint_that_already_has_a_query() -> None:
    assert _with_ticket("wss://host/connect", "t-aaaa") == "wss://host/connect?ticket=t-aaaa"
    # The endpoint DingTalk returns has been observed carrying its own query;
    # rebuilding the URL naively would drop it.
    assert _with_ticket("wss://host/connect?a=1", "t-aaaa") == "wss://host/connect?a=1&ticket=t-aaaa"
    # An endpoint that already carries a ticket keeps its own.
    assert _with_ticket("wss://host/connect?ticket=own", "t-aaaa") == "wss://host/connect?ticket=own"


def test_the_worker_descriptor_reads_only_the_generic_credential() -> None:
    """Written after CHN-P8, so it has no legacy pair to fall back to.

    The legacy `app_id`/`app_secret` fields are Feishu's names and are being
    deleted in CHN-P11; a second provider reaching for them would resurrect the
    very coupling the generic map exists to remove.
    """

    plan = WORKER_PROVIDER.build_managed(
        credential=RuntimeCredential.model_validate(
            {
                "app_id": "cli_unused",
                "app_secret": "unused-aaaa",
                "fields": {"client_id": "dingaaaaaaaaaaaaaaaa", "client_secret": "secret-aaaa-bbbb-cccc"},
            }
        ),
        public_config={"robot_code": "robot-aaaa", "allowed_user_ids": ["user_a"]},
    )

    assert plan.account_id == "dingaaaaaaaaaaaaaaaa"
    assert plan.allowed_sender_ids == frozenset({"user_a"})
    assert isinstance(plan.channel, DingTalkChannel)

    # The legacy pair alone is not a DingTalk credential, and silently using it
    # would connect as whatever Feishu app happened to be in that field.
    with pytest.raises(ChannelWorkerError):
        WORKER_PROVIDER.build_managed(
            credential=RuntimeCredential.model_validate({"app_id": "cli_unused", "app_secret": "unused-aaaa"}),
            public_config={"robot_code": "robot-aaaa"},
        )


def test_a_malformed_allowlist_fails_closed() -> None:
    credential = RuntimeCredential.model_validate(
        {
            "app_id": "cli_unused",
            "app_secret": "unused-aaaa",
            "fields": {"client_id": "dingaaaaaaaaaaaaaaaa", "client_secret": "secret-aaaa-bbbb-cccc"},
        }
    )
    for allowlist in ("user_a", [""], [1]):
        with pytest.raises(ChannelWorkerError):
            WORKER_PROVIDER.build_managed(credential=credential, public_config={"allowed_user_ids": allowlist})


def test_tuning_comes_from_this_providers_own_config_section() -> None:
    tuning = WORKER_PROVIDER.tuning(AppConfig())

    # Renewing after the lease expires would let another runner take a lease
    # this one still believes it holds.
    assert tuning.leader_renew_seconds < tuning.leader_ttl_seconds
    assert tuning.queue_size > 0
    assert tuning.worker_concurrency > 0


def _outgoing(chat_id: str, content: str) -> Any:
    from api.channels.core.base import OutgoingMessage

    return OutgoingMessage(chat_id=chat_id, content=content)


async def _record(sink: list[Any], message: Any) -> None:
    sink.append(message)


class _FakeWebSocket:
    closed = False

    def __init__(self, sink: list[dict[str, Any]]) -> None:
        self._sink = sink

    async def send_json(self, payload: dict[str, Any]) -> None:
        self._sink.append(payload)
