"""Cross-platform process supervisor for database-managed Channel bindings."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import os
import signal
import socket
import sys
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable
from uuid import uuid4

from api.channel_runtime.schemas import DesiredRuntime
from api.channel_runtime.tokens import derive_binding_workload_token
from api.channels.runtime_client import ChannelRuntimeClient, ChannelRuntimeClientError
from common.app_config import AppConfigError, get_app_config
from common.bootstrap import ensure_initialized
from common.file_utils import get_project_base_directory

LOGGER = logging.getLogger(__name__)
_SECRET_KEY_ENV = "MULTIRAG_CHANNELS__CONTROL__SECRET_ENCRYPTION_KEY"
_CONTROL_TOKEN_ENV = "MULTIRAG_CHANNELS__CONTROL__INTERNAL_API_TOKEN"
_DEMO_ENABLED_ENV = "MULTIRAG_CHANNELS__FEISHU__ENABLED"
_DEMO_SECRET_ENVS = (
    "MULTIRAG_CHANNELS__FEISHU__APP_SECRET",
    "MULTIRAG_CHANNELS__FEISHU__AGENT_API_TOKEN",
)
_SUPPORTED_PROVIDERS = frozenset({"feishu"})
_STOP_TIMEOUT_SECONDS = 10


@dataclass(slots=True)
class _RunningWorker:
    process: WorkerProcess
    generation: int
    provider: str


@runtime_checkable
class RuntimeControlClient(Protocol):
    """Minimal supervisor-facing control-plane client contract."""

    async def list_desired(self) -> list[DesiredRuntime]: ...

    async def close(self) -> None: ...


@runtime_checkable
class WorkerProcess(Protocol):
    """Cross-platform subprocess operations used by the reconciler."""

    @property
    def returncode(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    async def wait(self) -> int: ...


ProcessFactory = Callable[[DesiredRuntime], Awaitable[WorkerProcess]]


class ChannelSupervisorError(RuntimeError):
    """A safe startup/reconciliation error."""


class ChannelRuntimeSupervisor:
    """Reconcile desired bindings into one isolated process per account."""

    def __init__(
        self,
        *,
        client: RuntimeControlClient,
        process_factory: ProcessFactory,
        reconcile_interval_seconds: float,
    ) -> None:
        self._client = client
        self._process_factory = process_factory
        self._interval = reconcile_interval_seconds
        self._running: dict[str, _RunningWorker] = {}
        self._failures: dict[tuple[str, int], tuple[int, float]] = {}

    async def run(self, stop_event: asyncio.Event) -> None:
        try:
            while not stop_event.is_set():
                try:
                    await self.reconcile()
                except ChannelRuntimeClientError as exc:
                    LOGGER.warning(
                        "channel_supervisor_event=reconcile_failed result=failed error_code=%s",
                        exc.code,
                    )
                except Exception:
                    LOGGER.error("channel_supervisor_event=reconcile_failed result=failed error_code=UNEXPECTED_RECONCILE_FAILURE")
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=self._interval)
                except TimeoutError:
                    pass
        finally:
            await self.close()

    async def reconcile(self) -> None:
        desired_items = await self._client.list_desired()
        desired = {item.binding_id: item for item in desired_items}

        for binding_id, running in list(self._running.items()):
            item = desired.get(binding_id)
            changed = item is not None and (item.generation != running.generation or item.provider != running.provider)
            exited = running.process.returncode is not None
            if item is None or changed or exited:
                await self._stop_one(binding_id, terminate=not exited)
                if exited and item is not None and not changed:
                    self._record_failure(item)

        now = time.monotonic()
        for binding_id, item in desired.items():
            if binding_id in self._running:
                continue
            if item.provider not in _SUPPORTED_PROVIDERS:
                LOGGER.error(
                    "channel_supervisor_event=provider_unsupported binding_id_hash=%s provider=%s",
                    _short_hash(binding_id),
                    item.provider,
                )
                continue
            failure = self._failures.get((binding_id, item.generation))
            if failure is not None and now < failure[1]:
                continue
            try:
                process = await self._process_factory(item)
            except Exception:
                self._record_failure(item)
                LOGGER.error(
                    "channel_supervisor_event=worker_start_failed binding_id_hash=%s provider=%s generation=%s result=failed",
                    _short_hash(binding_id),
                    item.provider,
                    item.generation,
                )
                continue
            self._running[binding_id] = _RunningWorker(
                process=process,
                generation=item.generation,
                provider=item.provider,
            )
            LOGGER.info(
                "channel_supervisor_event=worker_started binding_id_hash=%s provider=%s generation=%s result=ok",
                _short_hash(binding_id),
                item.provider,
                item.generation,
            )

        active_keys = {(item.binding_id, item.generation) for item in desired_items}
        self._failures = {key: value for key, value in self._failures.items() if key in active_keys}

    async def close(self) -> None:
        for binding_id in list(self._running):
            await self._stop_one(binding_id, terminate=True)
        await self._client.close()

    def _record_failure(self, item: DesiredRuntime) -> None:
        key = (item.binding_id, item.generation)
        count, _retry_at = self._failures.get(key, (0, 0.0))
        next_count = count + 1
        delay = min(60, 2 ** min(next_count, 6))
        self._failures[key] = (next_count, time.monotonic() + delay)
        LOGGER.warning(
            "channel_supervisor_event=worker_exited binding_id_hash=%s generation=%s retry_seconds=%s result=failed",
            _short_hash(item.binding_id),
            item.generation,
            delay,
        )

    async def _stop_one(self, binding_id: str, *, terminate: bool) -> bool:
        running = self._running.get(binding_id)
        if running is None:
            return True
        process = running.process
        try:
            if terminate and process.returncode is None:
                process.terminate()
            if process.returncode is None:
                await asyncio.wait_for(process.wait(), timeout=_STOP_TIMEOUT_SECONDS)
        except TimeoutError:
            try:
                process.kill()
                await process.wait()
            except ProcessLookupError:
                pass
            except Exception:
                LOGGER.error(
                    "channel_supervisor_event=worker_stop_failed binding_id_hash=%s result=failed error_code=WORKER_KILL_FAILED",
                    _short_hash(binding_id),
                )
                return False
        except ProcessLookupError:
            # The child exited between checking ``returncode`` and signalling
            # it. Treat that race as a successful stop.
            pass
        except Exception:
            LOGGER.error(
                "channel_supervisor_event=worker_stop_failed binding_id_hash=%s result=failed error_code=WORKER_STOP_FAILED",
                _short_hash(binding_id),
            )
            return False
        self._running.pop(binding_id, None)
        LOGGER.info(
            "channel_supervisor_event=worker_stopped binding_id_hash=%s result=ok",
            _short_hash(binding_id),
        )
        return True


def _short_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


async def _spawn_worker(item: DesiredRuntime) -> WorkerProcess:
    child_env = os.environ.copy()
    # The worker obtains one decrypted provider credential through the private
    # API and never needs the database master encryption key.
    child_env.pop(_SECRET_KEY_ENV, None)
    child_env[_DEMO_ENABLED_ENV] = "false"
    for env_name in _DEMO_SECRET_ENVS:
        child_env.pop(env_name, None)
    master_token = get_app_config().channels.control.internal_api_token.get_secret_value()
    if not master_token:
        raise ChannelSupervisorError("CHANNEL_RUNTIME_CONTROL_NOT_CONFIGURED")
    child_env[_CONTROL_TOKEN_ENV] = derive_binding_workload_token(
        master_token,
        binding_id=item.binding_id,
        generation=item.generation,
    )
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "api.channels.worker",
        "--channel",
        item.provider,
        "--binding-id",
        item.binding_id,
        "--binding-generation",
        str(item.generation),
        cwd=get_project_base_directory(),
        env=child_env,
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


async def _run_supervisor() -> None:
    config = get_app_config().channels.control
    base_url = config.runtime_api_base_url.strip()
    token = config.internal_api_token.get_secret_value()
    if not base_url or not token:
        raise ChannelSupervisorError("CHANNEL_RUNTIME_CONTROL_NOT_CONFIGURED")
    runner_id = f"{socket.gethostname()}-{uuid4().hex[:12]}"
    client = ChannelRuntimeClient(
        base_url=base_url,
        api_token=token,
        runner_id=runner_id,
    )
    supervisor = ChannelRuntimeSupervisor(
        client=client,
        process_factory=_spawn_worker,
        # Pydantic exposes the configured PositiveInt as ``int`` while the
        # reconciler deliberately accepts sub-second floats in tests and
        # embeddings.  Normalize at this boundary for strict beartype runtime
        # checking.
        reconcile_interval_seconds=float(config.reconcile_interval_seconds),
    )
    stop_event = asyncio.Event()
    _install_signal_handlers(stop_event)
    await supervisor.run(stop_event)


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the independent MultiRAG Channel supervisor")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    _parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    try:
        ensure_initialized(initialize_resources=False)
        asyncio.run(_run_supervisor())
    except KeyboardInterrupt:
        return 0
    except (AppConfigError, ChannelSupervisorError, ChannelRuntimeClientError) as exc:
        LOGGER.error("channel_supervisor_event=failed result=failed error_code=%s", exc)
        return 1
    except Exception:
        LOGGER.error("channel_supervisor_event=failed result=failed error_code=UNEXPECTED_SUPERVISOR_FAILURE")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
