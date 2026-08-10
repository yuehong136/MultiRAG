"""Tests for concrete Channel execution boundary adapters."""

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from api.channel_execution.adapters import RedisChannelExecutionStateStore, SqlAlchemyBindingResolver
from api.channel_execution.errors import TargetExecutionFailedError, TargetRevisionUnavailableError
from api.channel_execution.executors import (
    SqlAlchemyCanvasTargetDriver,
    SqlAlchemyDialogTargetDriver,
)
from api.channel_execution.models import (
    ChannelActor,
    ChannelExecutionCommand,
    ChannelMessage,
    ExecutionOperation,
    ExecutionTargetRef,
    WorkloadIdentity,
)
from api.channel_execution.session_models import PreparedCanvasExecution, PreparedDialogExecution


class FakeCanvasHistoryTransaction:
    def __init__(self) -> None:
        self.prepared: list[tuple[str, str | None, str, ExecutionOperation]] = []
        self.completed: list[str] = []
        self.aborted: list[str | None] = []

    async def prepare(
        self,
        *,
        target_id: str,
        session_id: str | None,
        question: str,
        operation: ExecutionOperation,
    ) -> PreparedCanvasExecution:
        self.prepared.append((target_id, session_id, question, operation))
        return PreparedCanvasExecution(
            session_id,
            "candidate-canvas" if session_id else None,
            "source-fingerprint" if session_id else None,
        )

    async def commit(
        self,
        prepared: PreparedCanvasExecution,
        generated_session_id: str,
    ) -> str:
        self.completed.append(generated_session_id)
        return prepared.public_session_id or generated_session_id

    async def abort(
        self,
        prepared: PreparedCanvasExecution,
        generated_session_id: str | None,
    ) -> None:
        del prepared
        self.aborted.append(generated_session_id)


class FakeDialogHistoryTransaction:
    def __init__(self) -> None:
        self.prepared: list[tuple[str, str | None, str, ExecutionOperation]] = []
        self.completed: list[tuple[str, bool]] = []
        self.aborted: list[str | None] = []

    async def prepare(
        self,
        *,
        target_id: str,
        session_id: str | None,
        question: str,
        operation: ExecutionOperation,
    ) -> PreparedDialogExecution:
        self.prepared.append((target_id, session_id, question, operation))
        return PreparedDialogExecution(
            session_id,
            "candidate-dialog" if session_id else None,
            "source-fingerprint" if session_id else None,
            session_id is not None,
        )

    async def commit(
        self,
        prepared: PreparedDialogExecution,
        generated_session_id: str,
    ) -> str:
        self.completed.append((generated_session_id, prepared.require_visible_answer))
        return prepared.public_session_id or generated_session_id

    async def abort(
        self,
        prepared: PreparedDialogExecution,
        generated_session_id: str | None,
    ) -> None:
        del prepared
        self.aborted.append(generated_session_id)


class FakeRepository:
    def __init__(self, bundle: tuple[Any, Any, Any] | None) -> None:
        self.bundle = bundle
        self.seen_binding_id = ""

    async def get_runtime_binding(self, binding_id: str, *, for_update: bool = False):
        assert for_update is False
        self.seen_binding_id = binding_id
        return self.bundle


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.calls: list[tuple[str, str, int | None, bool]] = []

    async def set(
        self,
        name: str,
        value: str,
        *,
        ex: int | None = None,
        nx: bool = False,
    ) -> bool:
        self.calls.append((name, value, ex, nx))
        if nx and name in self.values:
            return False
        self.values[name] = value
        return True

    async def get(self, name: str) -> str | None:
        return self.values.get(name)

    async def delete(self, *names: str) -> int:
        removed = 0
        for name in names:
            removed += int(self.values.pop(name, None) is not None)
        return removed


def _command(provider: str = "feishu") -> ChannelExecutionCommand:
    return ChannelExecutionCommand(
        event_id="event-raw",
        conversation_key="conversation-raw",
        message=ChannelMessage(content="hello"),
        actor=ChannelActor(
            provider=provider,
            subject="sender-raw",
            conversation="chat-raw",
        ),
    )


