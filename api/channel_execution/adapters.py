"""Production adapters for trusted binding resolution and distributed state."""

from __future__ import annotations

import hashlib
from typing import Protocol, runtime_checkable

from api.channel_execution.models import (
    ChannelExecutionCommand,
    ExecutionTargetRef,
    TrustedChannelContext,
    WorkloadIdentity,
)
from api.db.db_models import ChannelBinding, ChannelSecret, ChatChannel

_STATE_PREFIX = "multirag:channel-execution:v1"
_PROCESSING_TTL_SECONDS = 600
_DEFAULT_STATE_TTL_SECONDS = 86_400


@runtime_checkable
class AsyncExecutionRedis(Protocol):
    """Minimal redis.asyncio surface used by the execution boundary."""

    async def set(
        self,
        name: str,
        value: str,
        *,
        ex: int | None = None,
        nx: bool = False,
    ) -> bool | None: ...

    async def get(self, name: str) -> bytes | str | None: ...

    async def delete(self, *names: str) -> int: ...


@runtime_checkable
class RuntimeBindingRepository(Protocol):
    """Least-privilege repository surface needed during execution."""

    async def get_runtime_binding(
        self,
        binding_id: str,
        *,
        for_update: bool = False,
    ) -> tuple[ChatChannel, ChannelBinding, ChannelSecret | None] | None: ...


def _opaque_key(*values: str) -> str:
    digest = hashlib.sha256()
    for value in values:
        encoded = value.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, byteorder="big"))
        digest.update(encoded)
    return digest.hexdigest()


class SqlAlchemyBindingResolver:
    """Resolve execution authority exclusively from MultiRAG control state."""

    def __init__(self, repository: RuntimeBindingRepository) -> None:
        self._repository = repository

    async def resolve(
        self,
        *,
        binding_id: str,
        workload: WorkloadIdentity,
        command: ChannelExecutionCommand,
    ) -> TrustedChannelContext | None:
        if not workload.subject.strip() or workload.binding_id != binding_id:
            return None
        bundle = await self._repository.get_runtime_binding(binding_id)
        if bundle is None:
            return None
        channel, binding, _secret = bundle
        if binding.channel_id != channel.id or workload.binding_generation != binding.generation or command.actor.provider != channel.channel:
            return None
        try:
            target = ExecutionTargetRef(
                target_type=binding.target_type,
                target_id=binding.target_id,
                revision_id=binding.target_revision_id,
            )
        except ValueError:
            return None
        return TrustedChannelContext(
            binding_id=binding.id,
            tenant_id=channel.tenant_id,
            target=target,
            enabled=bool(channel.status == 1 and binding.enabled),
            binding_generation=binding.generation,
            # External actor identity is deliberately not promoted to a
            # MultiRAG principal. A later verified identity mapper can set it.
            principal_id=None,
        )


class RedisChannelExecutionStateStore:
    """Redis-backed conversation state and server-side execution ownership."""

    def __init__(
        self,
        redis: AsyncExecutionRedis,
        *,
        session_ttl_seconds: int = _DEFAULT_STATE_TTL_SECONDS,
        dedupe_ttl_seconds: int = _DEFAULT_STATE_TTL_SECONDS,
    ) -> None:
        if session_ttl_seconds < 1 or dedupe_ttl_seconds < 1:
            raise ValueError("channel execution state TTLs must be positive")
        self._redis = redis
        self._session_ttl_seconds = session_ttl_seconds
        self._dedupe_ttl_seconds = dedupe_ttl_seconds

    async def get_session(
        self,
        *,
        binding_id: str,
        binding_generation: int,
        conversation_key: str,
    ) -> str | None:
        value = await self._redis.get(self._session_key(binding_id, binding_generation, conversation_key))
        if value is None:
            return None
        if isinstance(value, bytes):
            return value.decode("utf-8")
        if isinstance(value, str):
            return value
        raise TypeError("Redis returned an invalid channel session value")

    async def put_session(
        self,
        *,
        binding_id: str,
        binding_generation: int,
        conversation_key: str,
        session_id: str,
    ) -> None:
        if not session_id:
            raise ValueError("channel session ID must not be empty")
        await self._redis.set(
            self._session_key(binding_id, binding_generation, conversation_key),
            session_id,
            ex=self._session_ttl_seconds,
        )

    async def reset_session(
        self,
        *,
        binding_id: str,
        binding_generation: int,
        conversation_key: str,
    ) -> None:
        await self._redis.delete(self._session_key(binding_id, binding_generation, conversation_key))

    async def claim(self, *, binding_id: str, event_id: str) -> bool:
        claimed = await self._redis.set(
            self._event_key(binding_id, event_id),
            "processing",
            ex=_PROCESSING_TTL_SECONDS,
            nx=True,
        )
        return bool(claimed)

    async def complete(self, *, binding_id: str, event_id: str) -> None:
        await self._redis.set(
            self._event_key(binding_id, event_id),
            "completed",
            ex=self._dedupe_ttl_seconds,
        )

    async def fail(self, *, binding_id: str, event_id: str) -> None:
        # Execution may already have reached an MCP tool when the stream fails.
        # Keep a full-window tombstone instead of permitting an unsafe replay.
        await self._redis.set(
            self._event_key(binding_id, event_id),
            "executed",
            ex=self._dedupe_ttl_seconds,
        )

    @staticmethod
    def _session_key(binding_id: str, binding_generation: int, conversation_key: str) -> str:
        if binding_generation < 1:
            raise ValueError("channel binding generation must be positive")
        return f"{_STATE_PREFIX}:session:{_opaque_key(binding_id, str(binding_generation), conversation_key)}"

    @staticmethod
    def _event_key(binding_id: str, event_id: str) -> str:
        return f"{_STATE_PREFIX}:event:{_opaque_key(binding_id, event_id)}"
