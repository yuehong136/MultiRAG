"""Tests for concrete Channel execution boundary adapters."""

import json
from collections.abc import AsyncIterator
from copy import deepcopy
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
from api.channel_execution.session_models import (
    DialogHistoryHead,
    DialogWorkingCopy,
    PreparedCanvasExecution,
    PreparedDialogExecution,
)


class FakeCanvasHistoryTransaction:
    def __init__(self) -> None:
        self.prepared: list[tuple[str, str | None, str, ExecutionOperation, str | None]] = []
        self.completed: list[str] = []
        self.aborted: list[str | None] = []

    async def prepare(
        self,
        *,
        target_id: str,
        session_id: str | None,
        question: str,
        operation: ExecutionOperation,
        user_id: str | None = None,
    ) -> PreparedCanvasExecution:
        self.prepared.append((target_id, session_id, question, operation, user_id))
        return PreparedCanvasExecution(
            session_id,
            "candidate-canvas" if session_id else None,
            "source-fingerprint" if session_id else None,
            "owner-token",
            "canvas-1",
            user_id or "",
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
        self.prepared: list[tuple[str, str | None, str, ExecutionOperation, str | None]] = []
        self.completed: list[DialogWorkingCopy] = []
        self.aborted: list[str] = []

    async def prepare(
        self,
        *,
        target_id: str,
        session_id: str | None,
        question: str,
        operation: ExecutionOperation,
        user_id: str | None = None,
    ) -> PreparedDialogExecution:
        self.prepared.append((target_id, session_id, question, operation, user_id))
        public_session_id = session_id or "new-dialog-session"
        return PreparedDialogExecution(
            public_session_id=public_session_id,
            target_id=target_id,
            expected_head=(
                DialogHistoryHead(
                    messages=[{"role": "assistant", "content": "prologue"}],
                    references=[],
                    user_id="existing-user",
                )
                if session_id
                else None
            ),
            working_copy=DialogWorkingCopy(
                id=public_session_id,
                dialog_id=target_id,
                name="existing" if session_id else "New session",
                message=([{"role": "assistant", "content": "prologue"}] if session_id else []),
                reference=[],
                user_id="existing-user" if session_id else user_id,
            ),
        )

    async def commit(self, prepared: PreparedDialogExecution) -> str:
        self.completed.append(deepcopy(prepared.working_copy))
        return prepared.public_session_id

    async def abort(self, prepared: PreparedDialogExecution) -> None:
        self.aborted.append(prepared.public_session_id)


def _install_dialog_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    adapter: SqlAlchemyDialogTargetDriver,
) -> None:
    async def _snapshot(*, tenant_id: str, target_id: str) -> SimpleNamespace:
        assert tenant_id == "tenant-1"
        assert target_id == "dialog-1"
        return SimpleNamespace(prompt_config={"prologue": "prologue"})

    monkeypatch.setattr(adapter, "_load_dialog_snapshot", _snapshot)


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
        tenant_id="tenant-raw",
        principal_id=None,
    )

    assert (
        await store.get_session(
            binding_id="binding-raw",
            binding_generation=4,
            conversation_key="conversation-raw",
            tenant_id="tenant-raw",
            principal_id=None,
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
async def test_initial_claim_uses_full_dedupe_ttl_and_survives_failed_tombstone() -> None:
    class _FailingTombstoneRedis(FakeRedis):
        async def set(
            self,
            name: str,
            value: str,
            *,
            ex: int | None = None,
            nx: bool = False,
        ) -> bool:
            if value == "executed":
                raise RuntimeError("simulated tombstone write failure")
            return await super().set(name, value, ex=ex, nx=nx)

    redis = _FailingTombstoneRedis()
    store = RedisChannelExecutionStateStore(
        redis,
        dedupe_ttl_seconds=86_400,
    )

    assert await store.claim(binding_id="binding-raw", event_id="event-raw")
    claim_call = redis.calls[0]
    assert claim_call[1:] == ("processing", 86_400, True)

    with pytest.raises(RuntimeError, match="tombstone write failure"):
        await store.fail(binding_id="binding-raw", event_id="event-raw")

    # Even a failed best-effort status transition must not shorten ownership
    # back to the historical ten-minute processing lease.
    assert await store.claim(binding_id="binding-raw", event_id="event-raw") is False


@pytest.mark.asyncio
async def test_session_mapping_is_scoped_by_binding_generation() -> None:
    redis = FakeRedis()
    store = RedisChannelExecutionStateStore(redis)
    await store.put_session(
        binding_id="binding-raw",
        binding_generation=1,
        conversation_key="conversation-raw",
        session_id="session-v1",
        tenant_id="tenant-raw",
        principal_id=None,
    )

    assert (
        await store.get_session(
            binding_id="binding-raw",
            binding_generation=2,
            conversation_key="conversation-raw",
            tenant_id="tenant-raw",
            principal_id=None,
        )
        is None
    )


@pytest.mark.asyncio
async def test_session_envelope_is_principal_owned_and_legacy_safe() -> None:
    redis = FakeRedis()
    store = RedisChannelExecutionStateStore(redis)
    arguments = {
        "binding_id": "binding-raw",
        "binding_generation": 4,
        "conversation_key": "conversation-raw",
        "tenant_id": "tenant-raw",
    }

    await store.put_session(
        **arguments,
        session_id="legacy-session",
        principal_id=None,
    )
    assert await store.get_session(**arguments, principal_id=None) == "legacy-session"
    assert await store.get_session(**arguments, principal_id="principal-a") is None

    await store.put_session(
        **arguments,
        session_id="owned-session",
        principal_id="principal-a",
    )
    assert await store.get_session(**arguments, principal_id="principal-a") == "owned-session"
    assert await store.get_session(**arguments, principal_id="principal-b") is None
    assert await store.get_session(**arguments, principal_id=None) is None
    assert (
        await store.get_session(
            **{**arguments, "tenant_id": "other-tenant"},
            principal_id="principal-a",
        )
        is None
    )

    stored = next(value for value in redis.values.values() if value.startswith("{"))
    assert "principal-a" not in stored
    assert "tenant-raw" not in stored
    assert json.loads(stored)["v"] == 1


@pytest.mark.asyncio
async def test_session_envelope_rejects_malformed_and_tampered_values_then_resets() -> None:
    redis = FakeRedis()
    store = RedisChannelExecutionStateStore(redis)
    arguments = {
        "binding_id": "binding-raw",
        "binding_generation": 4,
        "conversation_key": "conversation-raw",
        "tenant_id": "tenant-raw",
        "principal_id": "principal-a",
    }
    await store.put_session(
        **arguments,
        session_id="owned-session",
    )
    session_key = next(key for key, value in redis.values.items() if value.startswith("{"))

    redis.values[session_key] = "{malformed"
    with pytest.raises(TypeError, match="invalid channel session"):
        await store.get_session(**arguments)

    redis.values[session_key] = json.dumps({"v": 1, "session_id": "owned-session", "owner": "tampered"})
    assert await store.get_session(**arguments) is None

    await store.reset_session(
        binding_id="binding-raw",
        binding_generation=4,
        conversation_key="conversation-raw",
    )
    assert await store.get_session(**arguments) is None


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
        if not db.in_transaction():
            await db.begin()
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
    assert db.in_transaction() is False
    assert safe_capabilities.regeneration == "always"
    assert safe_capabilities.retryable is True
    assert safe_capabilities.effect_class == "generation_only"

    released.dsl = {
        "components": {
            "tool": {"obj": {"component_name": "Tool", "params": {}}},
        }
    }
    unsafe_capabilities = await adapter.capabilities(tenant_id="tenant-1", target=target)
    assert db.in_transaction() is False
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
    assert sessions.prepared == [("agent-1", "session-1", "hello", "regenerate", None)]
    assert sessions.completed == ["candidate-canvas"]
    assert sessions.aborted == []

    stale_target = target.model_copy(update={"revision_id": "revision-stale"})
    with pytest.raises(TargetRevisionUnavailableError):
        await adapter.validate_revision(tenant_id="tenant-1", target=stale_target)
    await db.close()


@pytest.mark.asyncio
async def test_canvas_driver_closes_inner_stream_and_aborts_when_consumer_cancels(monkeypatch) -> None:
    from api.db.services import canvas_service as canvas_service_module

    target = ExecutionTargetRef(
        target_type="multirag.canvas_agent",
        target_id="canvas-1",
        revision_id="revision-latest",
    )
    db = AsyncSession()
    sessions = FakeCanvasHistoryTransaction()
    adapter = SqlAlchemyCanvasTargetDriver(db, sessions)
    inner_closed = False

    async def _completion(**_kwargs: Any) -> AsyncIterator[str]:
        nonlocal inner_closed
        try:
            yield 'data:{"event":"message","data":{"content":"partial"},"session_id":"candidate-canvas"}\n\n'
            yield 'data:{"event":"message_end","data":{},"session_id":"candidate-canvas"}\n\n'
        finally:
            inner_closed = True

    monkeypatch.setattr(canvas_service_module, "completion", _completion)
    stream = adapter.stream(
        tenant_id="tenant-1",
        target=target,
        question="hello",
        session_id="session-1",
        principal_id=None,
    )

    assert "partial" in await anext(stream)
    await stream.aclose()

    assert inner_closed is True
    assert sessions.completed == []
    assert sessions.aborted == ["candidate-canvas"]
    await db.close()


@pytest.mark.asyncio
async def test_dialog_driver_commits_complete_answer_from_detached_working_copy(monkeypatch) -> None:
    from api.db.services import dialog_service as dialog_service_module

    target = ExecutionTargetRef(
        target_type="multirag.dialog",
        target_id="dialog-1",
    )
    db = AsyncSession()
    sessions = FakeDialogHistoryTransaction()
    adapter = SqlAlchemyDialogTargetDriver(db, sessions)
    _install_dialog_snapshot(monkeypatch, adapter)
    captured: dict[str, object] = {}

    async def _chat(
        dialog: object,
        messages: list[dict[str, Any]],
        chat_db: AsyncSession,
        stream: bool,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        captured.update(
            {
                "dialog": dialog,
                "messages": messages,
                "db": chat_db,
                "stream": stream,
                **kwargs,
            }
        )
        yield {"answer": "answer", "reference": {}, "final": True}

    monkeypatch.setattr(dialog_service_module, "async_chat", _chat)
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

    assert captured["stream"] is True
    assert captured["db"] is db
    assert captured["messages"] == [{"content": "same question", "role": "user", "id": captured["messages"][0]["id"]}]
    assert '"session_id": "dialog-session"' in frames[0]
    assert frames[-1] == 'data:{"code": 0, "data": true}\n\n'
    assert sessions.prepared == [("dialog-1", "dialog-session", "same question", "regenerate", "principal-1")]
    assert sessions.completed[0].message[-1]["content"] == "answer"
    assert sessions.completed[0].reference == [{"chunks": []}]
    assert sessions.aborted == []
    await db.close()


@pytest.mark.asyncio
async def test_dialog_driver_generates_new_session_once_without_exposing_prologue(
    monkeypatch,
) -> None:
    from api.db.services import dialog_service as dialog_service_module

    target = ExecutionTargetRef(
        target_type="multirag.dialog",
        target_id="dialog-1",
    )
    db = AsyncSession()
    sessions = FakeDialogHistoryTransaction()
    adapter = SqlAlchemyDialogTargetDriver(db, sessions)
    _install_dialog_snapshot(monkeypatch, adapter)
    calls = 0

    async def _chat(
        dialog: object,
        messages: list[dict[str, Any]],
        chat_db: AsyncSession,
        stream: bool,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        nonlocal calls
        del dialog, chat_db, stream, kwargs
        calls += 1
        assert [message["content"] for message in messages] == ["hello"]
        yield {"answer": "first answer", "reference": {}, "final": True}

    monkeypatch.setattr(dialog_service_module, "async_chat", _chat)
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

    assert calls == 1
    assert "prologue" not in "".join(frames)
    assert "new-dialog-session" in frames[0]
    assert sessions.completed[0].message[0]["content"] == "prologue"
    assert sessions.completed[0].message[-1]["content"] == "first answer"
    assert sessions.aborted == []
    await db.close()


@pytest.mark.asyncio
async def test_dialog_driver_discards_working_copy_when_generation_fails(monkeypatch) -> None:
    from api.db.services import dialog_service as dialog_service_module

    target = ExecutionTargetRef(
        target_type="multirag.dialog",
        target_id="dialog-1",
    )
    db = AsyncSession()
    sessions = FakeDialogHistoryTransaction()
    adapter = SqlAlchemyDialogTargetDriver(db, sessions)
    _install_dialog_snapshot(monkeypatch, adapter)

    async def _chat(*args: Any, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        del args, kwargs
        yield {"answer": "partial", "reference": {}, "final": False}
        raise RuntimeError("upstream failed")

    monkeypatch.setattr(dialog_service_module, "async_chat", _chat)

    with pytest.raises(RuntimeError, match="upstream failed"):
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
    assert sessions.aborted == ["dialog-session"]
    await db.close()


@pytest.mark.asyncio
async def test_dialog_driver_rejects_partial_stream_without_final_snapshot(monkeypatch) -> None:
    from api.db.services import dialog_service as dialog_service_module

    db = AsyncSession()
    sessions = FakeDialogHistoryTransaction()
    adapter = SqlAlchemyDialogTargetDriver(db, sessions)
    _install_dialog_snapshot(monkeypatch, adapter)

    async def _chat(*args: Any, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        del args, kwargs
        yield {"answer": "partial", "reference": {}, "final": False}

    monkeypatch.setattr(dialog_service_module, "async_chat", _chat)

    with pytest.raises(TargetExecutionFailedError):
        _ = [
            frame
            async for frame in adapter.stream(
                tenant_id="tenant-1",
                target=ExecutionTargetRef(target_type="multirag.dialog", target_id="dialog-1"),
                question="hello",
                session_id="dialog-session",
                principal_id=None,
            )
        ]

    assert sessions.completed == []
    assert sessions.aborted == ["dialog-session"]
    await db.close()


@pytest.mark.asyncio
async def test_dialog_driver_preserves_complete_snapshot_for_terminal_projection(monkeypatch) -> None:
    from api.db.services import dialog_service as dialog_service_module

    db = AsyncSession()
    sessions = FakeDialogHistoryTransaction()
    adapter = SqlAlchemyDialogTargetDriver(db, sessions)
    _install_dialog_snapshot(monkeypatch, adapter)

    async def _chat(*args: Any, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        del args, kwargs
        yield {"answer": "a", "reference": {}, "final": False}
        yield {"answer": "b", "reference": {}, "final": False}
        yield {"answer": "ab", "reference": {}, "final": True}

    monkeypatch.setattr(dialog_service_module, "async_chat", _chat)
    frames = [
        frame
        async for frame in adapter.stream(
            tenant_id="tenant-1",
            target=ExecutionTargetRef(target_type="multirag.dialog", target_id="dialog-1"),
            question="hello",
            session_id="dialog-session",
            principal_id=None,
        )
    ]

    assert sum('"answer": "a"' in frame for frame in frames) == 1
    assert sum('"answer": "b"' in frame for frame in frames) == 1
    assert sum('"answer": "ab"' in frame for frame in frames) == 1
    assert sessions.completed[0].message[-1]["content"] == "ab"
    await db.close()


@pytest.mark.asyncio
async def test_dialog_driver_closes_inner_stream_and_aborts_when_consumer_cancels(monkeypatch) -> None:
    from api.db.services import dialog_service as dialog_service_module

    db = AsyncSession()
    sessions = FakeDialogHistoryTransaction()
    adapter = SqlAlchemyDialogTargetDriver(db, sessions)
    _install_dialog_snapshot(monkeypatch, adapter)
    inner_closed = False

    async def _chat(*args: Any, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        nonlocal inner_closed
        del args, kwargs
        try:
            yield {"answer": "partial", "reference": {}, "final": False}
            yield {"answer": "never", "reference": {}, "final": True}
        finally:
            inner_closed = True

    monkeypatch.setattr(dialog_service_module, "async_chat", _chat)
    stream = adapter.stream(
        tenant_id="tenant-1",
        target=ExecutionTargetRef(target_type="multirag.dialog", target_id="dialog-1"),
        question="hello",
        session_id="dialog-session",
        principal_id=None,
    )

    assert "partial" in await anext(stream)
    await stream.aclose()

    assert inner_closed is True
    assert sessions.completed == []
    assert sessions.aborted == ["dialog-session"]
    await db.close()


@pytest.mark.asyncio
async def test_failed_execution_keeps_non_replayable_tombstone() -> None:
    redis = FakeRedis()
    store = RedisChannelExecutionStateStore(redis, dedupe_ttl_seconds=123)
    assert await store.claim(binding_id="binding", event_id="event") is True

    await store.fail(binding_id="binding", event_id="event")

    assert await store.claim(binding_id="binding", event_id="event") is False
    assert redis.calls[-2][1:] == ("executed", 123, False)
