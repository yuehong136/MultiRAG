"""Unit contracts for the trusted, MultiRAG-only Channel execution layer."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest
from pydantic import ValidationError

from api.channel_execution.errors import (
    BindingDisabledError,
    BindingNotFoundError,
    DuplicateEventError,
    TargetRevisionUnavailableError,
)
from api.channel_execution.executors import MultiRAGCanvasAgentExecutor, MultiRAGDialogExecutor
from api.channel_execution.models import (
    ChannelExecutionCommand,
    ExecutionEvent,
    ExecutionTargetRef,
    TrustedChannelContext,
    WorkloadIdentity,
)
from api.channel_execution.registry import TargetExecutorRegistry
from api.channel_execution.service import ChannelExecutionService, PublishedTargetExecutionService


def _command(**overrides: object) -> ChannelExecutionCommand:
    payload: dict[str, object] = {
        "event_id": "evt-1",
        "conversation_key": "feishu:chat:user",
        "message": {"type": "text", "content": "hello"},
        "actor": {"provider": "feishu", "subject": "ou-1", "conversation": "oc-1"},
    }
    payload.update(overrides)
    return ChannelExecutionCommand.model_validate(payload)


def _context(
    *,
    target_type: str = "multirag.canvas_agent",
    revision_id: str | None = "rev-1",
    enabled: bool = True,
) -> TrustedChannelContext:
    return TrustedChannelContext(
        binding_id="binding-1",
        tenant_id="tenant-trusted",
        target=ExecutionTargetRef(
            target_type=target_type,
            target_id="target-trusted",
            revision_id=revision_id,
        ),
        enabled=enabled,
        binding_generation=7,
        principal_id="principal-trusted",
    )


async def _collect(events: AsyncIterator[ExecutionEvent]) -> list[ExecutionEvent]:
    return [event async for event in events]


class _Resolver:
    def __init__(self, context: TrustedChannelContext | None) -> None:
        self.context = context
        self.seen: tuple[str, str] | None = None

    async def resolve(
        self,
        *,
        binding_id: str,
        workload: WorkloadIdentity,
        command: ChannelExecutionCommand,
    ) -> TrustedChannelContext | None:
        self.seen = (binding_id, workload.subject)
        assert command.event_id == "evt-1"
        return self.context


class _ConversationStore:
    def __init__(self, session_id: str | None = None) -> None:
        self.session_id = session_id
        self.saved: list[tuple[str, int, str, str]] = []

    async def get_session(
        self,
        *,
        binding_id: str,
        binding_generation: int,
        conversation_key: str,
    ) -> str | None:
        assert (binding_id, binding_generation, conversation_key) == (
            "binding-1",
            7,
            "feishu:chat:user",
        )
        return self.session_id

    async def put_session(
        self,
        *,
        binding_id: str,
        binding_generation: int,
        conversation_key: str,
        session_id: str,
    ) -> None:
        self.saved.append((binding_id, binding_generation, conversation_key, session_id))

    async def reset_session(
        self,
        *,
        binding_id: str,
        binding_generation: int,
        conversation_key: str,
    ) -> None:
        del binding_id, binding_generation, conversation_key
        self.session_id = None


class _ClaimStore:
    def __init__(self, *, claimed: bool = True) -> None:
        self.claimed = claimed
        self.claims: list[tuple[str, str]] = []
        self.completed: list[tuple[str, str]] = []
        self.failed: list[tuple[str, str]] = []

    async def claim(self, *, binding_id: str, event_id: str) -> bool:
        self.claims.append((binding_id, event_id))
        return self.claimed

    async def complete(self, *, binding_id: str, event_id: str) -> None:
        self.completed.append((binding_id, event_id))

    async def fail(self, *, binding_id: str, event_id: str) -> None:
        self.failed.append((binding_id, event_id))


class _RecordingExecutor:
    target_type = "multirag.canvas_agent"

    def __init__(self) -> None:
        self.context: TrustedChannelContext | None = None

    async def execute(
        self,
        *,
        context: TrustedChannelContext,
        command: ChannelExecutionCommand,
    ) -> AsyncIterator[ExecutionEvent]:
        self.context = context

        async def _events() -> AsyncIterator[ExecutionEvent]:
            yield ExecutionEvent(event="message_delta", content=command.message.content, session_id="session-new")
            yield ExecutionEvent(event="message_completed", session_id="session-new")

        return _events()


def test_command_rejects_trust_fields_from_payload() -> None:
    base = _command().model_dump()
    for field in ("tenant_id", "target_id", "target_type", "revision_id", "session_id"):
        with pytest.raises(ValidationError):
            ChannelExecutionCommand.model_validate({**base, field: "attacker-value"})


async def test_service_uses_resolved_target_and_server_side_session() -> None:
    resolver = _Resolver(_context())
    store = _ConversationStore("session-existing")
    claims = _ClaimStore()
    executor = _RecordingExecutor()
    service = ChannelExecutionService(
        binding_resolver=resolver,
        conversation_store=store,
        claim_store=claims,
        target_service=PublishedTargetExecutionService(TargetExecutorRegistry([executor])),
    )

    events = await service.execute(
        binding_id="binding-1",
        workload=WorkloadIdentity(subject="runner-1"),
        command=_command(),
    )

    assert [event.event for event in await _collect(events)] == ["message_delta", "message_completed"]
    assert executor.context is not None
    assert executor.context.tenant_id == "tenant-trusted"
    assert executor.context.target.target_id == "target-trusted"
    assert executor.context.session_id == "session-existing"
    assert store.saved == [("binding-1", 7, "feishu:chat:user", "session-new")]
    assert claims.claims == [("binding-1", "evt-1")]
    assert claims.completed == [("binding-1", "evt-1")]
    assert claims.failed == []


async def test_registry_rejects_non_multirag_namespace_and_duplicates() -> None:
    executor = _RecordingExecutor()
    registry = TargetExecutorRegistry([executor])
    assert registry.get("multirag.canvas_agent") is executor

    with pytest.raises(ValueError, match="already registered"):
        registry.register(executor)

    class _ExternalExecutor(_RecordingExecutor):
        target_type = "external.dialog"

    with pytest.raises(ValueError, match="multirag namespace"):
        TargetExecutorRegistry([_ExternalExecutor()])


@pytest.mark.parametrize(
    ("resolved", "expected"),
    [
        (None, BindingNotFoundError),
        (_context(enabled=False), BindingDisabledError),
    ],
)
async def test_service_fails_before_target_for_missing_or_disabled_binding(
    resolved: TrustedChannelContext | None,
    expected: type[Exception],
) -> None:
    service = ChannelExecutionService(
        binding_resolver=_Resolver(resolved),
        conversation_store=_ConversationStore(),
        claim_store=_ClaimStore(),
        target_service=PublishedTargetExecutionService(TargetExecutorRegistry([_RecordingExecutor()])),
    )
    with pytest.raises(expected):
        await service.execute(
            binding_id="binding-1",
            workload=WorkloadIdentity(subject="runner"),
            command=_command(),
        )


async def test_duplicate_event_never_reaches_target_executor() -> None:
    executor = _RecordingExecutor()
    claims = _ClaimStore(claimed=False)
    service = ChannelExecutionService(
        binding_resolver=_Resolver(_context()),
        conversation_store=_ConversationStore(),
        claim_store=claims,
        target_service=PublishedTargetExecutionService(TargetExecutorRegistry([executor])),
    )

    with pytest.raises(DuplicateEventError):
        await service.execute(
            binding_id="binding-1",
            workload=WorkloadIdentity(subject="runner"),
            command=_command(),
        )

    assert claims.claims == [("binding-1", "evt-1")]
    assert executor.context is None


class _CanvasAdapter:
    def __init__(self, frames: list[str], *, invalid_revision: bool = False) -> None:
        self.frames = frames
        self.invalid_revision = invalid_revision
        self.validated: tuple[str, str | None] | None = None

    async def validate_revision(self, *, tenant_id: str, target: ExecutionTargetRef) -> None:
        self.validated = (tenant_id, target.revision_id)
        if self.invalid_revision:
            raise TargetRevisionUnavailableError()

    def stream(
        self,
        *,
        tenant_id: str,
        target: ExecutionTargetRef,
        question: str,
        session_id: str | None,
        principal_id: str | None,
    ) -> AsyncIterator[str]:
        del tenant_id, target, question, session_id, principal_id

        async def _frames() -> AsyncIterator[str]:
            for frame in self.frames:
                yield frame

        return _frames()


def _frame(payload: dict[str, object]) -> str:
    return "data:" + json.dumps(payload) + "\n\n"


async def test_canvas_executor_filters_reasoning_trace_and_tool_events() -> None:
    adapter = _CanvasAdapter(
        [
            _frame({"event": "node_finished", "data": {"trace": "private"}, "session_id": "s-1"}),
            _frame({"event": "message", "data": {"start_to_think": True, "content": "private"}, "session_id": "s-1"}),
            _frame({"event": "message", "data": {"content": "reasoning"}, "session_id": "s-1"}),
            _frame({"event": "message", "data": {"end_to_think": True}, "session_id": "s-1"}),
            _frame({"event": "tool_call", "data": {"arguments": "private"}, "session_id": "s-1"}),
            _frame({"event": "message", "data": {"content": "answer"}, "session_id": "s-1"}),
            _frame({"event": "message_end", "data": {}, "session_id": "s-1"}),
        ]
    )
    executor = MultiRAGCanvasAgentExecutor(adapter)

    events = await executor.execute(context=_context(), command=_command())

    assert [event.model_dump(exclude_none=True) for event in await _collect(events)] == [
        {"event": "message_delta", "content": "answer", "session_id": "s-1"},
        {"event": "message_completed", "session_id": "s-1"},
    ]
    assert adapter.validated == ("tenant-trusted", "rev-1")


class _DialogAdapter:
    def __init__(self) -> None:
        self.sessions: list[str | None] = []

    def stream(
        self,
        *,
        tenant_id: str,
        target: ExecutionTargetRef,
        question: str,
        session_id: str | None,
        principal_id: str | None,
    ) -> AsyncIterator[str]:
        del tenant_id, target, question, principal_id
        self.sessions.append(session_id)

        async def _frames() -> AsyncIterator[str]:
            if session_id is None:
                yield _frame({"code": 0, "data": {"answer": "prologue", "session_id": "dialog-session"}})
            else:
                yield _frame({"code": 0, "data": {"answer": "dialog-answer", "session_id": session_id}})
            yield _frame({"code": 0, "data": True})

        return _frames()


async def test_dialog_executor_bootstraps_then_answers_using_multirag_dialog() -> None:
    adapter = _DialogAdapter()
    executor = MultiRAGDialogExecutor(adapter)
    context = _context(target_type="multirag.dialog", revision_id=None)

    events = await executor.execute(context=context, command=_command())

    assert [event.model_dump(exclude_none=True) for event in await _collect(events)] == [
        {"event": "message_delta", "content": "dialog-answer", "session_id": "dialog-session"},
        {"event": "message_completed", "session_id": "dialog-session"},
    ]
    assert adapter.sessions == [None, "dialog-session"]


async def test_unexpected_target_error_is_desensitized(caplog: pytest.LogCaptureFixture) -> None:
    class _FailingExecutor(_RecordingExecutor):
        async def execute(
            self,
            *,
            context: TrustedChannelContext,
            command: ChannelExecutionCommand,
        ) -> AsyncIterator[ExecutionEvent]:
            del context, command
            raise RuntimeError("do-not-leak-this-secret")

    service = PublishedTargetExecutionService(TargetExecutorRegistry([_FailingExecutor()]))
    events = await service.execute(context=_context(), command=_command())

    assert [event.model_dump(exclude_none=True) for event in await _collect(events)] == [{"event": "execution_failed", "error_code": "TARGET_EXECUTION_FAILED"}]
    assert "do-not-leak-this-secret" not in caplog.text
