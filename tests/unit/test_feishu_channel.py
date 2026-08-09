import asyncio
import json
import threading
from types import SimpleNamespace
from typing import Any

import pytest

from api.channels.core.base import IncomingMessage, OutgoingMessage, ReplySessionState
from api.channels.feishu.channel import (
    FeishuAccount,
    FeishuChannel,
    FeishuSendError,
    _LarkOapiSDK,
)


class _Response:
    def __init__(self, *, ok: bool = True, code: int = 0) -> None:
        self.ok = ok
        self.code = code
        self.data = SimpleNamespace(
            card_id="card-1",
            message_id="reply-1",
            reaction_id="reaction-1",
        )

    def success(self) -> bool:
        return self.ok


class _BlockingWebSocket:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.stopped = threading.Event()
        self.loop: asyncio.AbstractEventLoop | None = None

    def start(self) -> None:
        self.started.set()
        assert self.loop is not None
        self.loop.run_forever()

    def stop(self) -> None:
        # Deliberately does not release start(). FeishuChannel must stop the
        # isolated event loop after the SDK disconnect step.
        self.stopped.set()

    def simulate_exit(self) -> None:
        assert self.loop is not None
        self.loop.call_soon_threadsafe(self.loop.stop)


class _NeverConnectedWebSocket(_BlockingWebSocket):
    def __init__(self) -> None:
        super().__init__()
        self._conn = None


class _FakeSDK:
    def __init__(self) -> None:
        self.websocket = _BlockingWebSocket()
        self.callback: Any = None
        self.bound_loop: asyncio.AbstractEventLoop | None = None
        self.replies: list[tuple[str, str]] = []
        self.reply_details: list[tuple[str, str, str, str | None]] = []
        self.creates: list[tuple[str, str]] = []
        self.cards: list[str] = []
        self.card_updates: list[tuple[str, str, int, str]] = []
        self.card_finishes: list[tuple[str, int, str]] = []
        self.reaction_creates: list[tuple[str, str]] = []
        self.reaction_deletes: list[tuple[str, str]] = []
        self.response = _Response()

    def build_rest_client(self, account: FeishuAccount) -> object:
        return object()

    def bind_ws_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self.bound_loop = loop
        self.websocket.loop = loop

    def build_ws_client(self, account: FeishuAccount, callback: Any) -> Any:
        self.callback = callback
        return self.websocket

    def reply_message(
        self,
        client: Any,
        message_id: str,
        content: str,
        *,
        message_type: str = "text",
        delivery_uuid: str | None = None,
    ) -> _Response:
        self.replies.append((message_id, content))
        self.reply_details.append((message_id, content, message_type, delivery_uuid))
        return self.response

    def create_message(self, client: Any, chat_id: str, content: str) -> _Response:
        self.creates.append((chat_id, content))
        return self.response

    def create_card(self, client: Any, card_json: str) -> _Response:
        self.cards.append(card_json)
        return self.response

    def update_card_text(
        self,
        client: Any,
        card_id: str,
        content: str,
        *,
        sequence: int,
        delivery_uuid: str,
    ) -> _Response:
        self.card_updates.append((card_id, content, sequence, delivery_uuid))
        return self.response

    def finish_streaming_card(
        self,
        client: Any,
        card_id: str,
        *,
        sequence: int,
        delivery_uuid: str,
    ) -> _Response:
        self.card_finishes.append((card_id, sequence, delivery_uuid))
        return self.response

    def create_reaction(self, client: Any, message_id: str, emoji_type: str) -> _Response:
        self.reaction_creates.append((message_id, emoji_type))
        return self.response

    def delete_reaction(self, client: Any, message_id: str, reaction_id: str) -> _Response:
        self.reaction_deletes.append((message_id, reaction_id))
        return self.response

    def stop_ws_client(self, client: Any) -> None:
        client.stop()

    def response_success(self, response: Any) -> bool:
        return bool(response.success())

    def response_code(self, response: Any) -> Any:
        return response.code


def _event(
    *,
    content: str = '{"text":"hello"}',
    sender_type: str = "user",
) -> Any:
    return SimpleNamespace(
        header=SimpleNamespace(event_id="evt-1", create_time="1720000000000"),
        event=SimpleNamespace(
            event_id="ignored-event-id",
            sender=SimpleNamespace(
                sender_type=sender_type,
                sender_id=SimpleNamespace(
                    open_id="ou-user",
                    union_id="on-user",
                    user_id="user-id",
                ),
            ),
            message=SimpleNamespace(
                chat_id="oc-chat",
                chat_type="p2p",
                message_id="om-message",
                message_type="text",
                create_time="1710000000000",
                content=content,
            ),
        ),
    )


