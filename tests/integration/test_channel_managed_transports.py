"""Real Channel HTTP/SQL/encryption and Redis callback admission (CHN-P15)."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
import requests
import sqlalchemy as sa
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from redis import Redis
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.channel_control.secret_store import EncryptedSecret
from api.channel_runtime.tokens import derive_binding_workload_token
from api.channels.core.base import ChannelAction, ChannelActionResponse, IncomingMessage, MessageAdmission, MessageHandler, OutgoingMessage
from api.channels.line.channel import LineChannel
from api.channels.provider import ChannelWorkerError
from api.channels.runtime_client import ChannelRuntimeClient
from api.channels.state_store import RedisChannelStateStore
from api.channels.telegram.channel import TelegramChannel
from api.channels.wecom.channel import WeComChannel
from api.channels.worker import ChannelWorker
from api.db.db_models import ChannelSecret, ChatChannel
from tests.support.channel_provider_http import channel_provider_http
from tests.support.database import scratch_database


@pytest.fixture(scope="module")
def channel_engine(tmp_path_factory: pytest.TempPathFactory) -> Iterator[sa.Engine]:
    # Use the explicit, journaled scratch lifecycle. The generic conftest's
    # Testcontainers fallback cannot provide a per-database ownership receipt.
    with scratch_database(tmp_path_factory.mktemp("managed-channel-sql")) as engine:
        yield engine


@pytest.fixture
def channel_http(channel_engine: sa.Engine, tmp_path: Path) -> Iterator[dict[str, Any]]:
    with channel_provider_http(channel_engine, tmp_path) as env:
        yield env


def request(env: dict[str, Any], method: str, path: str, *, actor: str = "owner", body: Any = None) -> dict[str, Any]:
    response = requests.request(method, env["base"] + "/api/v1" + path, headers={"Authorization": "Bearer " + env["tokens"][actor]}, json=body, timeout=10)
    assert response.status_code == 200, response.text
    return response.json()


def create(env: dict[str, Any], provider: str, credential: dict[str, str], *, enabled: bool = False) -> dict[str, Any]:
    payload: dict[str, Any] = {"name": "managed-" + provider, "channel": provider, "config": {"credential": credential}}
    if enabled:
        payload["binding"] = {"target_type": "multirag.dialog", "target_id": env["dialogs"]["owner"], "enabled": True}
    result = request(env, "POST", "/chat-channels", body=payload)
    assert result["retcode"] == 0, result
    return result["data"]


def runtime_headers(env: dict[str, Any], binding: str, generation: int) -> dict[str, str]:
    token = derive_binding_workload_token(env["master_token"], binding_id=binding, generation=generation)
    return {"Authorization": "Bearer " + token, "X-Channel-Binding-Generation": str(generation)}


def test_real_management_roundtrip_encrypts_every_new_provider(channel_http: dict[str, Any]) -> None:
    env = channel_http
    providers = request(env, "GET", "/chat-channels/providers")["data"]["items"]
    assert {item["provider"] for item in providers} == {"feishu", "dingtalk", "discord", "telegram", "line", "wecom"}
    for provider, credential in [
        ("discord", {"bot_id": "123", "bot_token": "discord-secret"}),
        ("telegram", {"bot_id": "123", "bot_token": "telegram-secret"}),
        ("line", {"channel_id": "123", "channel_secret": "line-secret", "access_token": "line-access"}),
        ("wecom", {"corp_id": "corp-test", "agent_id": "1001", "corp_secret": "corp-secret", "callback_token": "callback-secret", "aes_key": "a" * 43}),
    ]:
        created = create(env, provider, credential)
        identifier = created["id"]
        response = request(env, "PATCH", f"/chat-channels/{identifier}", body={"config": {"allowed_user_ids": ["one", "two"]}})
        assert response["retcode"] == 0
        read = request(env, "GET", f"/chat-channels/{identifier}")["data"]
        assert read["config"]["allowed_user_ids"] == ["one", "two"]
        assert request(env, "GET", f"/chat-channels/{identifier}", actor="other")["retcode"] != 0
        with Session(env["engine"]) as independent:
            row = independent.get(ChatChannel, identifier)
            secret = independent.scalar(select(ChannelSecret).where(ChannelSecret.channel_id == identifier))
            assert row is not None and secret is not None
            whole = asyncio.run(
                env["cipher"].decrypt(tenant_id=env["owners"]["owner"], channel_id=identifier, encrypted=EncryptedSecret(ciphertext=secret.ciphertext, key_id=secret.key_id, version=secret.version))
            )
            for key, value in whole.items():
                assert value == credential[key]
                assert key not in row.config["credential"]
                assert value not in json.dumps(read)
                assert value not in secret.ciphertext


def test_line_callback_auth_scope_generation_and_bounded_queue(channel_http: dict[str, Any]) -> None:
    env = channel_http
    created = create(env, "line", {"channel_id": "line-test", "channel_secret": "line-secret", "access_token": "line-access"}, enabled=True)
    binding, generation = created["binding"]["id"], created["binding"]["generation"]
    key = env["reserve_inbox"](binding, generation)
    body = json.dumps({"events": [{"type": "message", "message": {"type": "text", "id": "line-message-1", "text": "question"}, "source": {"type": "user", "userId": "line-user-1"}}]}).encode()
    signature = base64.b64encode(hmac.digest(b"line-secret", body, "sha256")).decode()
    public = f"{env['base']}/api/v1/chat-channels/callbacks/line/{binding}"
    assert requests.post(public, data=body, headers={"x-line-signature": "invalid"}, timeout=10).status_code == 403
    with Redis(**env["redis"], decode_responses=True) as redis:
        assert not redis.exists(key)
        assert requests.post(public, data=body, headers={"x-line-signature": signature}, timeout=10).status_code == 200
        assert redis.llen(key) == 1 and 0 < redis.ttl(key) <= 600
        assert env["owners"]["owner"] not in key and binding not in key
        endpoint = f"{env['base']}/api/v1/internal/channel-bindings/{binding}/callback-inbox"
        headers = runtime_headers(env, binding, generation)
        assert requests.get(endpoint, timeout=10).status_code == 401
        response = requests.get(endpoint, headers=headers, timeout=10)
        assert response.status_code == 200
        receipt = response.json()["items"][0]["receipt"]
        assert response.json()["items"][0]["message"]["text"] == "question"
        assert redis.llen(key) == 1  # peek does not lose the accepted event
        assert requests.post(endpoint + "/ack", headers=headers, json={"receipt": "b" * 64}, timeout=10).status_code == 409
        assert redis.llen(key) == 1
        assert requests.post(endpoint + "/ack", headers=headers, json={"receipt": receipt}, timeout=10).status_code == 204
        assert redis.llen(key) == 0
        entry = json.dumps({"receipt": "f" * 64, "message": {"text": "owned-capacity-fixture"}})
        redis.rpush(key, *([entry] * 256))
        assert requests.post(public, data=body, headers={"x-line-signature": signature}, timeout=10).status_code == 503
        assert redis.llen(key) == 256
        changed = request(env, "PATCH", f"/chat-channels/{created['id']}", body={"config": {"allowed_user_ids": ["line-user-1"]}})
        assert changed["retcode"] == 0
        next_generation = changed["data"]["binding"]["generation"]
        next_key = env["reserve_inbox"](binding, next_generation)
        assert next_generation > generation and next_key != key
        assert requests.get(endpoint, headers=headers, timeout=10).status_code == 404
        assert requests.get(endpoint, headers=runtime_headers(env, binding, next_generation), timeout=10).json() == {"items": []}
        assert requests.post(public, data=body, headers={"x-line-signature": signature}, timeout=10).status_code == 200
        assert redis.llen(next_key) == 1
        disabled = request(env, "PATCH", f"/chat-channels/{created['id']}", body={"status": 0})
        assert disabled["retcode"] == 0
        assert requests.post(public, data=body, headers={"x-line-signature": signature}, timeout=10).status_code == 404


def test_incomplete_multi_secret_cannot_enable_and_failure_rolls_back(channel_http: dict[str, Any]) -> None:
    env = channel_http
    created = create(env, "line", {"channel_id": "line-test", "channel_secret": "line-secret"})
    identifier = created["id"]
    binding = {"target_type": "multirag.dialog", "target_id": env["dialogs"]["owner"], "enabled": True}
    rejected = request(env, "PATCH", f"/chat-channels/{identifier}", body={"binding": binding})
    assert rejected["retcode"] != 0
    read = request(env, "GET", f"/chat-channels/{identifier}")["data"]
    assert read["status"] == 0 and read["binding"] is None
    completed = request(env, "PATCH", f"/chat-channels/{identifier}", body={"config": {"credential": {"access_token": "line-access"}}, "binding": binding})
    assert completed["retcode"] == 0
    assert completed["data"]["status"] == 1
    forbidden = request(env, "PATCH", f"/chat-channels/{identifier}", body={"binding": {**binding, "target_id": env["dialogs"]["other"]}})
    assert forbidden["retcode"] != 0
    assert request(env, "GET", f"/chat-channels/{identifier}")["data"]["binding"]["target_id"] == env["dialogs"]["owner"]


def test_wecom_shared_callback_verification_and_encrypted_message(channel_http: dict[str, Any]) -> None:
    env = channel_http
    credential = {"corp_id": "corp-test", "agent_id": "1001", "corp_secret": "corp-secret", "callback_token": "callback-secret", "aes_key": base64.b64encode(b"a" * 32).decode().rstrip("=")}
    created = create(env, "wecom", credential, enabled=True)
    binding, generation = created["binding"]["id"], created["binding"]["generation"]
    key = env["reserve_inbox"](binding, generation)
    url = f"{env['base']}/api/v1/chat-channels/callbacks/wecom/{binding}"

    def encrypt(message: bytes) -> tuple[str, dict[str, str]]:
        plain = b"r" * 16 + len(message).to_bytes(4, "big") + message + credential["corp_id"].encode()
        pad = 32 - len(plain) % 32
        encoder = Cipher(algorithms.AES(b"a" * 32), modes.CBC(b"a" * 16)).encryptor()
        encrypted = base64.b64encode(encoder.update(plain + bytes([pad]) * pad) + encoder.finalize()).decode()
        params = {"timestamp": str(int(time.time())), "nonce": "nonce-test"}
        params["msg_signature"] = hashlib.sha1("".join(sorted([credential["callback_token"], params["timestamp"], params["nonce"], encrypted])).encode()).hexdigest()
        return encrypted, params

    encrypted, params = encrypt(b"verified-echo")
    response = requests.get(url, params={**params, "echostr": encrypted}, timeout=10)
    assert response.status_code == 200 and response.content == b"verified-echo"
    message = b"<xml><ToUserName>corp-test</ToUserName><AgentID>1001</AgentID><MsgType>text</MsgType><FromUserName>employee-1</FromUserName><MsgId>100</MsgId><Content>question</Content></xml>"
    encrypted, params = encrypt(message)
    response = requests.post(url, params=params, data=f"<xml><Encrypt>{encrypted}</Encrypt></xml>", timeout=10)
    assert response.status_code == 200
    with Redis(**env["redis"], decode_responses=True) as independent:
        entry = json.loads(independent.lindex(key, 0))
        assert entry["message"]["sender_id"] == "employee-1"
    assert requests.post(url.replace("/wecom/", "/line/"), data=b"{}", timeout=10).status_code == 404


def test_real_http_poll_and_outbound_text_failures(channel_http: dict[str, Any]) -> None:
    """Only the vendor origin is redirected; real aiohttp and parsers run."""
    env = channel_http
    calls: list[dict[str, Any]] = []
    delivered: list[IncomingMessage] = []
    failures = {"line": False}

    class Vendor(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def do_GET(self) -> None:
            self.respond()

        def do_POST(self) -> None:
            self.respond()

        def respond(self) -> None:
            length = int(self.headers.get("content-length", "0"))
            body = json.loads(self.rfile.read(length)) if length else None
            calls.append({"path": self.path, "body": body, "authorization": self.headers.get("Authorization")})
            status = 200
            if self.path.endswith("getMe"):
                data: dict[str, Any] = {"ok": True, "result": {"id": 123}}
            elif self.path.endswith("getWebhookInfo"):
                data = {"ok": True, "result": {"url": ""}}
            elif self.path.endswith("getUpdates"):
                if not body["offset"]:
                    data = {"ok": True, "result": [{"update_id": 7, "message": {"message_id": 1, "chat": {"id": 10, "type": "private"}, "from": {"id": 20, "is_bot": False}, "text": "question"}}]}
                else:
                    time.sleep(0.05)
                    data = {"ok": True, "result": []}
            elif self.path.endswith("sendMessage"):
                data = {"ok": True, "result": {"message_id": 9}}
            elif "/gettoken?" in self.path:
                data = {"errcode": 0, "access_token": "wecom-test-token", "expires_in": 7200}
            elif "/message/send?" in self.path:
                data = {"errcode": 0}
            else:
                status = 503 if failures["line"] else 200
                data = {}
            encoded = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    env["journal"].event("reserve-vendor-listener", host="127.0.0.1", port=0)
    vendor = ThreadingHTTPServer(("127.0.0.1", 0), Vendor)
    env["journal"].event("vendor-listener-created", host="127.0.0.1", port=vendor.server_port)
    thread = threading.Thread(target=vendor.serve_forever, daemon=True)
    env["journal"].event("reserve-vendor-thread", name="controlled-channel-vendor")
    thread.start()

    async def exercise() -> None:
        from urllib.parse import urlsplit

        telegram = TelegramChannel({"bot_id": "123", "bot_token": "telegram-test-token"})
        line = LineChannel({"channel_id": "123", "access_token": "line-test-token"})
        wecom = WeComChannel({"corp_id": "corp-test", "agent_id": "1001", "corp_secret": "corp-test-secret"})
        channels = [telegram, line, wecom]
        for channel in channels:
            original = channel.request

            def redirect(original: Any) -> Any:
                async def send(method: str, url: str, **kwargs: Any) -> dict[str, Any]:
                    parts = urlsplit(url)
                    return await original(method, f"http://127.0.0.1:{vendor.server_port}" + parts.path, **kwargs)

                return send

            channel.request = redirect(original)
        received = asyncio.Event()

        async def receive(message: IncomingMessage) -> None:
            delivered.append(message)
            received.set()

        telegram.set_message_handler(receive)
        try:
            await telegram.start()
            await asyncio.wait_for(received.wait(), 5)
            assert delivered[0].message_id == "10:1"
            await telegram.send(OutgoingMessage(chat_id="10", text="answer", reply_to_message_id="10:1"))
            await line.open_http()
            await line.send(OutgoingMessage(chat_id="line-user", text="answer"))
            failures["line"] = True
            with pytest.raises(ChannelWorkerError, match="HTTP_REJECTED"):
                await line.send(OutgoingMessage(chat_id="line-user", text="answer"))
            await wecom.open_http()
            await wecom.send(OutgoingMessage(chat_id="employee", text="answer"))
        finally:
            for channel in channels:
                await channel.stop()
                assert channel._session is None and not channel.is_running

    try:
        asyncio.run(exercise())
        assert any(item["path"].endswith("sendMessage") and item["body"]["reply_parameters"]["message_id"] == 1 for item in calls)
        assert any(item["path"].endswith("/message/push") and item["authorization"] == "Bearer line-test-token" for item in calls)
        assert any("/message/send?" in item["path"] and item["body"]["agentid"] == 1001 for item in calls)
    finally:
        vendor.shutdown()
        vendor.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()
        env["journal"].event("vendor-listener-cleanup-readback", fd=vendor.socket.fileno(), thread_absent=True)


@pytest.mark.parametrize("pressure", ["stopping", "global", "followup"])
def test_live_inbox_private_client_and_real_worker_pressure(channel_http: dict[str, Any], pressure: str) -> None:
    """A rejected admission stays in Redis and later executes exactly once."""
    env = channel_http
    created = create(env, "line", {"channel_id": "line-test", "channel_secret": "line-secret", "access_token": "line-access"}, enabled=True)
    binding, generation = created["binding"]["id"], created["binding"]["generation"]
    key = env["reserve_inbox"](binding, generation)
    body = json.dumps({"events": [{"type": "message", "message": {"type": "text", "id": "retry-message", "text": "question"}, "source": {"type": "user", "userId": "sender"}}]}).encode()
    signature = base64.b64encode(hmac.digest(b"line-secret", body, "sha256")).decode()

    async def exercise() -> None:
        from redis.asyncio import Redis as AsyncRedis

        started, release, retried, admitted = (asyncio.Event() for _ in range(4))
        executed: list[str] = []
        rejected: list[str] = []

        class ControlledTarget:
            async def preflight(self) -> None:
                pass

            async def close(self) -> None:
                pass

        class ControlledExecution:
            def accepts_message(self, message: IncomingMessage) -> bool:
                return True

            def set_message_scheduler(self, scheduler: MessageHandler) -> None:
                pass

            def message_queued(self, message: IncomingMessage, *, queue_position: int) -> None:
                pass

            def message_rejected(self, message: IncomingMessage, *, reason: str) -> None:
                rejected.append(message.message_id)

            async def handle_action(self, action: ChannelAction) -> ChannelActionResponse:
                return ChannelActionResponse("success", "controlled")

            async def handle_message(self, message: IncomingMessage) -> None:
                executed.append(message.message_id)
                if message.message_id == "first":
                    started.set()
                    await release.wait()
                if message.message_id == "retry-message":
                    admitted.set()

            async def close(self) -> None:
                pass

        redis = AsyncRedis(**env["redis"], decode_responses=True, socket_connect_timeout=5, socket_timeout=5)
        state = RedisChannelStateStore(redis, scope=("channel-pressure", binding))
        leader_key = state._leader_key("line")
        env["journal"].event("reserve-redis-key", key=leader_key, purpose="real worker leader lease")
        token = derive_binding_workload_token(env["master_token"], binding_id=binding, generation=generation)
        runtime = ChannelRuntimeClient(base_url=env["base"], api_token=token, runner_id="pressure-worker", binding_id=binding, binding_generation=generation)
        channel = LineChannel({"channel_id": "line-test", "access_token": "line-access"})
        channel.bind_inbox(runtime.fetch_callback_events, runtime.ack_callback_event)
        worker = ChannelWorker(
            provider_name="line",
            channel=channel,
            bridge=ControlledExecution(),
            agent_client=ControlledTarget(),
            state_store=state,
            redis=redis,
            queue_size=1 if pressure == "global" else 10,
            worker_concurrency=1,
            followup_queue_size=1,
        )
        real_enqueue = worker.enqueue

        async def enqueue(message: IncomingMessage) -> MessageAdmission:
            result = await real_enqueue(message)
            if message.message_id == "retry-message" and result is MessageAdmission.RETRY:
                retried.set()
            return result

        worker.enqueue = enqueue
        stop = asyncio.Event()
        task = asyncio.create_task(worker.run(stop))
        try:
            for _ in range(200):
                if channel.is_running:
                    break
                await asyncio.sleep(0.01)
            assert channel.is_running
            if pressure == "stopping":
                worker._accepting_messages = False
            else:
                first = IncomingMessage("line", channel.account_id, "sender", "p2p", "first", "sender", "question")
                second = IncomingMessage("line", channel.account_id, "other-chat" if pressure == "global" else "sender", "p2p", "second", "sender", "question")
                assert await real_enqueue(first) is MessageAdmission.ACCEPTED
                await asyncio.wait_for(started.wait(), 2)
                assert await real_enqueue(second) is MessageAdmission.ACCEPTED
            response = await asyncio.to_thread(requests.post, f"{env['base']}/api/v1/chat-channels/callbacks/line/{binding}", data=body, headers={"x-line-signature": signature}, timeout=10)
            assert response.status_code == 200
            await asyncio.wait_for(retried.wait(), 3)
            assert await redis.llen(key) == 1
            assert rejected == [] and "retry-message" not in executed
            worker._accepting_messages = True
            release.set()
            await asyncio.wait_for(admitted.wait(), 4)
            for _ in range(100):
                if await redis.llen(key) == 0:
                    break
                await asyncio.sleep(0.01)
            assert await redis.llen(key) == 0
            assert executed.count("retry-message") == 1
        finally:
            release.set()
            stop.set()
            await asyncio.wait_for(task, 5)
            await runtime.close()
            async with AsyncRedis(**env["redis"], decode_responses=True) as independent:
                assert not await independent.exists(leader_key)
                env["journal"].event("worker-lease-cleanup-readback", key=leader_key, absent=True)

    asyncio.run(exercise())
