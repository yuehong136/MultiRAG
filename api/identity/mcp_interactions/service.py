"""Persistence host and bounded recovery worker for EIM-U14."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

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


@runtime_checkable
class InteractionResumeExecutor(Protocol):
    """Must rehydrate live authority and issue a new operation bearer."""

    async def execute(self, resume: InteractionResume) -> object: ...


@dataclass(frozen=True, slots=True)
class InteractionServiceLimits:
    lease_seconds: int
    batch_size: int
    max_rounds: int
    max_payload_bytes: int
    ttl_seconds: int = 600

    def __post_init__(self) -> None:
        if any(type(value) is not int or value <= 0 for value in (self.lease_seconds, self.batch_size, self.max_rounds, self.max_payload_bytes, self.ttl_seconds)):
            raise ValueError("interaction service limits are invalid")


class _InteractionLeaseLost(RuntimeError):
    """The worker no longer owns the durable resume attempt."""


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

    @property
    def ttl_seconds(self) -> int:
        return self._limits.ttl_seconds

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
        processed = 0
        while processed < self._limits.batch_size:
            async with self._repository() as repository:
                leases = await repository.lease_ready(
                    owner=owner,
                    lease_seconds=self._limits.lease_seconds,
                    limit=1,
                )
            if not leases:
                break
            lease = leases[0]
            await self._execute_lease(lease=lease, executor=executor)
            processed += 1
        return processed

    async def _execute_lease(
        self,
        *,
        lease: InteractionLease,
        executor: InteractionResumeExecutor,
    ) -> None:
        try:
            result = await self._execute_with_renewal(
                lease=lease,
                executor=executor,
            )
        except MCPInteractionPaused:
            # The connector already persisted and atomically advanced the same
            # interaction.  The previous leased job was completed by pause().
            return
        except _InteractionLeaseLost:
            logging.warning(
                "MCP interaction lease lost (revision=%s, attempt=%s)",
                lease.resume.revision,
                lease.attempt,
            )
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
                try:
                    async with self._repository() as repository:
                        await repository.terminal_fail(lease=lease, code=exc.code)
                except InteractionStateError:
                    logging.warning(
                        "MCP interaction completion lost its lease (revision=%s, attempt=%s)",
                        lease.resume.revision,
                        lease.attempt,
                    )

    async def _execute_with_renewal(
        self,
        *,
        lease: InteractionLease,
        executor: InteractionResumeExecutor,
    ) -> object:
        execution_task = asyncio.create_task(
            executor.execute(lease.resume),
            name=f"mcp-interaction-execute-{lease.resume.revision}",
        )
        renewal_task = asyncio.create_task(
            self._renew_lease(lease),
            name=f"mcp-interaction-renew-{lease.resume.revision}",
        )
        try:
            done, _pending = await asyncio.wait(
                (execution_task, renewal_task),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if execution_task in done:
                return await execution_task
            try:
                await renewal_task
            except Exception as exc:
                raise _InteractionLeaseLost from exc
            raise _InteractionLeaseLost
        finally:
            pending = [task for task in (execution_task, renewal_task) if not task.done()]
            for task in pending:
                task.cancel()
            # Observe both tasks even when execution and renewal finish in the
            # same loop turn, otherwise a simultaneous renewal failure can be
            # left as an unhandled task exception.
            await asyncio.gather(
                execution_task,
                renewal_task,
                return_exceptions=True,
            )

    async def _renew_lease(self, lease: InteractionLease) -> None:
        interval = max(0.05, self._limits.lease_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            async with self._repository() as repository:
                await repository.renew(
                    lease=lease,
                    lease_seconds=self._limits.lease_seconds,
                )

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
