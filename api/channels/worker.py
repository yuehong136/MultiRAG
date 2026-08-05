from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import logging
import os
import signal
import socket
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from redis.asyncio import Redis

from api.channel_runtime.schemas import RuntimeState
from api.channels.agent_bridge import FeishuAgentBridge, MultiRAGAgentClient
from api.channels.binding_bridge import FeishuBindingBridge
from api.channels.core.base import IncomingMessage, MessageHandler
from api.channels.provider import ChannelWorkerError, supported_provider_names, worker_provider
from api.channels.runtime_client import ChannelRuntimeClient, MultiRAGBindingExecutionClient
from api.channels.state_store import RedisChannelStateStore
from common.app_config import AppConfig, AppConfigError, FeishuChannelConfig, get_app_config
from common.bootstrap import ensure_initialized

LOGGER = logging.getLogger(__name__)

_QUEUE_DRAIN_TIMEOUT_SECONDS = 5
_CHANNEL_MONITOR_INTERVAL_SECONDS = 2
_REDIS_CONNECT_TIMEOUT_SECONDS = 5
_REDIS_OPERATION_TIMEOUT_SECONDS = 5


@dataclass(frozen=True, slots=True)
class _QueuedMessage:
    message: IncomingMessage
    enqueued_at: float
    order_key: str
    ticket: int


@dataclass(slots=True)
class _ConversationOrder:
    condition: asyncio.Condition
    next_ticket: int = 0
    next_to_run: int = 0
    pending: int = 0


@runtime_checkable
class WorkerChannel(Protocol):
    @property
    def is_running(self) -> bool: ...

    def set_message_handler(self, handler: MessageHandler) -> None: ...

    async def start(self) -> None: ...

    async def stop(self) -> None: ...


@runtime_checkable
class MessageBridge(Protocol):
    async def handle_message(self, message: IncomingMessage) -> None: ...


@runtime_checkable
class PreflightAgentClient(Protocol):
    async def preflight(self) -> None: ...

    async def close(self) -> None: ...


@runtime_checkable
class WorkerStateStore(Protocol):
    @property
    def leader_renew_interval_seconds(self) -> int: ...

    async def acquire_leader(self, *, lease_name: str) -> str | None: ...

    async def renew_leader(self, owner_token: str, *, lease_name: str) -> bool: ...

    async def release_leader(self, owner_token: str, *, lease_name: str) -> bool: ...


@runtime_checkable
class WorkerRedis(Protocol):
    async def ping(self) -> bool: ...

    async def aclose(self) -> None: ...


def _short_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _message_order_key(message: IncomingMessage) -> str:
    digest = hashlib.sha256()
    for value in (
        message.channel,
        message.account_id,
        message.chat_id,
        message.sender_id,
    ):
        encoded = value.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, byteorder="big"))
        digest.update(encoded)
    return digest.hexdigest()


def _redis_host_port(raw_host: str) -> tuple[str, int]:
    host, separator, raw_port = raw_host.rpartition(":")
    if not separator:
        return raw_host, 6379
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise ChannelWorkerError("REDIS_ADDRESS_INVALID") from exc
    normalized_host = host.removeprefix("[").removesuffix("]")
    if not normalized_host or not 0 < port < 65536:
        raise ChannelWorkerError("REDIS_ADDRESS_INVALID")
    return normalized_host, port


