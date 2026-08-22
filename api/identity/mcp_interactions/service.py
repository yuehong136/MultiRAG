"""Persistence host and bounded recovery worker for EIM-U14."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.identity.mcp_interactions.contracts import (
    InteractionErrorCode,
    InteractionLease,
    InteractionProjection,
    InteractionStateError,
    ResponseClaim,
)
from api.identity.mcp_interactions.crypto import InteractionPayloadCipher
from api.identity.mcp_interactions.repository import InteractionRepository
from api.identity.principal import Principal
from common.mcp_interactions import (
    InteractionReceipt,
    InteractionRequest,
    InteractionResume,
    MCPInteractionPaused,
)


class InteractionResumeExecutor(Protocol):
    """Must rehydrate live authority and issue a new operation bearer."""

    async def execute(self, resume: InteractionResume) -> object: ...


@dataclass(frozen=True, slots=True)
class InteractionServiceLimits:
    lease_seconds: int
    batch_size: int
    max_rounds: int
    max_payload_bytes: int


class PersistentInteractionService:
    """Global handler with a new short AsyncSession for every operation."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        cipher: InteractionPayloadCipher,
        limits: InteractionServiceLimits,
    ) -> None:
        self._session_factory = session_factory
        self._cipher = cipher
        self._limits = limits

    async def pause(self, request: InteractionRequest) -> InteractionReceipt:
        async with self._repository() as repository:
            return await repository.pause(request)

    async def submit_response(
        self,
        *,
        principal: Principal,
        interaction_id: str,
        revision: int,
        response_idempotency_key: str,
        input_responses: Mapping[str, Any],
    ) -> ResponseClaim:
        expired: InteractionStateError | None = None
        claim: ResponseClaim | None = None
        async with self._session_factory() as session, session.begin():
            repository = self._new_repository(session)
            try:
                claim = await repository.submit_response(
                    principal=principal,
                    interaction_id=interaction_id,
                    revision=revision,
                    response_idempotency_key=response_idempotency_key,
                    input_responses=input_responses,
                )
            except InteractionStateError as exc:
                if exc.code is not InteractionErrorCode.EXPIRED:
                    raise
                expired = exc
        if expired is not None:
            raise expired
        assert claim is not None
        return claim

    async def projection(
        self,
        *,
        tenant_id: str,
        platform_user_id: str,
        interaction_id: str,
    ) -> InteractionProjection:
        async with self._repository() as repository:
            return await repository.projection(
                tenant_id=tenant_id,
                platform_user_id=platform_user_id,
                interaction_id=interaction_id,
            )

    async def run_once(
        self,
        *,
        owner: str,
        executor: InteractionResumeExecutor,
    ) -> int:
        async with self._repository() as repository:
            leases = await repository.lease_ready(
                owner=owner,
                lease_seconds=self._limits.lease_seconds,
                limit=self._limits.batch_size,
            )
        for lease in leases:
            await self._execute_lease(lease=lease, executor=executor)
        return len(leases)

    async def _execute_lease(
        self,
        *,
        lease: InteractionLease,
        executor: InteractionResumeExecutor,
    ) -> None:
        try:
            result = await executor.execute(lease.resume)
        except MCPInteractionPaused:
            # The connector already persisted and atomically advanced the same
            # interaction.  The previous leased job was completed by pause().
            return
        except InteractionStateError as exc:
            async with self._repository() as repository:
                await repository.terminal_fail(lease=lease, code=exc.code)
        except (TimeoutError, ConnectionError):
            async with self._repository() as repository:
                await repository.retry(
                    lease=lease,
                    delay_seconds=min(60, 2 ** min(lease.resume.revision, 5)),
                    code=InteractionErrorCode.REMOTE_RESULT_UNKNOWN,
                )
        except Exception as exc:
            logging.error(
                "MCP interaction resume failed (type=%s, revision=%s)",
                type(exc).__name__,
                lease.resume.revision,
            )
            async with self._repository() as repository:
                await repository.terminal_fail(
                    lease=lease,
                    code=InteractionErrorCode.REMOTE_RESULT_UNKNOWN,
                )
        else:
            try:
                async with self._repository() as repository:
                    await repository.complete(lease=lease, result=result)
            except InteractionStateError as exc:
                async with self._repository() as repository:
                    await repository.terminal_fail(lease=lease, code=exc.code)

    @asynccontextmanager
    async def _repository(self) -> AsyncIterator[InteractionRepository]:
        async with self._session_factory() as session, session.begin():
            yield self._new_repository(session)

    def _new_repository(self, session: AsyncSession) -> InteractionRepository:
        return InteractionRepository(
            session,
            cipher=self._cipher,
            max_rounds=self._limits.max_rounds,
            max_payload_bytes=self._limits.max_payload_bytes,
        )


async def run_interaction_worker(
    *,
    service: PersistentInteractionService,
    executor: InteractionResumeExecutor,
    owner: str,
    poll_seconds: float,
    stopping: asyncio.Event,
) -> None:
    """Cooperative API-local worker; durable leases make restarts recoverable."""

    while not stopping.is_set():
        try:
            processed = await service.run_once(owner=owner, executor=executor)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logging.error(
                "MCP interaction recovery poll failed (type=%s)",
                type(exc).__name__,
            )
            processed = 0
        if processed:
            continue
        try:
            await asyncio.wait_for(stopping.wait(), timeout=poll_seconds)
        except TimeoutError:
            pass


__all__ = [
    "InteractionResumeExecutor",
    "InteractionServiceLimits",
    "PersistentInteractionService",
    "run_interaction_worker",
]
