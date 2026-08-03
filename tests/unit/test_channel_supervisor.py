from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from api.channel_runtime.schemas import DesiredRuntime
from api.channels import supervisor as supervisor_module
from api.channels.runtime_client import ChannelRuntimeClientError
from api.channels.supervisor import ChannelRuntimeSupervisor


class _FakeRuntimeClient:
    def __init__(
        self,
        responses: list[list[DesiredRuntime] | Exception] | None = None,
        *,
        on_call: Callable[[int], None] | None = None,
    ) -> None:
        self.responses = responses or []
        self.on_call = on_call
        self.calls = 0
        self.closed = False

    async def list_desired(self) -> list[DesiredRuntime]:
        self.calls += 1
        if self.on_call is not None:
            self.on_call(self.calls)
        response = self.responses.pop(0) if self.responses else []
        if isinstance(response, Exception):
            raise response
        return response

    async def close(self) -> None:
        self.closed = True


class _FakeProcess:
    def __init__(self, *, returncode: int | None = None) -> None:
        self.returncode = returncode
        self.terminated = False
        self.killed = False
        self.wait_calls = 0

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        self.wait_calls += 1
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


def _desired(binding_id: str, *, generation: int = 1) -> DesiredRuntime:
    return DesiredRuntime(binding_id=binding_id, provider="feishu", generation=generation)


def _supervisor(
    client: _FakeRuntimeClient,
    factory: Callable[[DesiredRuntime], Awaitable[Any]],
    *,
    interval: float = 0.01,
) -> ChannelRuntimeSupervisor:
    return ChannelRuntimeSupervisor(
        client=client,
        process_factory=factory,
        reconcile_interval_seconds=interval,
    )


@pytest.mark.asyncio
async def test_run_survives_temporary_control_plane_failure() -> None:
    stop_event = asyncio.Event()
    client = _FakeRuntimeClient(
        [ChannelRuntimeClientError("RUNTIME_API_TIMEOUT"), []],
        on_call=lambda calls: stop_event.set() if calls == 2 else None,
    )

    async def factory(_item: DesiredRuntime) -> _FakeProcess:
        pytest.fail("no worker should be started")

    await _supervisor(client, factory).run(stop_event)

    assert client.calls == 2
    assert client.closed is True


@pytest.mark.asyncio
async def test_spawn_failure_isolated_from_other_bindings(caplog: pytest.LogCaptureFixture) -> None:
    failed_id = "private-binding-one"
    healthy_id = "private-binding-two"
    client = _FakeRuntimeClient([[_desired(failed_id), _desired(healthy_id)]])
    healthy_process = _FakeProcess()
    started: list[str] = []

    async def factory(item: DesiredRuntime) -> _FakeProcess:
        started.append(item.binding_id)
        if item.binding_id == failed_id:
            raise OSError("sensitive operating-system detail")
        return healthy_process

    instance = _supervisor(client, factory)
    with caplog.at_level(logging.INFO):
        await instance.reconcile()

    assert started == [failed_id, healthy_id]
    assert list(instance._running) == [healthy_id]
    assert failed_id not in caplog.text
    assert healthy_id not in caplog.text
    await instance.close()


@pytest.mark.asyncio
async def test_generation_change_stops_then_restarts_worker() -> None:
    binding_id = "generation-binding"
    first = _FakeProcess()
    second = _FakeProcess()
    processes = iter((first, second))
    client = _FakeRuntimeClient(
        [
            [_desired(binding_id, generation=1)],
            [_desired(binding_id, generation=2)],
        ]
    )

    async def factory(_item: DesiredRuntime) -> _FakeProcess:
        return next(processes)

    instance = _supervisor(client, factory)
    await instance.reconcile()
    await instance.reconcile()

    assert first.terminated is True
    assert instance._running[binding_id].process is second
    assert instance._running[binding_id].generation == 2
    await instance.close()


@pytest.mark.asyncio
async def test_removed_or_disabled_binding_stops_worker() -> None:
    binding_id = "disabled-binding"
    process = _FakeProcess()
    client = _FakeRuntimeClient([[_desired(binding_id)], []])

    async def factory(_item: DesiredRuntime) -> _FakeProcess:
        return process

    instance = _supervisor(client, factory)
    await instance.reconcile()
    await instance.reconcile()

    assert process.terminated is True
    assert instance._running == {}
    await instance.close()