class ChannelWorker:
    """Owns one transport, a bounded queue, and its stateful bridge.

    Transport-agnostic: the concrete SDK arrives as a ``WorkerChannel``, and
    ``provider_name`` only names the leader lease, tasks and log lines.
    """

    def __init__(
        self,
        *,
        provider_name: str,
        channel: WorkerChannel,
        bridge: MessageBridge,
        agent_client: PreflightAgentClient,
        state_store: WorkerStateStore,
        redis: WorkerRedis,
        queue_size: int,
        worker_concurrency: int,
    ) -> None:
        self._provider_name = provider_name
        self._channel = channel
        self._bridge = bridge
        self._agent_client = agent_client
        self._state_store = state_store
        self._redis = redis
        self._queue: asyncio.Queue[_QueuedMessage] = asyncio.Queue(maxsize=queue_size)
        self._conversation_orders: dict[str, _ConversationOrder] = {}
        self._worker_concurrency = worker_concurrency
        self._tasks: list[asyncio.Task[None]] = []
        self._owner_token: str | None = None
        self._stop_event: asyncio.Event | None = None
        self._runtime_error_code = ""
        self._started = False
        self._accepting_messages = False

    async def run(self, stop_event: asyncio.Event) -> None:
        self._stop_event = stop_event
        graceful_shutdown = False
        try:
            await self._preflight()
            owner_token = await self._state_store.acquire_leader(lease_name=self._provider_name)
            if owner_token is None:
                raise ChannelWorkerError("LEADER_LEASE_HELD")
            self._owner_token = owner_token

            self._accepting_messages = True
            self._channel.set_message_handler(self.enqueue)
            self._tasks = [asyncio.create_task(self._consume(index), name=f"{self._provider_name}-channel-worker-{index}") for index in range(self._worker_concurrency)]
            self._tasks.append(asyncio.create_task(self._renew_leader(), name=f"{self._provider_name}-channel-leader-renew"))
            await self._channel.start()
            self._started = True
            self._tasks.append(asyncio.create_task(self._monitor_channel(), name=f"{self._provider_name}-channel-monitor"))
            LOGGER.info("channel_event=worker_started channel=%s result=ok", self._provider_name)
            await stop_event.wait()
            if self._runtime_error_code:
                raise ChannelWorkerError(self._runtime_error_code)
            graceful_shutdown = True
        finally:
            await self.close(drain=graceful_shutdown)

    async def enqueue(self, message: IncomingMessage) -> None:
        """Fast SDK callback target: no Redis, HTTP, or reply I/O is allowed."""

        if not self._accepting_messages:
            LOGGER.warning(
                "channel_event=queue_rejected trace_id=%s message_id_hash=%s result=dropped error_code=WORKER_STOPPING",
                _short_hash(message.message_id),
                _short_hash(message.message_id),
            )
            return

        order_key = _message_order_key(message)
        order = self._conversation_orders.get(order_key)
        if order is None:
            order = _ConversationOrder(condition=asyncio.Condition())
            self._conversation_orders[order_key] = order
        ticket = order.next_ticket

        try:
            self._queue.put_nowait(
                _QueuedMessage(
                    message=message,
                    enqueued_at=time.monotonic(),
                    order_key=order_key,
                    ticket=ticket,
                )
            )
        except asyncio.QueueFull:
            if order.pending == 0:
                self._conversation_orders.pop(order_key, None)
            LOGGER.error(
                "channel_event=queue_full trace_id=%s message_id_hash=%s result=dropped error_code=QUEUE_FULL",
                _short_hash(message.message_id),
                _short_hash(message.message_id),
            )
            return

        order.next_ticket += 1
        order.pending += 1

    async def close(self, *, drain: bool = True) -> None:
        self._accepting_messages = False
        # Always stop: channel.start() can fail after creating its isolated
        # thread but before the worker marks itself started.
        with contextlib.suppress(Exception):
            await self._channel.stop()
        self._started = False

        if drain:
            try:
                await asyncio.wait_for(self._queue.join(), timeout=_QUEUE_DRAIN_TIMEOUT_SECONDS)
            except TimeoutError:
                LOGGER.error("channel_event=queue_drain result=failed error_code=QUEUE_DRAIN_TIMEOUT")

        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

        if self._owner_token is not None:
            try:
                await self._state_store.release_leader(self._owner_token, lease_name=self._provider_name)
            except Exception:
                LOGGER.error("channel_event=leader_release result=failed error_code=REDIS_RELEASE_FAILED")
            self._owner_token = None

        await self._agent_client.close()
        await self._redis.aclose()
        LOGGER.info("channel_event=worker_stopped channel=%s result=ok", self._provider_name)

    async def _preflight(self) -> None:
        try:
            redis_ready = await self._redis.ping()
        except Exception as exc:
            raise ChannelWorkerError("REDIS_PREFLIGHT_FAILED") from exc
        if not redis_ready:
            raise ChannelWorkerError("REDIS_PREFLIGHT_FAILED")
        try:
            await self._agent_client.preflight()
        except Exception as exc:
            code = getattr(exc, "code", "AGENT_PREFLIGHT_FAILED")
            raise ChannelWorkerError(str(code)) from exc

    async def _consume(self, _worker_index: int) -> None:
        while True:
            queued = await self._queue.get()
            order = self._conversation_orders[queued.order_key]
            turn_acquired = False
            try:
                async with order.condition:
                    await order.condition.wait_for(lambda: queued.ticket == order.next_to_run)
                    turn_acquired = True

                queue_wait_ms = round((time.monotonic() - queued.enqueued_at) * 1000)
                LOGGER.info(
                    "channel_event=message_dequeued trace_id=%s message_id_hash=%s queue_wait_ms=%s result=ok",
                    _short_hash(queued.message.message_id),
                    _short_hash(queued.message.message_id),
                    queue_wait_ms,
                )
                try:
                    await self._bridge.handle_message(queued.message)
                except Exception:
                    LOGGER.error(
                        "channel_event=handler_failed trace_id=%s message_id_hash=%s result=failed error_code=MESSAGE_HANDLER_FAILURE",
                        _short_hash(queued.message.message_id),
                        _short_hash(queued.message.message_id),
                    )
            finally:
                if turn_acquired:
                    async with order.condition:
                        order.next_to_run += 1
                        order.pending -= 1
                        order.condition.notify_all()
                    if order.pending == 0:
                        self._conversation_orders.pop(queued.order_key, None)
                self._queue.task_done()

    async def _renew_leader(self) -> None:
        while True:
            await asyncio.sleep(self._state_store.leader_renew_interval_seconds)
            owner_token = self._owner_token
            if owner_token is None:
                return
            try:
                renewed = await self._state_store.renew_leader(owner_token, lease_name=self._provider_name)
            except Exception:
                renewed = False
            if not renewed:
                LOGGER.error("channel_event=leader_lost result=failed error_code=LEADER_LEASE_LOST")
                self._request_stop("LEADER_LEASE_LOST")
                return

    async def _monitor_channel(self) -> None:
        await asyncio.sleep(_CHANNEL_MONITOR_INTERVAL_SECONDS)
        while True:
            if not self._channel.is_running:
                LOGGER.error("channel_event=ws_stopped result=failed error_code=FEISHU_WS_STOPPED")
                self._request_stop("FEISHU_WS_STOPPED")
                return
            await asyncio.sleep(_CHANNEL_MONITOR_INTERVAL_SECONDS)

    def _request_stop(self, error_code: str) -> None:
        self._runtime_error_code = error_code
        self._accepting_messages = False
        if self._stop_event is not None:
            self._stop_event.set()