def _workload(*, binding_id: str = "binding-1", generation: int = 3) -> WorkloadIdentity:
    return WorkloadIdentity(
        subject="multirag-channel-runtime",
        binding_id=binding_id,
        binding_generation=generation,
    )


@pytest.mark.asyncio
async def test_binding_resolver_uses_only_server_owned_target_and_tenant() -> None:
    channel = SimpleNamespace(id="channel-1", tenant_id="tenant-trusted", channel="feishu", status=1)
    binding = SimpleNamespace(
        id="binding-1",
        channel_id="channel-1",
        target_type="multirag.canvas_agent",
        target_id="agent-trusted",
        target_revision_id="revision-trusted",
        enabled=True,
        generation=3,
        policy={},
    )
    resolver = SqlAlchemyBindingResolver(FakeRepository((channel, binding, None)))  # type: ignore[arg-type]

    context = await resolver.resolve(
        binding_id="binding-1",
        workload=_workload(),
        command=_command(),
    )

    assert context is not None
    assert context.tenant_id == "tenant-trusted"
    assert context.target.target_type == "multirag.canvas_agent"
    assert context.target.target_id == "agent-trusted"
    assert context.target.revision_id == "revision-trusted"
    assert context.binding_generation == 3
    assert context.principal_id is None


@pytest.mark.asyncio
async def test_binding_resolver_rejects_provider_mismatch_and_disabled_state() -> None:
    channel = SimpleNamespace(id="channel-1", tenant_id="tenant-1", channel="feishu", status=0)
    binding = SimpleNamespace(
        id="binding-1",
        channel_id="channel-1",
        target_type="multirag.dialog",
        target_id="dialog-1",
        target_revision_id=None,
        enabled=True,
        generation=2,
        policy={},
    )
    resolver = SqlAlchemyBindingResolver(FakeRepository((channel, binding, None)))  # type: ignore[arg-type]

    assert (
        await resolver.resolve(
            binding_id="binding-1",
            workload=_workload(generation=2),
            command=_command(provider="other"),
        )
        is None
    )

    context = await resolver.resolve(
        binding_id="binding-1",
        workload=_workload(generation=2),
        command=_command(),
    )
    assert context is not None
    assert context.enabled is False

    assert (
        await resolver.resolve(
            binding_id="binding-1",
            workload=_workload(generation=1),
            command=_command(),
        )
        is None
    )


@pytest.mark.asyncio
async def test_redis_state_store_is_atomic_opaque_and_persistent() -> None:
    redis = FakeRedis()
    store = RedisChannelExecutionStateStore(redis)

    assert await store.claim(binding_id="binding-raw", event_id="event-raw") is True
    assert await store.claim(binding_id="binding-raw", event_id="event-raw") is False
    await store.put_session(
        binding_id="binding-raw",
        binding_generation=4,
        conversation_key="conversation-raw",
        session_id="session-value",
    )

    assert (
        await store.get_session(
            binding_id="binding-raw",
            binding_generation=4,
            conversation_key="conversation-raw",
        )
        == "session-value"
    )
    assert all("binding-raw" not in key for key in redis.values)
    assert all("event-raw" not in key for key in redis.values)
    assert all("conversation-raw" not in key for key in redis.values)

    await store.complete(binding_id="binding-raw", event_id="event-raw")
    assert any(value == "completed" for value in redis.values.values())
    await store.reset_session(
        binding_id="binding-raw",
        binding_generation=4,
        conversation_key="conversation-raw",
    )
    assert "session-value" not in redis.values.values()


@pytest.mark.asyncio
async def test_session_mapping_is_scoped_by_binding_generation() -> None:
    redis = FakeRedis()
    store = RedisChannelExecutionStateStore(redis)
    await store.put_session(
        binding_id="binding-raw",
        binding_generation=1,
        conversation_key="conversation-raw",
        session_id="session-v1",
    )

    assert (
        await store.get_session(
            binding_id="binding-raw",
            binding_generation=2,
            conversation_key="conversation-raw",
        )
        is None
    )


