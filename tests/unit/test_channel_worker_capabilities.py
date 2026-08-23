"""Managed-worker assembly contracts for startup capability negotiation."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from api.channel_capabilities import EffectiveReplyCapabilities
from api.channel_runtime.schemas import RuntimeBindingConfig, RuntimeCredential
from api.channels import worker as worker_module
from api.channels.provider import WorkerTuning
from api.channels.runtime_client import ChannelRuntimeClientError
from common.app_config import AppConfig


@pytest.mark.parametrize("preflight_available", [True, False])
async def test_managed_worker_fetches_capabilities_once_and_passes_them_only_to_bridge(
    monkeypatch: pytest.MonkeyPatch,
    preflight_available: bool,
) -> None:
    negotiated = EffectiveReplyCapabilities(
        progressive_reply=True,
        cancel_queued=True,
        cancel_running=False,
        regenerate=True,
        retry=False,
        feedback=True,
    )
    runtime = RuntimeBindingConfig(
        binding_id="binding-1",
        provider="feishu",
        generation=7,
        public_config={},
        credential=RuntimeCredential(fields={"app_id": "app-1", "app_secret": "secret"}),
    )
    fetch_calls = 0

    class _RuntimeClient:
        def __init__(self, **_kwargs: object) -> None:
            return None

        async def fetch_binding(self, _binding_id: str) -> RuntimeBindingConfig:
            return runtime

        async def fetch_execution_capabilities(self, _binding_id: str) -> EffectiveReplyCapabilities:
            nonlocal fetch_calls
            fetch_calls += 1
            if not preflight_available:
                raise ChannelRuntimeClientError("RUNTIME_API_HTTP_404")
            return negotiated

        async def close(self) -> None:
            return None

    channel = SimpleNamespace(is_running=False)
    tuning = WorkerTuning(
        queue_size=10,
        followup_queue_size=5,
        worker_concurrency=1,
        dedupe_ttl_seconds=60,
        session_ttl_seconds=60,
        leader_ttl_seconds=60,
        leader_renew_seconds=20,
        max_question_chars=4000,
        max_answer_chars=8000,
        total_timeout_seconds=120.0,
    )
    provider = SimpleNamespace(
        name="feishu",
        tuning=lambda _config: tuning,
        build_managed=lambda **_kwargs: SimpleNamespace(
            channel=channel,
            allowed_sender_ids=frozenset(),
        ),
    )
    execution_client_kwargs: dict[str, object] = {}

    class _ExecutionClient:
        def __init__(self, **kwargs: object) -> None:
            execution_client_kwargs.update(kwargs)

        async def close(self) -> None:
            return None

    bridge_kwargs: dict[str, object] = {}

    class _Bridge:
        def __init__(self, **kwargs: object) -> None:
            bridge_kwargs.update(kwargs)

    class _Worker:
        def __init__(self, **_kwargs: object) -> None:
            return None

        async def run(self, _stop_event: asyncio.Event) -> None:
            return None

    class _Redis:
        async def aclose(self) -> None:
            return None

    async def _report(*_args: object, **_kwargs: object) -> None:
        return None

    async def _heartbeat(**_kwargs: object) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(worker_module, "ChannelRuntimeClient", _RuntimeClient)
    monkeypatch.setattr(worker_module, "worker_provider", lambda _name: provider)
    monkeypatch.setattr(
        worker_module,
        "provider_spec",
        lambda _name: SimpleNamespace(capabilities=SimpleNamespace(group_chat=False)),
    )
    monkeypatch.setattr(worker_module, "_build_redis", lambda _config: _Redis())
    monkeypatch.setattr(worker_module, "RedisChannelStateStore", lambda *_args, **_kwargs: SimpleNamespace())
    monkeypatch.setattr(worker_module, "MultiRAGBindingExecutionClient", _ExecutionClient)
    monkeypatch.setattr(worker_module, "BindingBridge", _Bridge)
    monkeypatch.setattr(worker_module, "ChannelWorker", _Worker)
    monkeypatch.setattr(worker_module, "_safe_runtime_report", _report)
    monkeypatch.setattr(worker_module, "_runtime_heartbeat", _heartbeat)

    app_config = AppConfig.model_construct(
        channels=SimpleNamespace(
            control=SimpleNamespace(
                runtime_api_base_url="http://multirag.local",
                internal_api_token=SecretStr("runtime-token"),
                runtime_heartbeat_seconds=30,
            )
        ),
        identity=SimpleNamespace(
            mcp_interactions=SimpleNamespace(enabled=False),
        ),
        redis=SimpleNamespace(),
    )

    await worker_module._run_managed_channel(
        app_config=app_config,
        provider_name="feishu",
        binding_id="binding-1",
        binding_generation=7,
        stop_event=asyncio.Event(),
    )

    assert fetch_calls == 1
    assert "capabilities" not in execution_client_kwargs
    assert bridge_kwargs["capabilities"] == (negotiated if preflight_available else EffectiveReplyCapabilities())
    assert bridge_kwargs["interaction_client"] is None
    assert bridge_kwargs["interaction_presenter"] is None


async def test_managed_worker_requires_full_feishu_transport_when_interactions_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = RuntimeBindingConfig(
        binding_id="binding-1",
        provider="feishu",
        generation=7,
        public_config={},
        credential=RuntimeCredential(
            fields={"app_id": "app-1", "app_secret": "secret"},
        ),
    )

    class _RuntimeClient:
        def __init__(self, **_kwargs: object) -> None:
            return None

        async def fetch_binding(self, _binding_id: str) -> RuntimeBindingConfig:
            return runtime

        async def fetch_execution_capabilities(
            self,
            _binding_id: str,
        ) -> EffectiveReplyCapabilities:
            return EffectiveReplyCapabilities()

        async def close(self) -> None:
            return None

    tuning = WorkerTuning(
        queue_size=10,
        followup_queue_size=5,
        worker_concurrency=1,
        dedupe_ttl_seconds=60,
        session_ttl_seconds=60,
        leader_ttl_seconds=60,
        leader_renew_seconds=20,
        max_question_chars=4000,
        max_answer_chars=8000,
        total_timeout_seconds=120.0,
    )
    provider = SimpleNamespace(
        name="feishu",
        tuning=lambda _config: tuning,
        build_managed=lambda **_kwargs: SimpleNamespace(
            channel=SimpleNamespace(is_running=False),
            allowed_sender_ids=frozenset(),
        ),
    )

    class _Redis:
        async def aclose(self) -> None:
            return None

    class _ExecutionClient:
        def __init__(self, **_kwargs: object) -> None:
            return None

        async def close(self) -> None:
            return None

    async def _report(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(worker_module, "ChannelRuntimeClient", _RuntimeClient)
    monkeypatch.setattr(worker_module, "worker_provider", lambda _name: provider)
    monkeypatch.setattr(worker_module, "_build_redis", lambda _config: _Redis())
    monkeypatch.setattr(
        worker_module,
        "RedisChannelStateStore",
        lambda *_args, **_kwargs: SimpleNamespace(),
    )
    monkeypatch.setattr(
        worker_module,
        "MultiRAGBindingExecutionClient",
        _ExecutionClient,
    )
    monkeypatch.setattr(worker_module, "_safe_runtime_report", _report)

    app_config = AppConfig.model_construct(
        channels=SimpleNamespace(
            control=SimpleNamespace(
                runtime_api_base_url="http://multirag.local",
                internal_api_token=SecretStr("runtime-token"),
                runtime_heartbeat_seconds=30,
            )
        ),
        identity=SimpleNamespace(
            mcp_interactions=SimpleNamespace(enabled=True),
        ),
        redis=SimpleNamespace(),
    )

    with pytest.raises(
        worker_module.ChannelWorkerError,
        match="CHANNEL_INTERACTION_TRANSPORT_INVALID",
    ):
        await worker_module._run_managed_channel(
            app_config=app_config,
            provider_name="feishu",
            binding_id="binding-1",
            binding_generation=7,
            stop_event=asyncio.Event(),
        )