def _build_redis(config: AppConfig) -> Redis:
    host, port = _redis_host_port(config.redis.host)
    return Redis(
        host=host,
        port=port,
        db=config.redis.db,
        username=config.redis.username or None,
        password=config.redis.password or None,
        decode_responses=True,
        socket_connect_timeout=_REDIS_CONNECT_TIMEOUT_SECONDS,
        socket_timeout=_REDIS_OPERATION_TIMEOUT_SECONDS,
    )


def _build_worker(app_config: AppConfig, channel_config: FeishuChannelConfig) -> ChannelWorker:
    """Build the demo runner, which stays Feishu-only by design.

    Demo mode reads one fixed published Agent out of environment variables, so it
    has no binding, no revision guard and no encrypted credential. Imports stay
    local to keep the transport SDK out of the managed code path.
    """

    from api.channels.feishu.channel import FeishuAccount, FeishuChannel

    if not channel_config.enabled:
        raise ChannelWorkerError("FEISHU_CHANNEL_DISABLED")

    redis = _build_redis(app_config)
    state_store = RedisChannelStateStore(
        redis,
        # Demo mode has no binding, so it namespaces by account -- and says so,
        # which also keeps it from ever colliding with a managed runner.
        scope=("demo", "feishu", channel_config.app_id),
        dedupe_ttl_seconds=channel_config.dedupe_ttl_seconds,
        session_ttl_seconds=channel_config.session_ttl_seconds,
        leader_ttl_seconds=channel_config.leader_ttl_seconds,
        leader_renew_interval_seconds=channel_config.leader_renew_seconds,
    )
    account_id = _short_hash(channel_config.app_id)
    channel = FeishuChannel(
        FeishuAccount(
            account_id=account_id,
            app_id=channel_config.app_id,
            app_secret=channel_config.app_secret.get_secret_value(),
            domain=channel_config.domain,
        )
    )
    agent_client = MultiRAGAgentClient(
        base_url=channel_config.multirag_base_url,
        agent_id=channel_config.agent_id,
        api_token=channel_config.agent_api_token.get_secret_value(),
        # Pydantic stores these PositiveInt settings as ints while the HTTP
        # boundary intentionally accepts seconds as floats. Cast explicitly so
        # the repository's Beartype import hook sees the declared runtime type.
        connect_timeout_seconds=float(channel_config.connect_timeout_seconds),
        total_timeout_seconds=float(channel_config.total_timeout_seconds),
        max_answer_chars=channel_config.max_answer_chars,
    )
    bridge = FeishuAgentBridge(
        channel=channel,
        executor=agent_client,
        state_store=state_store,
        app_id=channel_config.app_id,
        agent_id=channel_config.agent_id,
        release_marker=channel_config.release_marker,
        allowed_open_ids=set(channel_config.allowed_open_ids),
        max_question_chars=channel_config.max_question_chars,
    )
    return ChannelWorker(
        provider_name="feishu",
        channel=channel,
        bridge=bridge,
        agent_client=agent_client,
        state_store=state_store,
        redis=redis,
        queue_size=channel_config.queue_size,
        worker_concurrency=channel_config.worker_concurrency,
    )