def _channel(
    sdk: _FakeSDK | None = None,
    *,
    start_timeout_seconds: float = 1.0,
    stop_timeout_seconds: float = 1.0,
) -> tuple[FeishuChannel, _FakeSDK]:
    fake_sdk = sdk or _FakeSDK()
    account = FeishuAccount(
        account_id="bot-1",
        app_id="app-id",
        app_secret="app-secret",
    )
    return (
        FeishuChannel(
            account,
            sdk=fake_sdk,
            start_timeout_seconds=start_timeout_seconds,
            stop_timeout_seconds=stop_timeout_seconds,
        ),
        fake_sdk,
    )


def test_normalize_maps_feishu_message_envelope() -> None:
    channel, _ = _channel()

    message = channel._normalize(_event())

    assert message == IncomingMessage(
        channel="feishu",
        account_id="bot-1",
        chat_id="oc-chat",
        message_id="om-message",
        sender_id="ou-user",
        content="hello",
        message_type="text",
        chat_type="p2p",
        sender_type="user",
        event_id="evt-1",
        create_time="1720000000000",
        raw=None,
    )
    assert message.text == "hello"
    assert message.raw is None


def test_incoming_message_accepts_upstream_constructor_contract() -> None:
    raw = object()

    message = IncomingMessage(
        "feishu",
        "bot-1",
        "oc-chat",
        "p2p",
        "om-message",
        "ou-user",
        "hello",
        raw,
    )

    assert message.text == "hello"
    assert message.content == "hello"
    assert message.message_type == "text"
    assert message.raw is raw


def test_normalize_preserves_non_user_sender_type() -> None:
    channel, _ = _channel()

    message = channel._normalize(_event(sender_type="app"))

    assert message.sender_type == "app"


def test_sdk_domain_mapping_never_defaults_unknown_values_to_feishu() -> None:
    sdk = object.__new__(_LarkOapiSDK)
    sdk._lark = SimpleNamespace(FEISHU_DOMAIN="feishu-domain", LARK_DOMAIN="lark-domain")

    assert sdk._domain("feishu") == "feishu-domain"
    assert sdk._domain("lark") == "lark-domain"
    with pytest.raises(ValueError, match="domain"):
        sdk._domain("unknown")


def test_lark_sdk_builds_cardkit_reaction_and_idempotent_reply_requests() -> None:
    sdk = _LarkOapiSDK()
    captured: dict[str, Any] = {}

    def capture(name: str) -> Any:
        def call(request: Any) -> Any:
            captured[name] = request
            return request

        return call

    client = SimpleNamespace(
        im=SimpleNamespace(
            v1=SimpleNamespace(
                message=SimpleNamespace(reply=capture("reply")),
                message_reaction=SimpleNamespace(
                    create=capture("reaction_create"),
                    delete=capture("reaction_delete"),
                ),
            )
        ),
        cardkit=SimpleNamespace(
            v1=SimpleNamespace(
                card=SimpleNamespace(
                    create=capture("card_create"),
                    settings=capture("card_finish"),
                ),
                card_element=SimpleNamespace(content=capture("card_update")),
            )
        ),
    )

    sdk.reply_message(
        client,
        "om-message",
        '{"text":"answer"}',
        message_type="post",
        delivery_uuid="reply-uuid",
    )
    sdk.create_card(client, '{"schema":"2.0"}')
    sdk.update_card_text(
        client,
        "card-1",
        "answer",
        sequence=1,
        delivery_uuid="update-uuid",
    )
    sdk.finish_streaming_card(
        client,
        "card-1",
        sequence=2,
        delivery_uuid="finish-uuid",
    )
    sdk.create_reaction(client, "om-message", "Typing")
    sdk.delete_reaction(client, "om-message", "reaction-1")

    assert captured["reply"].body.msg_type == "post"
    assert captured["reply"].body.uuid == "reply-uuid"
    assert captured["card_create"].body.type == "card_json"
    assert captured["card_create"].body.data == '{"schema":"2.0"}'
    assert captured["card_update"].card_id == "card-1"
    assert captured["card_update"].element_id == "answer"
    assert captured["card_update"].body.sequence == 1
    assert captured["card_update"].body.uuid == "update-uuid"
    settings = json.loads(captured["card_finish"].body.settings)
    assert settings["config"]["streaming_mode"] is False
    assert captured["card_finish"].body.sequence == 2
    assert captured["reaction_create"].body.reaction_type.emoji_type == "Typing"
    assert captured["reaction_delete"].reaction_id == "reaction-1"