@pytest.mark.asyncio
async def test_canvas_adapter_guards_latest_release_without_extending_canvas_contract(monkeypatch) -> None:
    from api.db.services import canvas_service as canvas_service_module
    from api.db.services.canvas_service import UserCanvasService
    from api.db.services.user_canvas_version import UserCanvasVersionService

    target = ExecutionTargetRef(
        target_type="multirag.canvas_agent",
        target_id="agent-1",
        revision_id="revision-latest",
    )
    db = AsyncSession()

    async def _run_sync(operation):
        return operation(SimpleNamespace())

    monkeypatch.setattr(db, "run_sync", _run_sync)
    sessions = FakeCanvasHistoryTransaction()
    adapter = SqlAlchemyCanvasTargetDriver(db, sessions)

    monkeypatch.setattr(
        UserCanvasService,
        "get_by_id",
        lambda db, canvas_id: SimpleNamespace(id=canvas_id, user_id="tenant-1"),
    )
    released = SimpleNamespace(
        id="revision-latest",
        user_canvas_id="agent-1",
        dsl={
            "components": {
                "begin": {"obj": {"component_name": "Begin", "params": {}}},
                "llm": {"obj": {"component_name": "LLM", "params": {}}},
            }
        },
    )
    monkeypatch.setattr(
        UserCanvasVersionService,
        "get_latest_released",
        lambda db, canvas_id: released,
    )

    await adapter.validate_revision(tenant_id="tenant-1", target=target)
    safe_capabilities = await adapter.capabilities(tenant_id="tenant-1", target=target)
    assert safe_capabilities.regeneration == "always"
    assert safe_capabilities.retryable is True
    assert safe_capabilities.effect_class == "generation_only"

    released.dsl = {
        "components": {
            "tool": {"obj": {"component_name": "Tool", "params": {}}},
        }
    }
    unsafe_capabilities = await adapter.capabilities(tenant_id="tenant-1", target=target)
    assert unsafe_capabilities.regeneration == "never"
    assert unsafe_capabilities.retryable is False
    assert unsafe_capabilities.effect_class == "unknown"

    captured: dict[str, object] = {}

    def _completion(**kwargs: Any) -> AsyncIterator[str]:
        captured.update(kwargs)

        async def _frames() -> AsyncIterator[str]:
            yield 'data:{"event":"message","data":{"content":"answer"},"session_id":"candidate-canvas"}\n\n'
            yield 'data:{"event":"message_end","data":{},"session_id":"candidate-canvas"}\n\n'

        return _frames()

    monkeypatch.setattr(canvas_service_module, "completion", _completion)
    frames = [
        frame
        async for frame in adapter.stream(
            tenant_id="tenant-1",
            target=target,
            question="hello",
            session_id="session-1",
            principal_id=None,
            operation="regenerate",
        )
    ]

    assert captured["release"] is True
    assert captured["session_id"] == "candidate-canvas"
    assert "regenerate" not in captured
    assert "persist_reasoning" not in captured
    assert "require_visible_answer" not in captured
    assert "release_revision_id" not in captured
    assert all("candidate-canvas" not in frame for frame in frames)
    assert all('"session_id": "session-1"' in frame for frame in frames)
    assert sessions.prepared == [("agent-1", "session-1", "hello", "regenerate")]
    assert sessions.completed == ["candidate-canvas"]
    assert sessions.aborted == []

    stale_target = target.model_copy(update={"revision_id": "revision-stale"})
    with pytest.raises(TargetRevisionUnavailableError):
        await adapter.validate_revision(tenant_id="tenant-1", target=stale_target)
    await db.close()


