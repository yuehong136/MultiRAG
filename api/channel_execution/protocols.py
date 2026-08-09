"""Injectable contracts at the Channel control/data-plane boundary."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from fastapi import Request

from api.channel_execution.models import (
    ChannelExecutionCommand,
    ExecutionEvent,
    ExecutionOperation,
    ExecutionTargetRef,
    TrustedChannelContext,
    WorkloadIdentity,
)
from api.channel_execution.session_models import PreparedChannelSession


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
class ChannelSessionManager(Protocol):
    """Owns Channel copy-on-write history without extending upstream services."""

    async def prepare_canvas(
        self,
        *,
        target_id: str,
        session_id: str | None,
        question: str,
        operation: ExecutionOperation,
    ) -> PreparedChannelSession: ...

    async def prepare_dialog(
        self,
        *,
        target_id: str,
        session_id: str | None,
        question: str,
        operation: ExecutionOperation,
    ) -> PreparedChannelSession: ...

    async def complete_canvas(
        self,
        prepared: PreparedChannelSession,
        generated_session_id: str,
    ) -> str: ...

    async def complete_dialog(
        self,
        prepared: PreparedChannelSession,
        generated_session_id: str,
        *,
        require_visible_answer: bool,
    ) -> str: ...

    async def abort(
        self,
        prepared: PreparedChannelSession,
        generated_session_id: str | None,
    ) -> None: ...


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
        operation: ExecutionOperation,
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
        operation: ExecutionOperation,
    ) -> AsyncIterator[str]: ...