def test_lark_sdk_registers_read_receipt_as_intentionally_ignored() -> None:
    sdk = _LarkOapiSDK()
    dispatcher = sdk._lark.EventDispatcherHandler
    captured: dict[str, Any] = {}

    def client_factory(app_id: str, app_secret: str, **kwargs: Any) -> object:
        captured.update(app_id=app_id, app_secret=app_secret, **kwargs)
        return object()

    sdk._lark = SimpleNamespace(
        EventDispatcherHandler=dispatcher,
        FEISHU_DOMAIN="feishu-domain",
        LARK_DOMAIN="lark-domain",
        LogLevel=SimpleNamespace(WARNING="warning"),
        ws=SimpleNamespace(Client=client_factory),
    )
    account = FeishuAccount(
        account_id="bot-1",
        app_id="app-id",
        app_secret="app-secret",
    )

    sdk.build_ws_client(account, lambda data: None)

    processors = captured["event_handler"]._processorMap
    assert "p2.im.message.receive_v1" in processors
    assert "p2.im.message.message_read_v1" in processors


async def test_sdk_callback_schedules_handler_without_waiting() -> None:
    channel, _ = _channel()
    channel._loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()

    async def handle(message: IncomingMessage) -> None:
        assert message.message_id == "om-message"
        started.set()
        await release.wait()
        finished.set()

    channel.set_message_handler(handle)

    channel._on_message_receive(_event())

    await asyncio.wait_for(started.wait(), timeout=1)
    assert not finished.is_set()
    release.set()
    await asyncio.wait_for(finished.wait(), timeout=1)


async def test_send_replies_to_source_message_or_creates_chat_message() -> None:
    channel, sdk = _channel()

    await channel.send(
        OutgoingMessage(
            chat_id="oc-chat",
            content="回复内容",
            reply_to_message_id="om-message",
        )
    )
    await channel.send(OutgoingMessage(chat_id="oc-chat", content="主动消息"))

    assert sdk.replies == [("om-message", json.dumps({"text": "回复内容"}, ensure_ascii=False))]
    assert sdk.creates == [("oc-chat", json.dumps({"text": "主动消息"}, ensure_ascii=False))]


async def test_send_failure_raises_safe_error_code() -> None:
    channel, sdk = _channel()
    sdk.response = _Response(ok=False, code=230001)

    with pytest.raises(FeishuSendError) as raised:
        await channel.send(
            OutgoingMessage(
                chat_id="oc-chat",
                content="message body must not appear in the error",
                reply_to_message_id="om-message",
            )
        )

    assert raised.value.code == "230001"
    assert str(raised.value) == "Feishu send failed with code 230001"
    assert "message body" not in str(raised.value)


async def test_begin_reply_wires_cardkit_and_reaction_operations_through_sdk() -> None:
    channel, sdk = _channel()
    source = IncomingMessage(
        channel="feishu",
        account_id="bot-1",
        chat_id="oc-chat",
        chat_type="p2p",
        message_id="om-message",
        sender_id="ou-user",
        content="question",
        sender_type="user",
        event_id="evt-1",
    )

    session = await channel.begin_reply(source, max_content_chars=4000)
    await session.append("回答")
    await session.complete()

    assert session.state is ReplySessionState.COMPLETED
    assert sdk.reaction_creates == [("om-message", "Typing")]
    assert sdk.reaction_deletes == [("om-message", "reaction-1")]
    assert json.loads(sdk.cards[0])["config"]["streaming_mode"] is True
    message_id, content, message_type, message_uuid = sdk.reply_details[0]
    assert message_id == "om-message"
    assert message_type == "interactive"
    assert json.loads(content) == {"type": "card", "data": {"card_id": "card-1"}}
    assert message_uuid is not None
    assert sdk.card_updates[0][1] == "回答"
    assert sdk.card_updates[0][2] == 1
    assert sdk.card_finishes[0][1] == 2


async def test_start_and_stop_use_isolated_thread_with_bounded_join() -> None:
    channel, sdk = _channel(stop_timeout_seconds=1.0)

    await channel.start()

    assert await asyncio.to_thread(sdk.websocket.started.wait, 1)
    assert channel.is_running
    assert sdk.bound_loop is not asyncio.get_running_loop()

    await channel.stop()

    assert sdk.websocket.stopped.is_set()
    assert not channel.is_running


async def test_is_running_turns_false_when_websocket_thread_exits() -> None:
    channel, sdk = _channel(stop_timeout_seconds=1.0)
    await channel.start()
    assert channel.is_running

    # Simulate a connection/client crash after startup. The worker supervisor
    # observes this property and owns the process-level restart policy.
    sdk.websocket.simulate_exit()
    for _ in range(100):
        if not channel.is_running:
            break
        await asyncio.sleep(0.01)

    assert not channel.is_running
    await channel.stop()


async def test_start_fails_closed_when_sdk_never_connects() -> None:
    sdk = _FakeSDK()
    sdk.websocket = _NeverConnectedWebSocket()
    channel, _ = _channel(
        sdk,
        start_timeout_seconds=0.05,
        stop_timeout_seconds=0.5,
    )

    with pytest.raises(RuntimeError, match="did not connect in time"):
        await channel.start()

    assert sdk.websocket.stopped.is_set()
    assert not channel.is_running
