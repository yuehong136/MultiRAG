"""Injectable contracts at the Channel control/data-plane boundary."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from fastapi import Request

from api.channel_capabilities import TargetCapabilities
from api.channel_execution.models import (
    ChannelExecutionCommand,
    ExecutionEvent,
    ExecutionOperation,
    ExecutionTargetRef,
    TrustedChannelContext,
    WorkloadIdentity,
)
from api.channel_execution.session_models import PreparedCanvasExecution, PreparedDialogExecution


@runtime_checkable
class TargetExecutor(Protocol):
    """Executes one kind of MultiRAG-owned published target."""

    @property
    def target_type(self) -> str: ...

    async def capabilities(
        self,
        *,
        context: TrustedChannelContext,
    ) -> TargetCapabilities: ...

    async def execute(
        self,
        *,
        context: TrustedChannelContext,
        command: ChannelExecutionCommand,
    ) -> AsyncIterator[ExecutionEvent]: ...


@runtime_checkable
class BindingResolver(Protocol):
    """Resolves all trusted execution fields from server-side binding state."""

    async def resolve(
        self,
        *,
        binding_id: str,
        workload: WorkloadIdentity,
        command: ChannelExecutionCommand,
    ) -> TrustedChannelContext | None: ...


@runtime_checkable
class BindingCapabilityResolver(Protocol):
    """Resolves the trusted binding scope for a startup capability preflight."""

    async def resolve_capabilities(
        self,
        *,
        binding_id: str,
        workload: WorkloadIdentity,
    ) -> TrustedChannelContext | None: ...


@runtime_checkable
class ChannelPrincipalResolver(Protocol):
    """Promotes a linked, provider-verified actor after event ownership."""

    async def resolve(
        self,
        *,
        context: TrustedChannelContext,
        command: ChannelExecutionCommand,
    ) -> TrustedChannelContext: ...


@runtime_checkable
class ChannelConversationStore(Protocol):
    """Persists the trusted external-conversation to target-session mapping."""

    async def get_session(
        self,
        *,
        binding_id: str,
        binding_generation: int,
        conversation_key: str,
        tenant_id: str,
        principal_id: str | None,
    ) -> str | None: ...

    async def put_session(
        self,
        *,
        binding_id: str,
        binding_generation: int,
        conversation_key: str,
        session_id: str,
        tenant_id: str,
        principal_id: str | None,
    ) -> None: ...

    async def reset_session(
        self,
        *,
        binding_id: str,
        binding_generation: int,
        conversation_key: str,
    ) -> None: ...


@runtime_checkable
class ExecutionClaimStore(Protocol):
    """Atomically owns event execution across all Channel runner replicas."""

    async def claim(self, *, binding_id: str, event_id: str) -> bool: ...

    async def complete(self, *, binding_id: str, event_id: str) -> None: ...

    async def fail(self, *, binding_id: str, event_id: str) -> None: ...


@runtime_checkable
class WorkloadAuthenticator(Protocol):
    """Authenticates a Channel runner independently of end-user credentials."""

    async def authenticate(self, request: Request) -> WorkloadIdentity: ...


@runtime_checkable
class CanvasHistoryTransaction(Protocol):
    """Canvas-private candidate transaction around MultiRAG history."""

    async def prepare(
        self,
        *,
        target_id: str,
        session_id: str | None,
        question: str,
        operation: ExecutionOperation,
        user_id: str | None = None,
    ) -> PreparedCanvasExecution: ...

    async def commit(
        self,
        prepared: PreparedCanvasExecution,
        generated_session_id: str,
    ) -> str: ...

    async def abort(
        self,
        prepared: PreparedCanvasExecution,
        generated_session_id: str | None,
    ) -> None: ...


@runtime_checkable
class DialogHistoryTransaction(Protocol):
    """Dialog-private detached transaction around MultiRAG history."""

    async def prepare(
        self,
        *,
        target_id: str,
        session_id: str | None,
        question: str,
        operation: ExecutionOperation,
        user_id: str | None = None,
    ) -> PreparedDialogExecution: ...

    async def commit(self, prepared: PreparedDialogExecution) -> str: ...

    async def abort(self, prepared: PreparedDialogExecution) -> None: ...


@runtime_checkable
class CanvasTargetDriver(Protocol):
    """Target-private driver over the existing published Canvas service."""

    async def capabilities(
        self,
        *,
        tenant_id: str,
        target: ExecutionTargetRef,
    ) -> TargetCapabilities: ...

    async def validate_revision(
        self,
        *,
        tenant_id: str,
        target: ExecutionTargetRef,
    ) -> None: ...

    def stream(
        self,
        *,
        tenant_id: str,
        target: ExecutionTargetRef,
        question: str,
        session_id: str | None,
        principal_id: str | None,
        operation: ExecutionOperation,
    ) -> AsyncIterator[str]: ...


@runtime_checkable
class DialogTargetDriver(Protocol):
    """Target-private driver over the existing MultiRAG Dialog service."""

    def stream(
        self,
        *,
        tenant_id: str,
        target: ExecutionTargetRef,
        question: str,
        session_id: str | None,
        principal_id: str | None,
        operation: ExecutionOperation,
    ) -> AsyncIterator[str]: ...
