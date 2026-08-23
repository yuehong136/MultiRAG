"""Unit contracts for the trusted, MultiRAG-only Channel execution layer."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from api.channel_capabilities import TargetCapabilities
from api.channel_execution.errors import (
    BindingDisabledError,
    BindingNotFoundError,
    ChannelIdentityResolutionError,
    DuplicateEventError,
    TargetExecutionFailedError,
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
from api.identity.contracts import (
    ExternalIdentityRecord,
    IdentityResolutionResult,
    IdentityResolutionStatus,
    UserMembershipRecord,
)
from api.identity.principal import (
    AuthenticationContext,
    AuthenticationSource,
    IdentityAssurance,
    build_principal_from_resolved_identity,
)
from api.identity.run_context import RunContext
from common.mcp_interactions import MCPInteractionPaused


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
    session_id: str | None = None,
    run_policy: dict[str, object] | None = None,
) -> TrustedChannelContext:
    verified_at = datetime(2026, 8, 13, tzinfo=UTC)
    principal = build_principal_from_resolved_identity(
        result=IdentityResolutionResult(
            status=IdentityResolutionStatus.RESOLVED,
            identity=ExternalIdentityRecord(
                id="identity-trusted",
                tenant_id="tenant-trusted",
                user_id="principal-trusted",
                provider="feishu",
                provider_tenant_key="provider-tenant-secret",
                subject_type="user_id",
                subject_value="provider-user-secret",
                state="active",
                verified_at=verified_at,
                last_seen_at=verified_at,
                identity_revision=1,
                attributes=(("display_name", "Trusted principal"),),
            ),
            membership=UserMembershipRecord(
                user_id="principal-trusted",
                tenant_id="tenant-trusted",
                role="normal",
            ),
        ),
        authentication=AuthenticationContext(
            source=AuthenticationSource.ENTERPRISE_IDENTITY,
            assurance=IdentityAssurance.DIRECTORY_VERIFIED,
            validated_at=verified_at,
            assurance_verified_at=verified_at,
            provider="feishu",
            external_identity_id="identity-trusted",
        ),
    )
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
        provider="feishu",
        run_policy=run_policy or {},
        principal_id="principal-trusted",
        principal=principal,
        session_id=session_id,
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
        assert command.conversation_key == "feishu:chat:user"
        return self.context


class _ConversationStore:
    def __init__(self, session_id: str | None = None) -> None:
        self.session_id = session_id
        self.saved: list[tuple[str, int, str, str, str, str | None]] = []

    async def get_session(
        self,
        *,
        binding_id: str,
        binding_generation: int,
        conversation_key: str,
        tenant_id: str,
        principal_id: str | None,
    ) -> str | None:
        assert (binding_id, binding_generation, conversation_key) == (
            "binding-1",
            7,
            "feishu:chat:user",
        )
        assert tenant_id == "tenant-trusted"
        assert principal_id == "principal-trusted"
        return self.session_id

    async def put_session(
        self,
        *,
        binding_id: str,
        binding_generation: int,
        conversation_key: str,
        session_id: str,
        tenant_id: str,
        principal_id: str | None,
    ) -> None:
        self.saved.append(
            (
                binding_id,
                binding_generation,
                conversation_key,
                session_id,
                tenant_id,
                principal_id,
            )
        )

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


class _PrincipalResolver:
    def __init__(
        self,
        *,
        error: Exception | None = None,
        cancel: bool = False,
        order: list[str] | None = None,
    ) -> None:
        self.error = error
        self.cancel = cancel
        self.order = order
        self.calls: list[TrustedChannelContext] = []

    async def resolve(
        self,
        *,
        context: TrustedChannelContext,
        command: ChannelExecutionCommand,
    ) -> TrustedChannelContext:
        del command
        self.calls.append(context)
        if self.order is not None:
            self.order.append("principal")
        if self.cancel:
            raise asyncio.CancelledError
        if self.error is not None:
            raise self.error
        return context


class _FaultingConversationStore(_ConversationStore):
    def __init__(self, stage: str, *, cancel: bool) -> None:
        super().__init__("session-existing")
        self.stage = stage
        self.cancel = cancel

    def _raise(self) -> None:
        if self.cancel:
            raise asyncio.CancelledError
        raise RuntimeError("sensitive state-store failure")

    async def get_session(
        self,
        *,
        binding_id: str,
        binding_generation: int,
        conversation_key: str,
        tenant_id: str,
        principal_id: str | None,
    ) -> str | None:
        if self.stage == "session_get":
            self._raise()
        return await super().get_session(
            binding_id=binding_id,
            binding_generation=binding_generation,
            conversation_key=conversation_key,
            tenant_id=tenant_id,
            principal_id=principal_id,
        )

    async def put_session(
        self,
        *,
        binding_id: str,
        binding_generation: int,
        conversation_key: str,
        session_id: str,
        tenant_id: str,
        principal_id: str | None,
    ) -> None:
        if self.stage == "put":
            self._raise()
        await super().put_session(
            binding_id=binding_id,
            binding_generation=binding_generation,
            conversation_key=conversation_key,
            session_id=session_id,
            tenant_id=tenant_id,
            principal_id=principal_id,
        )


class _FaultingClaimStore(_ClaimStore):
    def __init__(self, stage: str, *, cancel: bool) -> None:
        super().__init__()
        self.stage = stage
        self.cancel = cancel

    async def complete(self, *, binding_id: str, event_id: str) -> None:
        if self.stage == "complete":
            if self.cancel:
                raise asyncio.CancelledError
            raise RuntimeError("sensitive completion failure")
        await super().complete(binding_id=binding_id, event_id=event_id)


class _FaultingExecutor:
    target_type = "multirag.canvas_agent"

    def __init__(self, stage: str, *, cancel: bool) -> None:
        self.stage = stage
        self.cancel = cancel
        self.context: TrustedChannelContext | None = None

    async def capabilities(
        self,
        *,
        context: TrustedChannelContext,
    ) -> TargetCapabilities:
        del context
        return TargetCapabilities(
            streaming=True,
            cancellable=True,
            regeneration="always",
            retryable=True,
            feedback=True,
            commit_mode="candidate_cas",
            effect_class="generation_only",
        )

    async def execute(
        self,
        *,
        context: TrustedChannelContext,
        command: ChannelExecutionCommand,
    ) -> AsyncIterator[ExecutionEvent]:
        if self.stage == "target_prepare":
            if self.cancel:
                raise asyncio.CancelledError
            raise RuntimeError("sensitive target prepare failure")
        if self.stage != "stream":
            self.context = context

            async def _completed() -> AsyncIterator[ExecutionEvent]:
                yield ExecutionEvent(
                    event="message_delta",
                    content=command.message.content,
                    session_id="session-new",
                )
                yield ExecutionEvent(
                    event="message_completed",
                    session_id="session-new",
                )

            return _completed()

        async def _events() -> AsyncIterator[ExecutionEvent]:
            if self.cancel:
                raise asyncio.CancelledError
            raise RuntimeError("sensitive target stream failure")
            yield ExecutionEvent(event="message_delta", content="unreachable")

        return _events()


class _RecordingExecutor:
    target_type = "multirag.canvas_agent"

    def __init__(self) -> None:
        self.context: TrustedChannelContext | None = None

    async def capabilities(
        self,
        *,
        context: TrustedChannelContext,
    ) -> TargetCapabilities:
        del context
        return TargetCapabilities(
            streaming=True,
            cancellable=True,
            regeneration="always",
            retryable=True,
            feedback=True,
            commit_mode="candidate_cas",
            effect_class="generation_only",
        )

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


class _PausingExecutor(_RecordingExecutor):
    async def execute(
        self,
        *,
        context: TrustedChannelContext,
        command: ChannelExecutionCommand,
    ) -> AsyncIterator[ExecutionEvent]:
        self.context = context

        async def _events() -> AsyncIterator[ExecutionEvent]:
            raise MCPInteractionPaused(
                interaction_id="interaction-1",
                revision=2,
            )
            yield ExecutionEvent(event="message_delta", content="unreachable")

        return _events()


class _InteractionRegistration:
    def __init__(self) -> None:
        self.action_id = "interaction-1"
        self.revision = 2
        self.expires_at = datetime(2026, 8, 14, tzinfo=UTC) + timedelta(minutes=5)


class _PresentationRegistrar:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def register(
        self,
        *,
        context: TrustedChannelContext,
        interaction_id: str,
        revision: int,
        source_event_id: str,
        conversation_ref: str,
        presentation_ref: str,
    ) -> _InteractionRegistration:
        self.calls.append(
            {
                "context": context,
                "interaction_id": interaction_id,
                "revision": revision,
                "source_event_id": source_event_id,
                "conversation_ref": conversation_ref,
                "presentation_ref": presentation_ref,
            }
        )
        return _InteractionRegistration()


def test_command_rejects_trust_fields_from_payload() -> None:
    base = _command().model_dump()
    for field in ("tenant_id", "target_id", "target_type", "revision_id", "session_id"):
        with pytest.raises(ValidationError):
            ChannelExecutionCommand.model_validate({**base, field: "attacker-value"})

    with pytest.raises(ValidationError):
        _command(operation="retry-anything")


async def test_service_uses_resolved_target_and_server_side_session() -> None:
    resolver = _Resolver(_context())
    store = _ConversationStore("session-existing")
    claims = _ClaimStore()
    executor = _RecordingExecutor()
    service = ChannelExecutionService(
        binding_resolver=resolver,
        principal_resolver=_PrincipalResolver(),
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
    assert store.saved == [
        (
            "binding-1",
            7,
            "feishu:chat:user",
            "session-new",
            "tenant-trusted",
            "principal-trusted",
        )
    ]
    assert claims.claims == [("binding-1", "evt-1")]
    assert claims.completed == [("binding-1", "evt-1")]
    assert claims.failed == []


async def test_paused_mcp_call_registers_terminal_interaction_without_saving_conversation() -> None:
    store = _ConversationStore("session-existing")
    claims = _ClaimStore()
    executor = _PausingExecutor()
    registrar = _PresentationRegistrar()
    service = ChannelExecutionService(
        binding_resolver=_Resolver(_context()),
        principal_resolver=_PrincipalResolver(),
        conversation_store=store,
        claim_store=claims,
        target_service=PublishedTargetExecutionService(
            TargetExecutorRegistry([executor]),
        ),
        presentation_registrar=registrar,
    )

    events = await service.execute(
        binding_id="binding-1",
        workload=WorkloadIdentity(subject="runner-1"),
        command=_command(presentation_ref="reply-message-1"),
    )
    collected = await _collect(events)

    assert collected == [
        ExecutionEvent(
            event="interaction_required",
            action_id="interaction-1",
            revision=2,
            expires_at=_InteractionRegistration().expires_at,
        )
    ]
    assert executor.context is not None
    assert registrar.calls == [
        {
            "context": executor.context,
            "interaction_id": "interaction-1",
            "revision": 2,
            "source_event_id": "evt-1",
            "conversation_ref": "oc-1",
            "presentation_ref": "reply-message-1",
        }
    ]
    assert claims.completed == [("binding-1", "evt-1")]
    assert claims.failed == []
    assert store.saved == []


async def test_legacy_action_without_operation_fails_closed_before_execution() -> None:
    executor = _RecordingExecutor()
    claims = _ClaimStore()
    service = ChannelExecutionService(
        binding_resolver=_Resolver(_context()),
        principal_resolver=_PrincipalResolver(),
        conversation_store=_ConversationStore("session-existing"),
        claim_store=claims,
        target_service=PublishedTargetExecutionService(TargetExecutorRegistry([executor])),
    )
    command = _command(event_id="action:legacy-event")

    events = await service.execute(
        binding_id="binding-1",
        workload=WorkloadIdentity(subject="runner-1"),
        command=command,
    )

    assert [event.model_dump(exclude_none=True) for event in await _collect(events)] == [{"event": "execution_failed", "error_code": "CHANNEL_RUNTIME_UPGRADE_REQUIRED"}]
    assert claims.claims == []
    assert executor.context is None


@pytest.mark.parametrize("denied_by", ["run_policy", "target"])
async def test_regenerate_is_authorized_before_claiming_the_event(denied_by: str) -> None:
    class _NoRegenerationExecutor(_RecordingExecutor):
        async def capabilities(
            self,
            *,
            context: TrustedChannelContext,
        ) -> TargetCapabilities:
            capabilities = await super().capabilities(context=context)
            return capabilities.model_copy(update={"regeneration": "never"})

    executor = _NoRegenerationExecutor() if denied_by == "target" else _RecordingExecutor()
    claims = _ClaimStore()
    context = _context(run_policy={"reply_capabilities": {"regenerate": False}} if denied_by == "run_policy" else None)
    service = ChannelExecutionService(
        binding_resolver=_Resolver(context),
        principal_resolver=_PrincipalResolver(),
        conversation_store=_ConversationStore("session-existing"),
        claim_store=claims,
        target_service=PublishedTargetExecutionService(TargetExecutorRegistry([executor])),
    )

    events = await service.execute(
        binding_id="binding-1",
        workload=WorkloadIdentity(subject="runner-1"),
        command=_command(operation="regenerate"),
    )

    assert [event.model_dump(exclude_none=True) for event in await _collect(events)] == [{"event": "execution_failed", "error_code": "CHANNEL_OPERATION_NOT_ALLOWED"}]
    assert claims.claims == []
    assert executor.context is None


@pytest.mark.parametrize("denied_by", ["run_policy", "target"])
async def test_failed_action_retry_is_authorized_before_claiming_the_event(denied_by: str) -> None:
    class _NoRetryExecutor(_RecordingExecutor):
        async def capabilities(
            self,
            *,
            context: TrustedChannelContext,
        ) -> TargetCapabilities:
            capabilities = await super().capabilities(context=context)
            return capabilities.model_copy(update={"retryable": False})

    executor = _NoRetryExecutor() if denied_by == "target" else _RecordingExecutor()
    claims = _ClaimStore()
    context = _context(run_policy={"reply_capabilities": {"retry": False}} if denied_by == "run_policy" else None)
    service = ChannelExecutionService(
        binding_resolver=_Resolver(context),
        principal_resolver=_PrincipalResolver(),
        conversation_store=_ConversationStore("session-existing"),
        claim_store=claims,
        target_service=PublishedTargetExecutionService(TargetExecutorRegistry([executor])),
    )

    events = await service.execute(
        binding_id="binding-1",
        workload=WorkloadIdentity(subject="runner-1"),
        command=_command(event_id="action:retry-event", operation="message"),
    )

    assert [event.model_dump(exclude_none=True) for event in await _collect(events)] == [{"event": "execution_failed", "error_code": "CHANNEL_OPERATION_NOT_ALLOWED"}]
    assert claims.claims == []
    assert executor.context is None


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
        principal_resolver=_PrincipalResolver(),
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
    principal_resolver = _PrincipalResolver()
    service = ChannelExecutionService(
        binding_resolver=_Resolver(_context()),
        principal_resolver=principal_resolver,
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
    assert principal_resolver.calls == []
    assert executor.context is None


async def test_identity_resolution_runs_only_after_successful_claim() -> None:
    order: list[str] = []

    class _OrderedClaimStore(_ClaimStore):
        async def claim(self, *, binding_id: str, event_id: str) -> bool:
            order.append("claim")
            return await super().claim(binding_id=binding_id, event_id=event_id)

    claims = _OrderedClaimStore()
    principal_resolver = _PrincipalResolver(order=order)
    service = ChannelExecutionService(
        binding_resolver=_Resolver(_context()),
        principal_resolver=principal_resolver,
        conversation_store=_ConversationStore("session-existing"),
        claim_store=claims,
        target_service=PublishedTargetExecutionService(TargetExecutorRegistry([_RecordingExecutor()])),
    )

    events = await service.execute(
        binding_id="binding-1",
        workload=WorkloadIdentity(subject="runner"),
        command=_command(),
    )
    await _collect(events)

    assert order == ["claim", "principal"]


async def test_identity_failure_after_claim_writes_tombstone_and_skips_target() -> None:
    claims = _ClaimStore()
    executor = _RecordingExecutor()
    principal_resolver = _PrincipalResolver(error=ChannelIdentityResolutionError("IDENTITY_INACTIVE"))
    service = ChannelExecutionService(
        binding_resolver=_Resolver(_context()),
        principal_resolver=principal_resolver,
        conversation_store=_ConversationStore("session-existing"),
        claim_store=claims,
        target_service=PublishedTargetExecutionService(TargetExecutorRegistry([executor])),
    )

    events = await service.execute(
        binding_id="binding-1",
        workload=WorkloadIdentity(subject="runner"),
        command=_command(),
    )

    assert [event.model_dump(exclude_none=True) for event in await _collect(events)] == [{"event": "execution_failed", "error_code": "IDENTITY_INACTIVE"}]
    assert claims.claims == [("binding-1", "evt-1")]
    assert claims.failed == [("binding-1", "evt-1")]
    assert executor.context is None


async def test_identity_cancellation_after_claim_writes_tombstone_and_propagates() -> None:
    claims = _ClaimStore()
    service = ChannelExecutionService(
        binding_resolver=_Resolver(_context()),
        principal_resolver=_PrincipalResolver(cancel=True),
        conversation_store=_ConversationStore("session-existing"),
        claim_store=claims,
        target_service=PublishedTargetExecutionService(TargetExecutorRegistry([_RecordingExecutor()])),
    )

    with pytest.raises(asyncio.CancelledError):
        await service.execute(
            binding_id="binding-1",
            workload=WorkloadIdentity(subject="runner"),
            command=_command(),
        )

    assert claims.claims == [("binding-1", "evt-1")]
    assert claims.failed == [("binding-1", "evt-1")]


@pytest.mark.parametrize(
    "stage",
    ["session_get", "target_prepare", "stream", "put", "complete"],
)
@pytest.mark.parametrize("cancel", [False, True], ids=["error", "cancel"])
async def test_every_postclaim_stage_is_tombstoned_on_error_or_cancellation(
    stage: str,
    cancel: bool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    claims = _FaultingClaimStore(stage, cancel=cancel)
    store = _FaultingConversationStore(stage, cancel=cancel)
    executor = _FaultingExecutor(stage, cancel=cancel)
    service = ChannelExecutionService(
        binding_resolver=_Resolver(_context()),
        principal_resolver=_PrincipalResolver(),
        conversation_store=store,
        claim_store=claims,
        target_service=PublishedTargetExecutionService(TargetExecutorRegistry([executor])),
    )

    async def _run() -> list[ExecutionEvent]:
        events = await service.execute(
            binding_id="binding-1",
            workload=WorkloadIdentity(subject="runner"),
            command=_command(),
        )
        return await _collect(events)

    if cancel:
        with pytest.raises(asyncio.CancelledError):
            await _run()
    else:
        events = await _run()
        assert events[-1].event == "execution_failed"

    assert claims.claims == [("binding-1", "evt-1")]
    assert claims.failed == [("binding-1", "evt-1")]
    assert "sensitive" not in caplog.text


class _CanvasAdapter:
    def __init__(self, frames: list[str], *, invalid_revision: bool = False) -> None:
        self.frames = frames
        self.invalid_revision = invalid_revision
        self.validated: tuple[str, str | None] | None = None
        self.operations: list[object] = []
        self.run_contexts: list[RunContext] = []
        self.closed = False

    async def capabilities(
        self,
        *,
        tenant_id: str,
        target: ExecutionTargetRef,
    ) -> TargetCapabilities:
        del tenant_id, target
        return TargetCapabilities(
            streaming=True,
            cancellable=True,
            regeneration="conditional",
            retryable=True,
            feedback=True,
            commit_mode="candidate_cas",
            effect_class="unknown",
        )

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
        run_context: RunContext,
        operation: object,
    ) -> AsyncIterator[str]:
        del tenant_id, target, question, session_id
        self.run_contexts.append(run_context)
        self.operations.append(operation)

        async def _frames() -> AsyncIterator[str]:
            try:
                for frame in self.frames:
                    yield frame
            finally:
                self.closed = True

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
    context = _context()

    events = await executor.execute(context=context, command=_command())

    assert [event.model_dump(exclude_none=True) for event in await _collect(events)] == [
        {"event": "message_delta", "content": "answer", "session_id": "s-1"},
        {"event": "message_completed", "session_id": "s-1"},
    ]
    assert adapter.validated == ("tenant-trusted", "rev-1")
    assert adapter.operations == ["message"]
    assert adapter.run_contexts[0].principal is context.principal
    assert adapter.run_contexts[0].platform_user_id == "principal-trusted"


async def test_target_executors_declare_current_target_private_capabilities() -> None:
    canvas = MultiRAGCanvasAgentExecutor(_CanvasAdapter([]))
    dialog = MultiRAGDialogExecutor(_DialogAdapter())

    canvas_capabilities = await canvas.capabilities(context=_context())
    dialog_capabilities = await dialog.capabilities(context=_context(target_type="multirag.dialog", revision_id=None))

    assert canvas_capabilities.regeneration == "conditional"
    assert canvas_capabilities.commit_mode == "candidate_cas"
    assert canvas_capabilities.effect_class == "unknown"
    assert dialog_capabilities.regeneration == "always"
    assert dialog_capabilities.commit_mode == "detached_cas"
    assert dialog_capabilities.effect_class == "generation_only"


async def test_canvas_executor_rejects_a_reasoning_only_completion() -> None:
    adapter = _CanvasAdapter(
        [
            _frame({"event": "message", "data": {"start_to_think": True}, "session_id": "s-1"}),
            _frame({"event": "message", "data": {"content": "private"}, "session_id": "s-1"}),
            _frame({"event": "message", "data": {"end_to_think": True}, "session_id": "s-1"}),
            _frame({"event": "message_end", "data": {}, "session_id": "s-1"}),
        ]
    )
    executor = MultiRAGCanvasAgentExecutor(adapter)
    events = await executor.execute(context=_context(), command=_command())

    with pytest.raises(TargetExecutionFailedError):
        await _collect(events)


class _DialogAdapter:
    def __init__(self, frames: list[str] | None = None) -> None:
        self.sessions: list[str | None] = []
        self.operations: list[object] = []
        self.run_contexts: list[RunContext] = []
        self.frames = frames
        self.closed = False

    def stream(
        self,
        *,
        tenant_id: str,
        target: ExecutionTargetRef,
        question: str,
        session_id: str | None,
        run_context: RunContext,
        operation: object,
    ) -> AsyncIterator[str]:
        del tenant_id, target, question
        self.run_contexts.append(run_context)
        self.sessions.append(session_id)
        self.operations.append(operation)

        async def _frames() -> AsyncIterator[str]:
            try:
                public_session_id = session_id or "dialog-session"
                if self.frames is not None:
                    for frame in self.frames:
                        yield frame
                    return
                yield _frame({"code": 0, "data": {"answer": "dialog-answer", "session_id": public_session_id}})
                yield _frame({"code": 0, "data": True})
            finally:
                self.closed = True

        return _frames()


async def test_dialog_executor_generates_first_answer_in_one_target_run() -> None:
    adapter = _DialogAdapter()
    executor = MultiRAGDialogExecutor(adapter)
    context = _context(target_type="multirag.dialog", revision_id=None)

    events = await executor.execute(context=context, command=_command())

    assert [event.model_dump(exclude_none=True) for event in await _collect(events)] == [
        {"event": "message_delta", "content": "dialog-answer", "session_id": "dialog-session"},
        {
            "event": "message_completed",
            "content": "dialog-answer",
            "session_id": "dialog-session",
        },
    ]
    assert adapter.run_contexts[0].principal is context.principal
    assert adapter.run_contexts[0].platform_user_id == "principal-trusted"
    assert adapter.sessions == [None]
    assert adapter.operations == ["message"]


async def test_dialog_executor_streams_deltas_and_completes_with_decorated_snapshot() -> None:
    adapter = _DialogAdapter(
        [
            _frame(
                {
                    "code": 0,
                    "data": {
                        "answer": "foo ",
                        "final": False,
                        "session_id": "dialog-session",
                    },
                }
            ),
            _frame(
                {
                    "code": 0,
                    "data": {
                        "answer": "bar baz",
                        "final": False,
                        "session_id": "dialog-session",
                    },
                }
            ),
            _frame(
                {
                    "code": 0,
                    "data": {
                        "answer": "foo bar ##0$$ baz",
                        "final": True,
                        "session_id": "dialog-session",
                    },
                }
            ),
            _frame({"code": 0, "data": True}),
        ]
    )
    executor = MultiRAGDialogExecutor(adapter)

    events = await executor.execute(
        context=_context(target_type="multirag.dialog", revision_id=None),
        command=_command(),
    )

    assert [event.model_dump(exclude_none=True) for event in await _collect(events)] == [
        {"event": "message_delta", "content": "foo ", "session_id": "dialog-session"},
        {"event": "message_delta", "content": "bar baz", "session_id": "dialog-session"},
        {
            "event": "message_completed",
            "content": "foo bar ##0$$ baz",
            "session_id": "dialog-session",
        },
    ]


async def test_dialog_executor_never_exposes_split_or_uppercase_reasoning_markers() -> None:
    adapter = _DialogAdapter(
        [
            _frame(
                {
                    "code": 0,
                    "data": {
                        "answer": "<THI",
                        "final": False,
                        "session_id": "dialog-session",
                    },
                }
            ),
            _frame(
                {
                    "code": 0,
                    "data": {
                        "answer": "NK>private reasoning</TH",
                        "final": False,
                        "session_id": "dialog-session",
                    },
                }
            ),
            _frame(
                {
                    "code": 0,
                    "data": {
                        "answer": "INK>visible answer",
                        "final": False,
                        "session_id": "dialog-session",
                    },
                }
            ),
            _frame(
                {
                    "code": 0,
                    "data": {
                        "answer": "visible answer",
                        "final": True,
                        "session_id": "dialog-session",
                    },
                }
            ),
            _frame({"code": 0, "data": True}),
        ]
    )
    executor = MultiRAGDialogExecutor(adapter)

    events = await executor.execute(
        context=_context(target_type="multirag.dialog", revision_id=None),
        command=_command(),
    )
    payloads = [event.model_dump(exclude_none=True) for event in await _collect(events)]

    assert payloads == [
        {
            "event": "message_delta",
            "content": "visible answer",
            "session_id": "dialog-session",
        },
        {
            "event": "message_completed",
            "content": "visible answer",
            "session_id": "dialog-session",
        },
    ]
    assert "private reasoning" not in str(payloads)


async def test_target_executors_close_driver_stream_when_consumer_stops() -> None:
    canvas_adapter = _CanvasAdapter(
        [
            _frame({"event": "message", "data": {"content": "partial"}, "session_id": "canvas-session"}),
            _frame({"event": "message_end", "data": {}, "session_id": "canvas-session"}),
        ]
    )
    canvas_events = await MultiRAGCanvasAgentExecutor(canvas_adapter).execute(
        context=_context(),
        command=_command(),
    )
    assert (await anext(canvas_events)).content == "partial"
    await canvas_events.aclose()
    assert canvas_adapter.closed is True

    dialog_adapter = _DialogAdapter(
        [
            _frame(
                {
                    "code": 0,
                    "data": {
                        "answer": "partial",
                        "final": False,
                        "session_id": "dialog-session",
                    },
                }
            ),
            _frame(
                {
                    "code": 0,
                    "data": {
                        "answer": "complete",
                        "final": True,
                        "session_id": "dialog-session",
                    },
                }
            ),
            _frame({"code": 0, "data": True}),
        ]
    )
    dialog_events = await MultiRAGDialogExecutor(dialog_adapter).execute(
        context=_context(target_type="multirag.dialog", revision_id=None),
        command=_command(),
    )
    assert (await anext(dialog_events)).content == "partial"
    await dialog_events.aclose()
    assert dialog_adapter.closed is True


async def test_dialog_regenerate_forwards_explicit_operation_to_existing_session() -> None:
    adapter = _DialogAdapter()
    executor = MultiRAGDialogExecutor(adapter)
    context = _context(
        target_type="multirag.dialog",
        revision_id=None,
        session_id="dialog-session",
    )

    events = await executor.execute(
        context=context,
        command=_command(operation="regenerate"),
    )

    assert [event.event for event in await _collect(events)] == ["message_delta", "message_completed"]
    assert adapter.sessions == ["dialog-session"]
    assert adapter.operations == ["regenerate"]


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
