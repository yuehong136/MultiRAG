import asyncio
import hashlib
import json
import logging
import threading
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from typing import Any

import pytest

from api.channel_capabilities import EffectiveReplyCapabilities
from api.channels.core.base import (
    ChannelAction,
    ChannelActionResponse,
    ChannelFormAction,
    IncomingIdentityAssertion,
    IncomingIdentityIdentifier,
    IncomingMessage,
    OutgoingMessage,
    ReplyContext,
    ReplySessionState,
)
from api.channels.feishu.channel import (
    FeishuAccount,
    FeishuChannel,
    FeishuSendError,
    _LarkOapiSDK,
)
from api.channels.feishu.reply import FeishuReplyTransport


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
        self.action_callback: Any = None
        self.bound_loop: asyncio.AbstractEventLoop | None = None
        self.replies: list[tuple[str, str]] = []
        self.reply_details: list[tuple[str, str, str, str | None]] = []
        self.creates: list[tuple[str, str]] = []
        self.cards: list[str] = []
        self.card_message_updates: list[tuple[str, str]] = []
        self.card_updates: list[tuple[str, str, int, str]] = []
        self.card_batch_updates: list[tuple[str, list[dict[str, object]], int, str]] = []
        self.card_finishes: list[tuple[str, int, str]] = []
        self.reaction_creates: list[tuple[str, str]] = []
        self.reaction_deletes: list[tuple[str, str]] = []
        self.response = _Response()

    def build_rest_client(self, account: FeishuAccount) -> object:
        return object()

    def bind_ws_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self.bound_loop = loop
        self.websocket.loop = loop

    def build_ws_client(
        self,
        account: FeishuAccount,
        message_callback: Any,
        action_callback: Any,
    ) -> Any:
        self.callback = message_callback
        self.action_callback = action_callback
        return self.websocket

    def card_action_response(self, response: ChannelActionResponse) -> Any:
        return response

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

    def patch_card_message(
        self,
        client: Any,
        message_id: str,
        card_json: str,
    ) -> _Response:
        self.card_message_updates.append((message_id, card_json))
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

    def batch_update_card(
        self,
        client: Any,
        card_id: str,
        actions: list[dict[str, object]],
        *,
        sequence: int,
        delivery_uuid: str,
    ) -> _Response:
        self.card_batch_updates.append((card_id, actions, sequence, delivery_uuid))
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
    header_tenant_key: Any = "tenant-key",
    sender_tenant_key: Any = "tenant-key",
    app_id: Any = "app-id",
    open_id: Any = "ou-user",
    user_id: Any = "user-id",
    union_id: Any = "on-user",
) -> Any:
    return SimpleNamespace(
        header=SimpleNamespace(
            event_id="evt-1",
            create_time="1720000000000",
            tenant_key=header_tenant_key,
            app_id=app_id,
        ),
        event=SimpleNamespace(
            event_id="ignored-event-id",
            sender=SimpleNamespace(
                sender_type=sender_type,
                tenant_key=sender_tenant_key,
                sender_id=SimpleNamespace(
                    open_id=open_id,
                    union_id=union_id,
                    user_id=user_id,
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


def _card_action_event() -> Any:
    return SimpleNamespace(
        header=SimpleNamespace(event_id="card-event-1"),
        event=SimpleNamespace(
            operator=SimpleNamespace(
                open_id="ou-user",
                union_id="on-user",
                user_id="user-id",
            ),
            action=SimpleNamespace(value={"action_id": "opaque-action-1"}),
            context=SimpleNamespace(
                open_chat_id="oc-chat",
                open_message_id="om-bot-reply",
            ),
        ),
    )


def _form_action_event(
    *,
    form_value: Any = None,
    header_tenant_key: Any = "tenant-key",
    operator_tenant_key: Any = "tenant-key",
    app_id: Any = "app-id",
) -> Any:
    return SimpleNamespace(
        header=SimpleNamespace(
            event_id="form-event-1",
            tenant_key=header_tenant_key,
            app_id=app_id,
        ),
        event=SimpleNamespace(
            operator=SimpleNamespace(
                tenant_key=operator_tenant_key,
                open_id="ou-form-user",
                user_id="user-form",
                union_id="on-form-user",
            ),
            action=SimpleNamespace(
                tag="button",
                value={
                    "action_id": "opaque-form-action",
                    "nonce": "opaque-form-nonce",
                    "revision": 3,
                },
                form_value={
                    "f0_0": "2026-08-25",
                    "f0_1": ["annual", "paid"],
                    "f0_2": True,
                }
                if form_value is None
                else form_value,
            ),
            context=SimpleNamespace(
                open_chat_id="oc-form-chat",
                open_message_id="om-original-reply",
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


def test_channel_satisfies_full_reply_transport_protocol() -> None:
    channel, _ = _channel()

    assert isinstance(channel, FeishuReplyTransport)


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
        identity=IncomingIdentityAssertion(
            provider="feishu",
            provider_tenant_key="tenant-key",
            identifiers=(
                IncomingIdentityIdentifier(kind="open_id", value="ou-user"),
                IncomingIdentityIdentifier(kind="user_id", value="user-id"),
                IncomingIdentityIdentifier(kind="union_id", value="on-user"),
            ),
        ),
        message_type="text",
        chat_type="p2p",
        sender_type="user",
        event_id="evt-1",
        create_time="1720000000000",
        raw=None,
    )
    assert message.text == "hello"
    assert message.raw is None


def test_normalized_identity_is_frozen_and_redacts_provider_values() -> None:
    channel, _ = _channel()

    identity = channel._normalize(_event()).identity

    assert identity is not None
    rendered = repr(identity)
    assert "tenant-key" not in rendered
    assert "ou-user" not in rendered
    assert "user-id" not in rendered
    assert "on-user" not in rendered
    assert "app-id" not in rendered
    assert "ou-user" not in repr(identity.identifiers[0])
    with pytest.raises(FrozenInstanceError):
        identity.provider = "other"


@pytest.mark.parametrize(
    ("kind", "value"),
    [
        (None, "external-id"),
        (1, "external-id"),
        ("open_id", None),
        ("open_id", 1),
        (" open_id", "external-id"),
        ("open_id", " external-id"),
    ],
)
def test_incoming_identity_identifier_rejects_malformed_values(kind: Any, value: Any) -> None:
    with pytest.raises(ValueError, match="identifier is invalid"):
        IncomingIdentityIdentifier(kind=kind, value=value)


@pytest.mark.parametrize(
    ("provider", "tenant_key", "identifiers"),
    [
        (None, "tenant-key", (IncomingIdentityIdentifier("open_id", "external-id"),)),
        (1, "tenant-key", (IncomingIdentityIdentifier("open_id", "external-id"),)),
        ("feishu", 1, (IncomingIdentityIdentifier("open_id", "external-id"),)),
        ("feishu", "tenant-key", [IncomingIdentityIdentifier("open_id", "external-id")]),
        ("feishu", "tenant-key", (object(),)),
    ],
)
def test_incoming_identity_assertion_rejects_malformed_types(
    provider: Any,
    tenant_key: Any,
    identifiers: Any,
) -> None:
    with pytest.raises(ValueError, match="assertion is invalid"):
        IncomingIdentityAssertion(
            provider=provider,
            provider_tenant_key=tenant_key,
            identifiers=identifiers,
        )


def test_incoming_identity_assertion_rejects_duplicate_identifier_kinds() -> None:
    with pytest.raises(ValueError, match="identifier kinds must be unique"):
        IncomingIdentityAssertion(
            provider="feishu",
            provider_tenant_key="tenant-key",
            identifiers=(
                IncomingIdentityIdentifier("open_id", "external-id-1"),
                IncomingIdentityIdentifier("open_id", "external-id-2"),
            ),
        )


@pytest.mark.parametrize("tenant_key", [None, "", " tenant-key"])
def test_normalize_rejects_missing_or_malformed_header_tenant(tenant_key: Any) -> None:
    channel, _ = _channel()

    with pytest.raises(ValueError, match="identity field"):
        channel._normalize(_event(header_tenant_key=tenant_key))


@pytest.mark.parametrize("open_id", [None, "", " ou-user"])
def test_normalize_requires_canonical_open_id(open_id: Any) -> None:
    channel, _ = _channel()

    with pytest.raises(ValueError, match="identity field"):
        channel._normalize(_event(open_id=open_id))


def test_normalize_rejects_sender_tenant_conflict() -> None:
    channel, _ = _channel()

    with pytest.raises(ValueError, match="sender tenant"):
        channel._normalize(_event(sender_tenant_key="other-tenant"))


def test_normalize_rejects_event_app_conflict_without_propagating_app_id() -> None:
    channel, _ = _channel()

    with pytest.raises(ValueError, match="event app"):
        channel._normalize(_event(app_id="other-app"))

    identity = channel._normalize(_event()).identity
    assert identity is not None
    assert not hasattr(identity, "app_id")


def test_normalize_accepts_absent_optional_sender_identity_fields() -> None:
    channel, _ = _channel()

    message = channel._normalize(
        _event(
            sender_tenant_key=None,
            app_id=None,
            user_id=None,
            union_id=None,
        )
    )

    assert message.identity == IncomingIdentityAssertion(
        provider="feishu",
        provider_tenant_key="tenant-key",
        identifiers=(IncomingIdentityIdentifier(kind="open_id", value="ou-user"),),
    )
    assert message.sender_id == "ou-user"
    assert message.raw is None


def test_message_callback_logs_identity_structure_without_identity_values(caplog: pytest.LogCaptureFixture) -> None:
    channel, _ = _channel()
    identity_values = (
        "tenant-secret-sentinel",
        "open-secret-sentinel",
        "user-secret-sentinel",
        "union-secret-sentinel",
    )

    with caplog.at_level(logging.INFO, logger="api.channels.feishu.channel"):
        channel._on_message_receive(
            _event(
                header_tenant_key=identity_values[0],
                sender_tenant_key=identity_values[0],
                open_id=identity_values[1],
                user_id=identity_values[2],
                union_id=identity_values[3],
            )
        )

    assert "channel_event=identity_normalized" in caplog.text
    assert "identity_present=true" in caplog.text
    assert "tenant_key_present=true" in caplog.text
    assert "identifier_kinds=open_id,user_id,union_id" in caplog.text
    assert "identifier_count=3" in caplog.text
    assert "app-id" not in caplog.text
    for value in identity_values:
        assert value not in caplog.text
        assert hashlib.sha256(value.encode()).hexdigest()[:16] not in caplog.text


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
    assert message.identity is None


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
                message=SimpleNamespace(
                    reply=capture("reply"),
                    patch=capture("message_patch"),
                ),
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
                    batch_update=capture("card_batch_update"),
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
    sdk.patch_card_message(client, "om-reply", '{"schema":"2.0"}')
    sdk.update_card_text(
        client,
        "card-1",
        "answer",
        sequence=1,
        delivery_uuid="update-uuid",
    )
    sdk.batch_update_card(
        client,
        "card-1",
        [{"action": "delete_elements", "params": {"element_ids": ["old"]}}],
        sequence=2,
        delivery_uuid="batch-uuid",
    )
    sdk.finish_streaming_card(
        client,
        "card-1",
        sequence=3,
        delivery_uuid="finish-uuid",
    )
    sdk.create_reaction(client, "om-message", "Typing")
    sdk.delete_reaction(client, "om-message", "reaction-1")

    assert captured["reply"].body.msg_type == "post"
    assert captured["reply"].body.uuid == "reply-uuid"
    assert captured["card_create"].body.type == "card_json"
    assert captured["card_create"].body.data == '{"schema":"2.0"}'
    assert captured["message_patch"].message_id == "om-reply"
    assert captured["message_patch"].body.content == '{"schema":"2.0"}'
    assert captured["card_update"].card_id == "card-1"
    assert captured["card_update"].element_id == "answer"
    assert captured["card_update"].body.sequence == 1
    assert captured["card_update"].body.uuid == "update-uuid"
    assert json.loads(captured["card_batch_update"].body.actions)[0]["action"] == "delete_elements"
    assert captured["card_batch_update"].body.sequence == 2
    assert captured["card_batch_update"].body.uuid == "batch-uuid"
    settings = json.loads(captured["card_finish"].body.settings)
    assert settings["config"]["streaming_mode"] is False
    assert captured["card_finish"].body.sequence == 3
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

    sdk.build_ws_client(account, lambda data: None, lambda data: None)

    processors = captured["event_handler"]._processorMap
    assert "p2.im.message.receive_v1" in processors
    assert "p2.im.message.message_read_v1" in processors
    assert "p2.im.message.reaction.created_v1" in processors
    assert "p2.im.message.reaction.deleted_v1" in processors
    assert "p2.im.chat.access_event.bot_p2p_chat_entered_v1" in processors
    assert "p2.card.action.trigger" in captured["event_handler"]._callback_processor_map


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


async def test_card_action_callback_normalizes_and_acknowledges_within_ws_thread() -> None:
    channel, _ = _channel()
    channel._loop = asyncio.get_running_loop()
    received: list[ChannelAction] = []

    async def handle(action: ChannelAction) -> ChannelActionResponse:
        received.append(action)
        return ChannelActionResponse("success", "accepted")

    channel.set_action_handler(handle)
    response = await asyncio.to_thread(channel._on_card_action, _card_action_event())

    assert response == ChannelActionResponse("success", "accepted")
    assert received == [
        ChannelAction(
            action_id="opaque-action-1",
            operator_id="ou-user",
            chat_id="oc-chat",
            message_id="om-bot-reply",
            event_id="card-event-1",
        )
    ]


async def test_form_callback_uses_separate_typed_handler_and_bounded_values() -> None:
    channel, sdk = _channel()
    channel._loop = asyncio.get_running_loop()
    low_risk_received: list[ChannelAction] = []
    forms_received: list[ChannelFormAction] = []

    async def handle_low_risk(action: ChannelAction) -> ChannelActionResponse:
        low_risk_received.append(action)
        return ChannelActionResponse("error", "wrong handler")

    async def handle_form(action: ChannelFormAction) -> ChannelActionResponse:
        forms_received.append(action)
        return ChannelActionResponse("success", "已接收")

    channel.set_action_handler(handle_low_risk)
    channel.set_form_action_handler(handle_form)
    response = await asyncio.to_thread(channel._on_card_action, _form_action_event())

    assert response == ChannelActionResponse("success", "已接收")
    assert low_risk_received == []
    assert len(forms_received) == 1
    action = forms_received[0]
    assert action.action_id == "opaque-form-action"
    assert action.nonce == "opaque-form-nonce"
    assert action.revision == 3
    assert dict(action.form_value) == {
        "f0_0": "2026-08-25",
        "f0_1": ("annual", "paid"),
        "f0_2": True,
    }
    assert action.identity == IncomingIdentityAssertion(
        provider="feishu",
        provider_tenant_key="tenant-key",
        identifiers=(
            IncomingIdentityIdentifier(kind="open_id", value="ou-form-user"),
            IncomingIdentityIdentifier(kind="user_id", value="user-form"),
            IncomingIdentityIdentifier(kind="union_id", value="on-form-user"),
        ),
    )
    assert (action.chat_id, action.message_id, action.event_id) == (
        "oc-form-chat",
        "om-original-reply",
        "form-event-1",
    )
    with pytest.raises(TypeError):
        action.form_value["f0_0"] = "tampered"  # type: ignore[index]
    rendered = repr(action)
    assert "2026-08-25" not in rendered
    assert "opaque-form-nonce" not in rendered
    assert sdk.cards == []
    assert sdk.card_message_updates == []


@pytest.mark.parametrize(
    ("header_tenant_key", "operator_tenant_key", "app_id"),
    [
        ("tenant-key", "other-tenant", "app-id"),
        ("tenant-key", "tenant-key", "other-app"),
        (None, "tenant-key", "app-id"),
    ],
)
def test_form_callback_rejects_broken_operator_and_header_lineage(
    header_tenant_key: Any,
    operator_tenant_key: Any,
    app_id: Any,
) -> None:
    channel, _ = _channel()

    with pytest.raises(ValueError):
        channel._normalize_card_action(
            _form_action_event(
                header_tenant_key=header_tenant_key,
                operator_tenant_key=operator_tenant_key,
                app_id=app_id,
            )
        )


@pytest.mark.parametrize(
    "form_value",
    [
        {"nested": {"unsafe": "value"}},
        {"too_many": ["choice"] * 33},
        {"oversized": "x" * 4097},
    ],
)
def test_form_callback_rejects_unbounded_or_nested_form_values(
    form_value: dict[str, object],
) -> None:
    channel, _ = _channel()

    with pytest.raises(ValueError):
        channel._normalize_card_action(_form_action_event(form_value=form_value))


def test_form_callback_waits_at_most_two_and_a_half_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel, _ = _channel()
    loop = asyncio.new_event_loop()
    channel._loop = loop
    timeouts: list[float | None] = []

    class _ImmediateFuture:
        def result(self, timeout: float | None = None) -> ChannelActionResponse:
            timeouts.append(timeout)
            return ChannelActionResponse("success", "queued")

    def submit(coroutine: Any, target_loop: asyncio.AbstractEventLoop) -> _ImmediateFuture:
        assert target_loop is loop
        coroutine.close()
        return _ImmediateFuture()

    monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", submit)
    try:
        response = channel._on_card_action(_form_action_event())
    finally:
        loop.close()

    assert response == ChannelActionResponse("success", "queued")
    assert timeouts == [2.5]


async def test_whole_card_update_targets_original_reply_message() -> None:
    channel, sdk = _channel()

    await channel.update_card("om-original-reply", '{"schema":"2.0"}')

    assert sdk.card_message_updates == [
        ("om-original-reply", '{"schema":"2.0"}'),
    ]


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
    assert sdk.card_batch_updates[0][2] == 3


async def test_begin_reply_uses_buffered_delivery_when_progression_is_not_negotiated() -> None:
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
    context = ReplyContext(capabilities=EffectiveReplyCapabilities())

    session = await channel.begin_reply(source, max_content_chars=4000, context=context)
    await session.append("完整回答")
    await session.complete()

    assert session.state is ReplySessionState.COMPLETED
    assert sdk.cards == []
    assert sdk.card_updates == []
    assert sdk.reaction_creates == []
    assert sdk.replies == [("om-message", json.dumps({"text": "完整回答"}, ensure_ascii=False))]


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