@pytest.mark.asyncio
async def test_dialog_adapter_promotes_only_complete_stream_and_hides_candidate_id(monkeypatch) -> None:
    from api.db.services import conversation_service as conversation_service_module

    target = ExecutionTargetRef(
        target_type="multirag.dialog",
        target_id="dialog-1",
    )
    db = AsyncSession()
    sessions = FakeDialogHistoryTransaction()
    adapter = SqlAlchemyDialogTargetDriver(db, sessions)
    captured: dict[str, object] = {}

    def _completion(**kwargs: Any) -> AsyncIterator[str]:
        captured.update(kwargs)

        async def _frames() -> AsyncIterator[str]:
            yield 'data:{"code":0,"data":{"answer":"answer","session_id":"candidate-dialog"}}\n\n'
            yield 'data:{"code":0,"data":true}\n\n'

        return _frames()

    monkeypatch.setattr(conversation_service_module, "async_completion", _completion)
    frames = [
        frame
        async for frame in adapter.stream(
            tenant_id="tenant-1",
            target=target,
            question="same question",
            session_id="dialog-session",
            principal_id="principal-1",
            operation="regenerate",
        )
    ]

    assert captured["session_id"] == "candidate-dialog"
    assert "regenerate" not in captured
    assert "persist_reasoning" not in captured
    assert "require_visible_answer" not in captured
    assert "candidate-dialog" not in "".join(frames)
    assert '"session_id": "dialog-session"' in frames[0]
    assert sessions.completed == [("candidate-dialog", True)]
    assert sessions.aborted == []
    await db.close()


@pytest.mark.asyncio
async def test_dialog_adapter_publishes_new_session_only_after_bootstrap_completes(
    monkeypatch,
) -> None:
    from api.db.services import conversation_service as conversation_service_module

    target = ExecutionTargetRef(
        target_type="multirag.dialog",
        target_id="dialog-1",
    )
    db = AsyncSession()
    sessions = FakeDialogHistoryTransaction()
    adapter = SqlAlchemyDialogTargetDriver(db, sessions)

    def _completion(**kwargs: Any) -> AsyncIterator[str]:
        assert kwargs["session_id"] is None

        async def _frames() -> AsyncIterator[str]:
            yield 'data:{"code":0,"data":{"answer":"prologue","session_id":"new-session"}}\n\n'
            yield 'data:{"code":0,"data":true}\n\n'

        return _frames()

    monkeypatch.setattr(conversation_service_module, "async_completion", _completion)
    frames = [
        frame
        async for frame in adapter.stream(
            tenant_id="tenant-1",
            target=target,
            question="hello",
            session_id=None,
            principal_id="principal-1",
        )
    ]

    assert "new-session" in frames[0]
    assert sessions.completed == [("new-session", False)]
    assert sessions.aborted == []
    await db.close()


@pytest.mark.asyncio
async def test_dialog_adapter_aborts_candidate_when_upstream_stream_fails(monkeypatch) -> None:
    from api.db.services import conversation_service as conversation_service_module

    target = ExecutionTargetRef(
        target_type="multirag.dialog",
        target_id="dialog-1",
    )
    db = AsyncSession()
    sessions = FakeDialogHistoryTransaction()
    adapter = SqlAlchemyDialogTargetDriver(db, sessions)

    async def _failed_frames() -> AsyncIterator[str]:
        yield 'data:{"code":500,"data":false}\n\n'
        yield 'data:{"code":0,"data":true}\n\n'

    def _completion(**kwargs: Any) -> AsyncIterator[str]:
        del kwargs
        return _failed_frames()

    monkeypatch.setattr(conversation_service_module, "async_completion", _completion)

    with pytest.raises(TargetExecutionFailedError):
        _ = [
            frame
            async for frame in adapter.stream(
                tenant_id="tenant-1",
                target=target,
                question="same question",
                session_id="dialog-session",
                principal_id=None,
                operation="regenerate",
            )
        ]

    assert sessions.completed == []
    assert sessions.aborted == ["candidate-dialog"]
    await db.close()


@pytest.mark.asyncio
async def test_failed_execution_keeps_non_replayable_tombstone() -> None:
    redis = FakeRedis()
    store = RedisChannelExecutionStateStore(redis, dedupe_ttl_seconds=123)
    assert await store.claim(binding_id="binding", event_id="event") is True

    await store.fail(binding_id="binding", event_id="event")

    assert await store.claim(binding_id="binding", event_id="event") is False
    assert redis.calls[-2][1:] == ("executed", 123, False)
