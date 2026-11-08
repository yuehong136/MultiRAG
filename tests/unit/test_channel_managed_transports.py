"""Managed text transports preserve failure, scope and lifecycle contracts."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
from typing import Any

import discord
import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from api.channel_control.callback_validation import CallbackRejected, line_messages, wecom_decrypt, wecom_messages
from api.channel_control.dependencies import get_runtime_control_service
from api.channel_control.service import ChannelAccessDenied, RuntimeBindingSpec
from api.channel_providers import provider_names, provider_spec
from api.channel_providers.functions import split_config, validate_config
from api.channel_runtime.callbacks import CallbackBatch, CallbackMessage, CallbackReceipt
from api.channel_runtime.schemas import RuntimeCredential
from api.channels.core.base import IncomingMessage, MessageAdmission, OutgoingMessage
from api.channels.discord.channel import DiscordChannel
from api.channels.line.channel import LineChannel
from api.channels.provider import ChannelWorkerError, worker_provider
from api.channels.runtime_client import ChannelRuntimeClient, ChannelRuntimeClientError
from api.channels.telegram.channel import TelegramChannel
from api.channels.wecom.channel import WeComChannel

WECOM_CREDENTIAL = {"corp_id": "corp-test", "agent_id": "1001", "corp_secret": "secret-test", "callback_token": "callback-test", "aes_key": base64.b64encode(b"a" * 32).decode().rstrip("=")}


def encrypted_wecom(message: bytes, *, corp: str = "corp-test", padding: int | None = None) -> tuple[str, dict[str, str]]:
    plaintext = b"r" * 16 + len(message).to_bytes(4, "big") + message + corp.encode()
    pad = 32 - len(plaintext) % 32
    padded = plaintext + bytes([pad if padding is None else padding]) * pad
    encryptor = Cipher(algorithms.AES(b"a" * 32), modes.CBC(b"a" * 16)).encryptor()
    encrypted = base64.b64encode(encryptor.update(padded) + encryptor.finalize()).decode()
    query = {"timestamp": str(int(time.time())), "nonce": "nonce-test"}
    query["msg_signature"] = hashlib.sha1("".join(sorted([WECOM_CREDENTIAL["callback_token"], query["timestamp"], query["nonce"], encrypted])).encode()).hexdigest()
    return encrypted, query


def test_registry_only_publishes_real_managed_providers() -> None:
    assert set(provider_names()) == {"feishu", "dingtalk", "discord", "telegram", "line", "wecom"}
    for provider in ("discord", "telegram", "line", "wecom"):
        assert worker_provider(provider).name == provider
        assert provider_spec(provider).capabilities.progressive_reply is False


@pytest.mark.parametrize(
    "provider,credential",
    [
        ("discord", {"bot_id": "123", "bot_token": "bot-test"}),
        ("telegram", {"bot_id": "123", "bot_token": "bot-test"}),
        ("line", {"channel_id": "123", "channel_secret": "channel-test", "access_token": "access-test"}),
        ("wecom", WECOM_CREDENTIAL),
    ],
)
def test_config_secrets_are_removed_and_whole_credential_is_required(provider: str, credential: dict[str, str]) -> None:
    spec = provider_spec(provider)
    config = validate_config(spec, {"credential": credential})
    public, secret = split_config(spec, config)
    assert secret is not None
    for path in spec.secret_paths:
        key = path.rsplit(".", 1)[-1]
        assert key not in public["credential"]
        assert secret[key] == credential[key]
    plan = worker_provider(provider).build_managed(credential=RuntimeCredential(fields=credential), public_config=public)
    assert plan.channel.channel_id == provider
    with pytest.raises(ChannelWorkerError, match="CHANNEL_RUNTIME_CONFIG_INVALID"):
        worker_provider(provider).build_managed(credential=RuntimeCredential(fields={}), public_config=public)


def test_wecom_account_identity_includes_corp_and_app() -> None:
    spec = provider_spec("wecom")
    identity = spec.account_identity({"credential": {"corp_id": "one", "agent_id": "1001"}})
    assert identity != spec.account_identity({"credential": {"corp_id": "two", "agent_id": "1001"}})
    assert identity != spec.account_identity({"credential": {"corp_id": "one", "agent_id": "1002"}})


def test_line_checks_exact_body_signature_and_group_scope() -> None:
    body = json.dumps({"events": [{"type": "message", "message": {"type": "text", "id": "m-1", "text": "question"}, "source": {"type": "group", "groupId": "g-1", "userId": "u-1"}}]}).encode()
    signature = base64.b64encode(hmac.digest(b"channel-test", body, "sha256")).decode()
    messages = line_messages(body, signature, {"channel_secret": "channel-test"})
    assert messages[0].chat_type == "group" and messages[0].chat_id == "g-1" and messages[0].sender_id == "u-1"
    with pytest.raises(CallbackRejected, match="SIGNATURE_INVALID"):
        line_messages(body + b" ", signature, {"channel_secret": "channel-test"})


def test_wecom_verified_decryption_rejects_bad_corp_padding_and_signature() -> None:
    encrypted, query = encrypted_wecom(b"verified-echo")
    assert wecom_decrypt(encrypted, query, WECOM_CREDENTIAL) == b"verified-echo"
    with pytest.raises(CallbackRejected):
        wecom_decrypt(encrypted, {**query, "msg_signature": "bad"}, WECOM_CREDENTIAL)
    for values in ({"corp": "other-corp"}, {"padding": 0}):
        encrypted, query = encrypted_wecom(b"text", **values)
        with pytest.raises(CallbackRejected):
            wecom_decrypt(encrypted, query, WECOM_CREDENTIAL)


def test_wecom_message_checks_agent_and_rejects_xml_entities() -> None:
    content = b"<xml><ToUserName>corp-test</ToUserName><AgentID>1001</AgentID><MsgType>text</MsgType><FromUserName>user-1</FromUserName><MsgId>100</MsgId><Content>question</Content></xml>"
    encrypted, query = encrypted_wecom(content)
    body = f"<xml><Encrypt>{encrypted}</Encrypt></xml>".encode()
    assert wecom_messages(body, query, WECOM_CREDENTIAL)[0].sender_id == "user-1"
    with pytest.raises(CallbackRejected):
        wecom_messages(body, query, {**WECOM_CREDENTIAL, "agent_id": "1002"})
    with pytest.raises(CallbackRejected):
        wecom_messages(b'<!DOCTYPE x [<!ENTITY a SYSTEM "file:///etc/passwd">]><xml><Encrypt>&a;</Encrypt></xml>', query, WECOM_CREDENTIAL)


async def test_telegram_chat_scoped_ids_and_utf16_reply_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    channel = TelegramChannel({"bot_id": "123", "bot_token": "secret-test"})
    received: list[IncomingMessage] = []

    async def receive(message: IncomingMessage) -> None:
        received.append(message)

    channel.set_message_handler(receive)
    for chat_id in (10, 20):
        await channel.handle_update({"update_id": 1, "message": {"message_id": 7, "from": {"id": 1, "is_bot": False}, "chat": {"id": chat_id, "type": "private"}, "text": "question"}})
    assert [item.message_id for item in received] == ["10:7", "20:7"]
    payloads: list[dict[str, Any]] = []

    async def api(method: str, payload: dict[str, Any]) -> dict[str, Any]:
        assert method == "sendMessage"
        payloads.append(payload)
        return {"message_id": 1}

    monkeypatch.setattr(channel, "_api", api)
    await channel.send(OutgoingMessage(chat_id="10", text="😀" * 3000, reply_to_message_id="10:7"))
    assert len(payloads) == 2
    assert all(len(item["text"].encode("utf-16-le")) <= 8000 for item in payloads)
    assert payloads[0]["reply_parameters"]["message_id"] == 7
    with pytest.raises(ChannelWorkerError, match="REPLY_INVALID"):
        await channel.send(OutgoingMessage(chat_id="10", text="reply", reply_to_message_id="20:7"))


async def test_telegram_rejects_active_webhook_and_closes_session(monkeypatch: pytest.MonkeyPatch) -> None:
    channel = TelegramChannel({"bot_id": "123", "bot_token": "secret-test"})

    async def api(method: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {"id": 123} if method == "getMe" else {"url": "https://other.example/callback"}

    monkeypatch.setattr(channel, "_api", api)
    with pytest.raises(ChannelWorkerError, match="WEBHOOK_ACTIVE"):
        await channel.start()
    assert channel._session is None and channel.is_running is False


@pytest.mark.parametrize("admission", [MessageAdmission.ACCEPTED, MessageAdmission.DENIED])
async def test_callback_channel_dispatches_then_acknowledges_and_stops(admission: MessageAdmission) -> None:
    channel = LineChannel({"channel_id": "line-test", "access_token": "secret-test"})
    steps: list[str] = []
    acknowledged = asyncio.Event()
    item = CallbackReceipt(receipt="a" * 64, message=CallbackMessage(message_id="message", chat_id="chat", sender_id="sender", chat_type="p2p", text="question"))

    async def peek() -> CallbackBatch:
        return CallbackBatch(items=[] if steps else [item])

    async def ack(receipt: str) -> None:
        assert receipt == item.receipt
        steps.append("ack")
        acknowledged.set()

    async def receive(message: IncomingMessage) -> MessageAdmission:
        assert message.text == "question"
        steps.append("enqueue")
        return admission

    channel.bind_inbox(peek, ack)
    channel.set_message_handler(receive)
    try:
        await channel.start()
        await asyncio.wait_for(acknowledged.wait(), 2)
        assert channel.is_running
    finally:
        await channel.stop()
    assert steps == ["enqueue", "ack"] and not channel.is_running


async def test_wecom_token_cache_and_send_failure_are_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    channel = WeComChannel(WECOM_CREDENTIAL)
    calls: list[dict[str, Any]] = []
    fail = False

    async def request(method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        calls.append({"url": url, **kwargs})
        if url.endswith("gettoken"):
            return {"errcode": 0, "access_token": "ephemeral-test", "expires_in": 7200}
        return {"errcode": 40014 if fail else 0}

    monkeypatch.setattr(channel, "request", request)
    await channel.send(OutgoingMessage(chat_id="sender", text="中文" * 1500))
    assert sum(item["url"].endswith("gettoken") for item in calls) == 1
    assert all(len(item["json"]["text"]["content"].encode()) <= 2000 for item in calls if "json" in item)
    fail = True
    with pytest.raises(ChannelWorkerError, match="SEND_REJECTED"):
        await channel.send(OutgoingMessage(chat_id="sender", text="reply"))
    assert channel._token == ""


async def test_discord_start_waits_for_ready_and_closes_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    channel = DiscordChannel({"bot_id": "123", "bot_token": "secret-test"})
    closed: list[bool] = []

    async def login(token: str) -> None:
        raise discord.LoginFailure("sensitive provider diagnostics")

    async def close() -> None:
        closed.append(True)

    monkeypatch.setattr(channel._client, "login", login)
    monkeypatch.setattr(channel._client, "close", close)
    with pytest.raises(ChannelWorkerError, match=r"^CHANNEL_PROVIDER_START_FAILED$"):
        await channel.start()
    assert closed and channel.is_running is False


async def test_callback_client_uses_binding_token_and_never_master_scope() -> None:
    client = ChannelRuntimeClient(base_url="http://unused.example", api_token="test-token", runner_id="test")
    try:
        with pytest.raises(ChannelRuntimeClientError, match="SCOPE_MISMATCH"):
            await client.fetch_callback_events()
    finally:
        await client.close()


def test_callback_route_is_mounted_on_the_actual_application(client: Any) -> None:
    lookups: list[str] = []

    class MissingBinding:
        async def load_runtime_binding(self, binding_id: str) -> RuntimeBindingSpec:
            lookups.append(binding_id)
            raise ChannelAccessDenied

    client.app.dependency_overrides[get_runtime_control_service] = MissingBinding
    response = client.post("/api/v1/chat-channels/callbacks/line/binding-test", content=b"{}")
    assert response.status_code == 404
    # The real app owns the standard 404 envelope, including explicit 404s
    # raised by a mounted route. The isolated HTTP tests assert route details.
    assert response.json()["code"] == 404
    assert lookups == ["binding-test"]


@pytest.mark.parametrize("pressure", ["stopping", "global", "followup"])
async def test_real_worker_retry_keeps_callback_until_queue_admission(pressure: str) -> None:
    from api.channels.worker import ChannelWorker
    from tests.unit.test_feishu_worker import FakeAgentClient, FakeRedis, FakeStateStore, LifecycleBlockingBridge

    channel = LineChannel({"channel_id": "line-test", "access_token": "line-test-token"})
    bridge = LifecycleBlockingBridge()
    worker = ChannelWorker(
        provider_name="line",
        channel=channel,
        bridge=bridge,
        agent_client=FakeAgentClient(),
        state_store=FakeStateStore(),
        redis=FakeRedis(),
        queue_size=1 if pressure == "global" else 10,
        worker_concurrency=1,
        followup_queue_size=1,
    )
    pending: list[CallbackReceipt] = []
    attempts = asyncio.Event()
    acked = asyncio.Event()

    async def peek() -> CallbackBatch:
        return CallbackBatch(items=pending[:1])

    async def ack(receipt: str) -> None:
        assert pending and pending[0].receipt == receipt
        pending.pop(0)
        acked.set()

    real_enqueue = worker.enqueue

    async def observe(message: IncomingMessage) -> MessageAdmission:
        result = await real_enqueue(message)
        if result is MessageAdmission.RETRY:
            attempts.set()
        return result

    worker.enqueue = observe
    channel.bind_inbox(peek, ack)
    stop = asyncio.Event()
    task = asyncio.create_task(worker.run(stop))
    try:
        for _ in range(100):
            if channel.is_running:
                break
            await asyncio.sleep(0.01)
        assert channel.is_running
        if pressure == "stopping":
            worker._accepting_messages = False
        else:
            first = IncomingMessage("line", channel.account_id, "chat", "p2p", "first", "sender", "question")
            second = IncomingMessage("line", channel.account_id, "second-chat" if pressure == "global" else "chat", "p2p", "second", "sender", "question")
            assert await real_enqueue(first) is MessageAdmission.ACCEPTED
            await asyncio.wait_for(bridge.first_started.wait(), 2)
            assert await real_enqueue(second) is MessageAdmission.ACCEPTED
        pending.append(
            CallbackReceipt(
                receipt="e" * 64, message=CallbackMessage(message_id="callback-third", chat_id="third-chat" if pressure == "global" else "chat", sender_id="sender", chat_type="p2p", text="question")
            )
        )
        await asyncio.wait_for(attempts.wait(), 2)
        assert pending and not acked.is_set()
        assert bridge.rejected == []  # No terminal queue-busy claim poisons retry.
        worker._accepting_messages = True
        bridge.release_first.set()
        await asyncio.wait_for(acked.wait(), 3)
        for _ in range(100):
            if any(message.message_id == "callback-third" for message in bridge.messages):
                break
            await asyncio.sleep(0.01)
        assert [message.message_id for message in bridge.messages].count("callback-third") == 1
        assert pending == []
    finally:
        bridge.release_first.set()
        stop.set()
        await asyncio.wait_for(task, 3)


async def test_telegram_keeps_offset_on_worker_retry_and_handler_error(monkeypatch: pytest.MonkeyPatch) -> None:
    channel = TelegramChannel({"bot_id": "123", "bot_token": "test-token"})
    offsets: list[int] = []
    retry_count = 0
    accepted = asyncio.Event()
    block = asyncio.Event()

    async def receive(message: IncomingMessage) -> MessageAdmission:
        nonlocal retry_count
        retry_count += 1
        if retry_count == 1:
            raise RuntimeError("controlled handler failure")
        if retry_count == 2:
            return MessageAdmission.RETRY
        return MessageAdmission.ACCEPTED

    async def api(method: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
        offsets.append(payload["offset"])
        if payload["offset"] == 8:
            accepted.set()
            await block.wait()
        return [{"update_id": 7, "message": {"message_id": 1, "chat": {"id": 10, "type": "private"}, "from": {"id": 20, "is_bot": False}, "text": "question"}}]

    monkeypatch.setattr(channel, "_api", api)
    channel.set_message_handler(receive)
    task = asyncio.create_task(channel._poll())
    try:
        await asyncio.wait_for(accepted.wait(), 3)
        assert offsets == [0, 0, 0, 8]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_callback_cancellation_does_not_acknowledge_message() -> None:
    channel = LineChannel({"channel_id": "line-test", "access_token": "test-token"})
    item = CallbackReceipt(receipt="a" * 64, message=CallbackMessage(message_id="m", chat_id="c", sender_id="s", chat_type="p2p", text="q"))
    called = asyncio.Event()
    acknowledged: list[str] = []

    async def peek() -> CallbackBatch:
        return CallbackBatch(items=[item])

    async def ack(receipt: str) -> None:
        acknowledged.append(receipt)

    async def cancel(message: IncomingMessage) -> MessageAdmission:
        called.set()
        raise asyncio.CancelledError

    channel.bind_inbox(peek, ack)
    channel.set_message_handler(cancel)
    try:
        await channel.start()
        await asyncio.wait_for(called.wait(), 2)
        assert channel._task is not None
        with pytest.raises(asyncio.CancelledError):
            await channel._task
        assert acknowledged == []
    finally:
        await channel.stop()