@pytest.mark.asyncio
async def test_exited_worker_uses_generation_scoped_exponential_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binding_id = "crashing-binding"
    clock = [100.0]
    monkeypatch.setattr(supervisor_module.time, "monotonic", lambda: clock[0])
    desired = _desired(binding_id)
    client = _FakeRuntimeClient([[desired], [desired], [desired], [desired], [desired]])
    spawned: list[_FakeProcess] = []

    async def factory(_item: DesiredRuntime) -> _FakeProcess:
        process = _FakeProcess()
        spawned.append(process)
        return process

    instance = _supervisor(client, factory)
    await instance.reconcile()
    spawned[0].returncode = 1

    await instance.reconcile()
    assert len(spawned) == 1
    assert instance._failures[(binding_id, 1)] == (1, 102.0)

    clock[0] = 102.0
    await instance.reconcile()
    assert len(spawned) == 2
    spawned[1].returncode = 1

    await instance.reconcile()
    assert len(spawned) == 2
    assert instance._failures[(binding_id, 1)] == (2, 106.0)

    clock[0] = 106.0
    await instance.reconcile()
    assert len(spawned) == 3
    await instance.close()


@pytest.mark.asyncio
async def test_failed_stop_does_not_start_replacement_or_lose_worker() -> None:
    class _UnstoppableProcess(_FakeProcess):
        def terminate(self) -> None:
            raise PermissionError("private process detail")

    binding_id = "unstoppable-binding"
    process = _UnstoppableProcess()
    client = _FakeRuntimeClient(
        [
            [_desired(binding_id, generation=1)],
            [_desired(binding_id, generation=2)],
        ]
    )
    starts = 0

    async def factory(_item: DesiredRuntime) -> _FakeProcess:
        nonlocal starts
        starts += 1
        return process

    instance = _supervisor(client, factory)
    await instance.reconcile()
    await instance.reconcile()

    assert starts == 1
    assert instance._running[binding_id].generation == 1


@pytest.mark.asyncio
async def test_spawn_worker_removes_master_and_demo_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MULTIRAG_CHANNELS__CONTROL__SECRET_ENCRYPTION_KEY", "master-key")
    monkeypatch.setenv("MULTIRAG_CHANNELS__FEISHU__APP_SECRET", "app-secret")
    monkeypatch.setenv("MULTIRAG_CHANNELS__FEISHU__AGENT_API_TOKEN", "agent-token")
    monkeypatch.setenv("MULTIRAG_CHANNELS__FEISHU__ENABLED", "true")
    captured: dict[str, Any] = {}
    process = _FakeProcess()

    async def fake_spawn(*args: str, **kwargs: Any) -> _FakeProcess:
        captured["args"] = args
        captured.update(kwargs)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
    item = _desired("managed-binding")

    assert await supervisor_module._spawn_worker(item) is process

    child_env = captured["env"]
    assert "MULTIRAG_CHANNELS__CONTROL__SECRET_ENCRYPTION_KEY" not in child_env
    assert "MULTIRAG_CHANNELS__FEISHU__APP_SECRET" not in child_env
    assert "MULTIRAG_CHANNELS__FEISHU__AGENT_API_TOKEN" not in child_env
    assert child_env["MULTIRAG_CHANNELS__FEISHU__ENABLED"] == "false"
    assert "managed-binding" in captured["args"]


@pytest.mark.asyncio
async def test_signal_handlers_use_event_loop_registration(monkeypatch: pytest.MonkeyPatch) -> None:
    event = asyncio.Event()
    callbacks: dict[signal.Signals, Callable[[], None]] = {}

    class _Loop:
        def add_signal_handler(self, sig: signal.Signals, callback: Callable[[], None]) -> None:
            callbacks[sig] = callback

    monkeypatch.setattr(asyncio, "get_running_loop", lambda: _Loop())
    supervisor_module._install_signal_handlers(event)

    assert set(callbacks) == {signal.SIGINT, signal.SIGTERM}
    callbacks[signal.SIGTERM]()
    assert event.is_set()


@pytest.mark.asyncio
async def test_signal_handlers_fall_back_on_windows_style_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    event = asyncio.Event()
    registered: dict[signal.Signals, Callable[..., None]] = {}

    class _Loop:
        def add_signal_handler(self, _sig: signal.Signals, _callback: Callable[[], None]) -> None:
            raise NotImplementedError

        def call_soon_threadsafe(self, callback: Callable[[], None]) -> None:
            callback()

    monkeypatch.setattr(asyncio, "get_running_loop", lambda: _Loop())
    monkeypatch.setattr(signal, "signal", lambda sig, callback: registered.setdefault(sig, callback))
    supervisor_module._install_signal_handlers(event)

    assert set(registered) == {signal.SIGINT, signal.SIGTERM}
    registered[signal.SIGINT](signal.SIGINT, None)
    assert event.is_set()