async def _run_managed_channel(
    *,
    app_config: AppConfig,
    provider_name: str,
    binding_id: str,
    binding_generation: int,
    stop_event: asyncio.Event,
) -> None:
    provider = worker_provider(provider_name)
    control_config = app_config.channels.control
    base_url = control_config.runtime_api_base_url.strip()
    token = control_config.internal_api_token.get_secret_value()
    if not base_url or not token:
        raise ChannelWorkerError("CHANNEL_RUNTIME_CONTROL_NOT_CONFIGURED")

    runner_id = f"{socket.gethostname()}-{os.getpid()}"
    runtime_client = ChannelRuntimeClient(
        base_url=base_url,
        api_token=token,
        runner_id=runner_id,
        binding_id=binding_id,
        binding_generation=binding_generation,
    )
    worker: ChannelWorker | None = None
    redis: Redis | None = None
    execution_client: MultiRAGBindingExecutionClient | None = None
    heartbeat_task: asyncio.Task[None] | None = None
    generation = 0
    try:
        runtime = await runtime_client.fetch_binding(binding_id)
        if runtime.binding_id != binding_id or runtime.generation != binding_generation or runtime.provider != provider.name:
            raise ChannelWorkerError("CHANNEL_RUNTIME_BINDING_INVALID")
        generation = runtime.generation

        # The provider owns its credential shape, its account rules and its
        # tuning section; everything below is transport-agnostic.
        tuning = provider.tuning(app_config)
        plan = provider.build_managed(credential=runtime.credential, public_config=runtime.public_config)
        redis = _build_redis(app_config)
        state_store = RedisChannelStateStore(
            redis,
            # Per binding, not per provider account: the lease is taken before
            # the credential is verified, so an account-scoped namespace let one
            # tenant squat on another tenant's account and block its worker from
            # restarting. binding_id is already tenant-scoped and needs nothing
            # added to the private runtime contract.
            scope=("binding", binding_id),
            dedupe_ttl_seconds=tuning.dedupe_ttl_seconds,
            session_ttl_seconds=tuning.session_ttl_seconds,
            leader_ttl_seconds=tuning.leader_ttl_seconds,
            leader_renew_interval_seconds=tuning.leader_renew_seconds,
        )
        channel = plan.channel
        execution_client = MultiRAGBindingExecutionClient(
            base_url=base_url,
            binding_id=binding_id,
            binding_generation=binding_generation,
            api_token=token,
            total_timeout_seconds=tuning.total_timeout_seconds,
            max_answer_chars=tuning.max_answer_chars,
        )
        bridge = FeishuBindingBridge(
            channel=channel,
            executor=execution_client,
            state_store=state_store,
            binding_id=binding_id,
            allowed_open_ids=set(plan.allowed_sender_ids),
            max_question_chars=tuning.max_question_chars,
        )
        worker = ChannelWorker(
            provider_name=provider.name,
            channel=channel,
            bridge=bridge,
            agent_client=execution_client,
            state_store=state_store,
            redis=redis,
            queue_size=tuning.queue_size,
            worker_concurrency=tuning.worker_concurrency,
        )
        await _safe_runtime_report(
            runtime_client,
            binding_id=binding_id,
            generation=generation,
            state="starting",
        )
        heartbeat_task = asyncio.create_task(
            _runtime_heartbeat(
                runtime_client=runtime_client,
                channel=channel,
                binding_id=binding_id,
                generation=generation,
                interval_seconds=control_config.runtime_heartbeat_seconds,
            ),
            name=f"{provider.name}-channel-runtime-heartbeat",
        )
        await worker.run(stop_event)
    except ChannelWorkerError as exc:
        if generation:
            await _safe_runtime_report(
                runtime_client,
                binding_id=binding_id,
                generation=generation,
                state="error",
                error_code=str(exc),
            )
        raise
    except Exception as exc:
        if generation:
            await _safe_runtime_report(
                runtime_client,
                binding_id=binding_id,
                generation=generation,
                state="error",
                error_code="MANAGED_WORKER_FAILURE",
            )
        raise ChannelWorkerError("MANAGED_WORKER_FAILURE") from exc
    else:
        await _safe_runtime_report(
            runtime_client,
            binding_id=binding_id,
            generation=generation,
            state="stopped",
        )
    finally:
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)
        # worker.run owns execution_client and Redis cleanup. Failures before
        # worker construction have no such resources.
        if worker is None:
            if execution_client is not None:
                await execution_client.close()
            if redis is not None:
                await redis.aclose()
            LOGGER.info(
                "channel_event=managed_worker_not_started binding_id_hash=%s result=failed",
                _short_hash(binding_id),
            )
        await runtime_client.close()


