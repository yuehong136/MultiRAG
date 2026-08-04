from __future__ import annotations

import asyncio
import logging

import pytest

from api.channels import worker as worker_module
from api.channels.core.base import IncomingMessage, MessageHandler
from api.channels.feishu import provider as feishu_provider
from api.channels.worker import ChannelWorker, ChannelWorkerError
from common.app_config import AppConfig, ChannelsConfig, FeishuChannelConfig


class FakeChannel:
    def __init__(self) -> None:
        self.handler: MessageHandler | None = None
        self.is_running = False
        self.started = asyncio.Event()
        self.stopped = False

    def set_message_handler(self, handler: MessageHandler) -> None:
        self.handler = handler

    async def start(self) -> None:
        self.is_running = True
        self.started.set()

    async def stop(self) -> None:
        self.is_running = False
        self.stopped = True


class FakeBridge:
    def __init__(self) -> None:
        self.messages: list[IncomingMessage] = []

    async def handle_message(self, message: IncomingMessage) -> None:
        self.messages.append(message)


class BlockingBridge(FakeBridge):
    def __init__(self) -> None:
        super().__init__()
        self.first_started = asyncio.Event()
        self.release_first = asyncio.Event()

    async def handle_message(self, message: IncomingMessage) -> None:
        self.messages.append(message)
        if len(self.messages) == 1:
            self.first_started.set()
            await self.release_first.wait()


class FakeAgentClient:
    def __init__(self) -> None:
        self.preflight_called = False
        self.closed = False

    async def preflight(self) -> None:
        self.preflight_called = True

    async def close(self) -> None:
        self.closed = True


class FakeStateStore:
    def __init__(
        self,
        *,
        acquire: bool = True,
        renew: bool = True,
        renew_interval_seconds: float = 3600,
    ) -> None:
        self.leader_renew_interval_seconds = renew_interval_seconds
        self._acquire = acquire
        self._renew = renew
        self.released: list[str] = []

    async def acquire_leader(self, *, lease_name: str) -> str | None:
        del lease_name
        return "owner-token" if self._acquire else None

    async def renew_leader(self, owner_token: str, *, lease_name: str) -> bool:
        del owner_token, lease_name
        return self._renew

    async def release_leader(self, owner_token: str, *, lease_name: str) -> bool:
        del lease_name
        self.released.append(owner_token)
        return True


class FakeRedis:
    def __init__(self, *, ready: bool = True) -> None:
        self._ready = ready
        self.closed = False

    async def ping(self) -> bool:
        return self._ready

    async def aclose(self) -> None:
        self.closed = True


def _worker(
    *,
    channel: FakeChannel,
    bridge: FakeBridge,
    agent_client: FakeAgentClient,
    state_store: FakeStateStore,
    redis: FakeRedis,
    queue_size: int = 2,
    worker_concurrency: int = 1,
) -> ChannelWorker:
    return ChannelWorker(
        provider_name="feishu",
        channel=channel,
        bridge=bridge,
        agent_client=agent_client,
        state_store=state_store,
        redis=redis,
        queue_size=queue_size,
        worker_concurrency=worker_concurrency,
    )


def _message(message_id: str) -> IncomingMessage:
    return IncomingMessage(
        channel="feishu",
        account_id="account",
        chat_id="chat",
        message_id=message_id,
        sender_id="sender",
        content="hello",
        message_type="text",
        chat_type="p2p",
        sender_type="user",
    )


def test_managed_domain_resolver_prefers_root_and_accepts_upstream_nested_shape() -> None:
    assert feishu_provider._resolve_domain({"domain": "feishu", "credential": {"domain": "lark"}}) == "feishu"
    assert feishu_provider._resolve_domain({"credential": {"domain": "lark"}}) == "lark"


@pytest.mark.parametrize(
    "public_config",
    [
        {},
        {"domain": ""},
        {"domain": "international", "credential": {"domain": "lark"}},
    ],
)
def test_managed_domain_resolver_never_silently_defaults(
    public_config: dict[str, object],
) -> None:
    with pytest.raises(ChannelWorkerError, match="CHANNEL_RUNTIME_CONFIG_INVALID"):
        feishu_provider._resolve_domain(public_config)


@pytest.mark.asyncio
async def test_worker_runs_preflight_consumes_message_and_releases_lease(
    caplog: pytest.LogCaptureFixture,
) -> None:
    channel = FakeChannel()
    bridge = FakeBridge()
    agent_client = FakeAgentClient()
    state_store = FakeStateStore()
    redis = FakeRedis()
    worker = _worker(
        channel=channel,
        bridge=bridge,
        agent_client=agent_client,
        state_store=state_store,
        redis=redis,
    )
    stop_event = asyncio.Event()
    caplog.set_level(logging.INFO, logger="api.channels.worker")

    run_task = asyncio.create_task(worker.run(stop_event))
    await channel.started.wait()
    assert channel.handler is not None
    await channel.handler(_message("message-1"))
    await asyncio.sleep(0)
    stop_event.set()
    await run_task

    assert agent_client.preflight_called is True
    assert bridge.messages[0].message_id == "message-1"
    assert channel.stopped is True
    assert state_store.released == ["owner-token"]
    assert agent_client.closed is True
    assert redis.closed is True
    assert "queue_wait_ms=" in caplog.text
    assert "message-1" not in caplog.text


