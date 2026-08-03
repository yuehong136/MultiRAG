"""Injectable contracts at the Channel control/data-plane boundary."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from fastapi import Request

from api.channel_execution.models import (
    ChannelExecutionCommand,
    ExecutionEvent,
    ExecutionTargetRef,
    TrustedChannelContext,
    WorkloadIdentity,
)


@runtime_checkable
class TargetExecutor(Protocol):
    """Executes one kind of MultiRAG-owned published target."""

    @property
    def target_type(self) -> str: ...

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
class ChannelConversationStore(Protocol):
    """Persists the trusted external-conversation to target-session mapping."""

    async def get_session(
        self,
        *,
        binding_id: str,
        binding_generation: int,
        conversation_key: str,
    ) -> str | None: ...

    async def put_session(
        self,
        *,
        binding_id: str,
        binding_generation: int,
        conversation_key: str,
        session_id: str,
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
class CanvasCompletionAdapter(Protocol):
    """Narrow adapter over the existing published Canvas execution service."""

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
    ) -> AsyncIterator[str]: ...


@runtime_checkable
class DialogCompletionAdapter(Protocol):
    """Narrow adapter over the existing MultiRAG Dialog service."""

    def stream(
        self,
        *,
        tenant_id: str,
        target: ExecutionTargetRef,
        question: str,
        session_id: str | None,
        principal_id: str | None,
    ) -> AsyncIterator[str]: ...