async def _runtime_heartbeat(
    *,
    runtime_client: ChannelRuntimeClient,
    channel: WorkerChannel,
    binding_id: str,
    generation: int,
    interval_seconds: int,
) -> None:
    connected_at = None
    while True:
        if channel.is_running:
            if connected_at is None:
                connected_at = datetime.now(UTC)
            await _safe_runtime_report(
                runtime_client,
                binding_id=binding_id,
                generation=generation,
                state="connected",
                connected_at=connected_at,
            )
            await asyncio.sleep(interval_seconds)
        else:
            await asyncio.sleep(0.2)


async def _safe_runtime_report(
    runtime_client: ChannelRuntimeClient,
    *,
    binding_id: str,
    generation: int,
    state: RuntimeState,
    connected_at: datetime | None = None,
    error_code: str | None = None,
) -> None:
    try:
        await runtime_client.report(
            binding_id=binding_id,
            generation=generation,
            state=state,
            connected_at=connected_at,
            error_code=error_code,
        )
    except Exception:
        LOGGER.warning(
            "channel_event=runtime_report_failed binding_id_hash=%s state=%s result=failed",
            _short_hash(binding_id),
            state,
        )


def _install_signal_handlers(stop_event: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for handled_signal in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(handled_signal, stop_event.set)
        except NotImplementedError:
            signal.signal(
                handled_signal,
                lambda *_args, event=stop_event: loop.call_soon_threadsafe(event.set),
            )


async def _run_channel(
    channel_name: str,
    *,
    binding_id: str | None = None,
    binding_generation: int | None = None,
) -> None:
    if channel_name not in supported_provider_names():
        raise ChannelWorkerError("CHANNEL_NOT_SUPPORTED")
    app_config = get_app_config()
    stop_event = asyncio.Event()
    _install_signal_handlers(stop_event)
    if binding_id is not None:
        if binding_generation is None:
            raise ChannelWorkerError("CHANNEL_RUNTIME_BINDING_INVALID")
        await _run_managed_channel(
            app_config=app_config,
            provider_name=channel_name,
            binding_id=binding_id,
            binding_generation=binding_generation,
            stop_event=stop_event,
        )
        return
    # Demo mode has no binding, so it stays on the Feishu-only env config.
    if channel_name != "feishu":
        raise ChannelWorkerError("CHANNEL_DEMO_MODE_UNSUPPORTED")
    worker = _build_worker(app_config, app_config.channels.feishu)
    await worker.run(stop_event)


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one external MultiRAG messaging channel")
    parser.add_argument("--channel", choices=list(supported_provider_names()), required=True)
    parser.add_argument("--binding-id")
    parser.add_argument("--binding-generation", type=int)
    args = parser.parse_args(argv)
    if (args.binding_id is None) != (args.binding_generation is None):
        parser.error("--binding-id and --binding-generation must be provided together")
    if args.binding_generation is not None and args.binding_generation < 1:
        parser.error("--binding-generation must be positive")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    try:
        # Keep the repository's unified bootstrap entry point, but this
        # transport process only needs typed config + observability. Agent data
        # access remains behind the HTTP API; local doc-store/storage resources
        # would add unrelated startup dependencies.
        ensure_initialized(initialize_resources=False)
        if args.binding_id:
            asyncio.run(
                _run_channel(
                    args.channel,
                    binding_id=args.binding_id,
                    binding_generation=args.binding_generation,
                )
            )
        else:
            asyncio.run(_run_channel(args.channel))
    except KeyboardInterrupt:
        return 0
    except AppConfigError as exc:
        LOGGER.error(
            "channel_event=worker_failed result=failed error_code=CHANNEL_CONFIG_INVALID detail=%s",
            exc,
        )
        return 1
    except ChannelWorkerError as exc:
        LOGGER.error("channel_event=worker_failed result=failed error_code=%s", exc)
        return 1
    except Exception:
        LOGGER.error("channel_event=worker_failed result=failed error_code=UNEXPECTED_WORKER_FAILURE")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