@pytest.mark.asyncio
async def test_worker_fails_closed_when_leader_lease_is_held() -> None:
    channel = FakeChannel()
    bridge = FakeBridge()
    agent_client = FakeAgentClient()
    state_store = FakeStateStore(acquire=False)
    redis = FakeRedis()
    worker = _worker(
        channel=channel,
        bridge=bridge,
        agent_client=agent_client,
        state_store=state_store,
        redis=redis,
    )

    with pytest.raises(ChannelWorkerError, match="LEADER_LEASE_HELD"):
        await worker.run(asyncio.Event())

    assert channel.started.is_set() is False
    assert agent_client.closed is True
    assert redis.closed is True


@pytest.mark.asyncio
async def test_worker_fails_closed_when_redis_preflight_fails() -> None:
    channel = FakeChannel()
    bridge = FakeBridge()
    agent_client = FakeAgentClient()
    state_store = FakeStateStore()
    redis = FakeRedis(ready=False)
    worker = _worker(
        channel=channel,
        bridge=bridge,
        agent_client=agent_client,
        state_store=state_store,
        redis=redis,
    )

    with pytest.raises(ChannelWorkerError, match="REDIS_PREFLIGHT_FAILED"):
        await worker.run(asyncio.Event())

    assert agent_client.preflight_called is False
    assert channel.started.is_set() is False
    assert agent_client.closed is True
    assert redis.closed is True


@pytest.mark.asyncio
async def test_worker_preserves_same_conversation_arrival_order_with_two_consumers() -> None:
    channel = FakeChannel()
    bridge = BlockingBridge()
    worker = _worker(
        channel=channel,
        bridge=bridge,
        agent_client=FakeAgentClient(),
        state_store=FakeStateStore(),
        redis=FakeRedis(),
        queue_size=10,
        worker_concurrency=2,
    )
    stop_event = asyncio.Event()
    run_task = asyncio.create_task(worker.run(stop_event))
    await channel.started.wait()
    assert channel.handler is not None

    await channel.handler(_message("message-first"))
    await channel.handler(_message("message-second"))
    await asyncio.wait_for(bridge.first_started.wait(), timeout=1)
    await asyncio.sleep(0.02)
    assert [message.message_id for message in bridge.messages] == ["message-first"]

    bridge.release_first.set()
    for _ in range(100):
        if len(bridge.messages) == 2:
            break
        await asyncio.sleep(0.01)
    stop_event.set()
    await run_task

    assert [message.message_id for message in bridge.messages] == [
        "message-first",
        "message-second",
    ]


@pytest.mark.asyncio
async def test_leader_loss_cancels_consumers_without_draining_old_queue() -> None:
    channel = FakeChannel()
    bridge = BlockingBridge()
    state_store = FakeStateStore(renew=False, renew_interval_seconds=0.05)
    worker = _worker(
        channel=channel,
        bridge=bridge,
        agent_client=FakeAgentClient(),
        state_store=state_store,
        redis=FakeRedis(),
        queue_size=10,
        worker_concurrency=1,
    )
    run_task = asyncio.create_task(worker.run(asyncio.Event()))
    await channel.started.wait()
    assert channel.handler is not None
    await channel.handler(_message("message-in-flight"))
    await channel.handler(_message("message-must-not-run"))
    await asyncio.wait_for(bridge.first_started.wait(), timeout=1)

    with pytest.raises(ChannelWorkerError, match="LEADER_LEASE_LOST"):
        await asyncio.wait_for(run_task, timeout=1)

    assert [message.message_id for message in bridge.messages] == ["message-in-flight"]
    assert channel.stopped is True
    assert state_store.released == ["owner-token"]


@pytest.mark.asyncio
async def test_redis_client_has_bounded_network_timeouts() -> None:
    redis = worker_module._build_redis(AppConfig())
    connection = redis.connection_pool.connection_kwargs

    assert connection["socket_connect_timeout"] == 5
    assert connection["socket_timeout"] == 5
    assert connection["retry_on_error"] == []

    await redis.aclose()


@pytest.mark.asyncio
async def test_build_worker_casts_integer_timeout_config_for_runtime_type_check() -> None:
    channel_config = FeishuChannelConfig(
        enabled=True,
        app_id="cli_test",
        app_secret="app-secret",
        multirag_base_url="http://127.0.0.1:8123",
        agent_id="agent-id",
        agent_api_token="multirag-standard-token",
        release_marker="test-v1",
    )
    app_config = AppConfig(channels=ChannelsConfig(feishu=channel_config))

    worker = worker_module._build_worker(app_config, channel_config)

    assert isinstance(worker, ChannelWorker)
    await worker.close(drain=False)


def test_worker_main_uses_lightweight_unified_bootstrap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bootstrap_calls: list[bool] = []

    def fake_initialize(
        force: bool = False,
        *,
        initialize_resources: bool = True,
    ) -> None:
        del force
        bootstrap_calls.append(initialize_resources)

    async def fake_run_channel(channel_name: str) -> None:
        assert channel_name == "feishu"

    monkeypatch.setattr(worker_module, "ensure_initialized", fake_initialize)
    monkeypatch.setattr(worker_module, "_run_channel", fake_run_channel)

    assert worker_module.main(["--channel", "feishu"]) == 0
    assert bootstrap_calls == [False]
