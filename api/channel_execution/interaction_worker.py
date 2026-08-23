"""Durable callback processor for native Channel interaction forms.

The Provider callback was acknowledged only after
:mod:`api.channel_execution.interaction_presentations` durably stored it.  This
worker therefore performs the slower current-binding lookup, I4 identity
reverification, and U14 response claim outside the Provider callback request.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from sqlalchemy.exc import SQLAlchemyError

from api.channel_execution.errors import ChannelIdentityResolutionError
from api.channel_execution.interaction_presentations import CallbackLease
from api.channel_execution.models import ChannelActor, TrustedChannelContext, WorkloadIdentity
from api.channel_execution.protocols import BindingCapabilityResolver
from api.identity.mcp_interactions.contracts import (
    InteractionErrorCode,
    InteractionStateError,
    ResponseClaim,
)
from api.identity.principal import Principal


class InteractionCallbackWorkerErrorCode(StrEnum):
    """Closed, non-sensitive worker failures safe to persist and render."""

    BINDING_REVOKED = "INTERACTION_BINDING_REVOKED"
    PROVIDER_MISMATCH = "INTERACTION_PROVIDER_MISMATCH"
    AUTHORIZATION_REJECTED = "INTERACTION_AUTHORIZATION_REJECTED"
    PROCESSING_UNAVAILABLE = "INTERACTION_CALLBACK_UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class InteractionCallbackWorkerLimits:
    lease_seconds: int
    retry_max_seconds: int = 60
    reconcile_limit: int = 20

    def __post_init__(self) -> None:
        if (
            type(self.lease_seconds) is not int
            or self.lease_seconds < 1
            or type(self.retry_max_seconds) is not int
            or self.retry_max_seconds < 1
            or type(self.reconcile_limit) is not int
            or self.reconcile_limit < 1
        ):
            raise ValueError("interaction callback worker limits are invalid")


@runtime_checkable
class CallbackActorResolver(Protocol):
    """Action-specific surface implemented by ChannelIdentityResolver."""

    async def resolve_actor(
        self,
        *,
        context: TrustedChannelContext,
        actor: ChannelActor,
        expected_provider_account_id: str | None,
    ) -> TrustedChannelContext: ...


@runtime_checkable
class CallbackPresentationStore(Protocol):
    """Short-session surface implemented by InteractionPresentationService."""

    async def lease_callback(
        self,
        *,
        owner: str,
        lease_seconds: int,
    ) -> CallbackLease | None: ...

    async def mark_callback_claimed(self, lease: CallbackLease) -> None: ...

    async def reject_callback(
        self,
        lease: CallbackLease,
        *,
        code: str,
        terminal: bool = False,
    ) -> None: ...

    async def retry_callback(
        self,
        lease: CallbackLease,
        *,
        code: str,
        delay_seconds: int,
    ) -> None: ...

    async def renew_callback(
        self,
        lease: CallbackLease,
        *,
        lease_seconds: int,
    ) -> None: ...

    async def reconcile(self, *, limit: int = 20) -> int: ...


@runtime_checkable
class CallbackInteractionStore(Protocol):
    """U14 submission surface implemented by PersistentInteractionService."""

    async def submit_response(
        self,
        *,
        principal: Principal,
        interaction_id: str,
        revision: int,
        response_idempotency_key: str,
        input_responses: Mapping[str, Any],
    ) -> ResponseClaim: ...


@runtime_checkable
class InteractionCallbackPoller(Protocol):
    """Minimal poll-loop surface for production composition and tests."""

    async def run_once(self, *, owner: str) -> int: ...


class _CallbackLeaseLost(RuntimeError):
    """The processor no longer owns the durable callback receipt."""


_TRANSIENT_IDENTITY_CODES = frozenset(
    {
        "IDENTITY_POLICY_UNAVAILABLE",
        "IDENTITY_PROVIDER_CREDENTIAL_UNAVAILABLE",
        "IDENTITY_PROVIDER_UNAVAILABLE",
        "IDENTITY_REPOSITORY_UNAVAILABLE",
    }
)
_REOPEN_IDENTITY_CODES = frozenset(
    {
        "IDENTITY_ASSERTION_INVALID",
        "IDENTITY_PROVIDER_MISMATCH",
        "IDENTITY_TENANT_MISMATCH",
    }
)
_REOPEN_INTERACTION_CODES = frozenset(
    {
        InteractionErrorCode.ACTOR_MISMATCH,
        InteractionErrorCode.REAUTHORIZATION_DENIED,
        InteractionErrorCode.RESPONSE_INVALID,
    }
)


class InteractionCallbackProcessor:
    """Claim at most one callback and re-authorize it against current state.

    Production composition supplies a ``SessionFactoryBindingResolver`` via
    the narrow ``BindingCapabilityResolver`` protocol.  Every call therefore
    observes the current binding generation rather than a request snapshot.
    """

    def __init__(
        self,
        *,
        presentations: CallbackPresentationStore,
        interactions: CallbackInteractionStore,
        binding_resolver: BindingCapabilityResolver,
        actor_resolver: CallbackActorResolver,
        limits: InteractionCallbackWorkerLimits,
    ) -> None:
        self._presentations = presentations
        self._interactions = interactions
        self._binding_resolver = binding_resolver
        self._actor_resolver = actor_resolver
        self._limits = limits

    async def run_once(self, *, owner: str) -> int:
        """Process one just-in-time lease; reconcile presentation state idle."""

        lease = await self._presentations.lease_callback(
            owner=owner,
            lease_seconds=self._limits.lease_seconds,
        )
        if lease is None:
            await self._presentations.reconcile(limit=self._limits.reconcile_limit)
            return 0
        await self._execute_lease(lease)
        return 1

    async def _execute_lease(self, lease: CallbackLease) -> None:
        try:
            await self._execute_with_renewal(lease)
        except asyncio.CancelledError:
            raise
        except _CallbackLeaseLost:
            logging.warning(
                "Channel interaction callback lease lost (revision=%s, attempt=%s)",
                lease.revision,
                lease.attempt,
            )
        except (TimeoutError, ConnectionError, SQLAlchemyError) as exc:
            await self._retry(
                lease,
                code=InteractionCallbackWorkerErrorCode.PROCESSING_UNAVAILABLE.value,
                failure=exc,
            )
        except Exception as exc:
            # Unknown processing failures are bounded by the durable attempt
            # cap in retry_callback.  Persist only a closed code and exception
            # type; actor material, event digests, and lease tokens stay out of
            # logs.
            await self._retry(
                lease,
                code=InteractionCallbackWorkerErrorCode.PROCESSING_UNAVAILABLE.value,
                failure=exc,
            )

    async def _execute_with_renewal(self, lease: CallbackLease) -> None:
        processing = asyncio.create_task(
            self._process(lease),
            name=f"channel-interaction-callback-{lease.revision}",
        )
        renewal = asyncio.create_task(
            self._renew(lease),
            name=f"channel-interaction-callback-renew-{lease.revision}",
        )
        try:
            done, _pending = await asyncio.wait(
                (processing, renewal),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if processing in done:
                await processing
                return
            try:
                await renewal
            except Exception as exc:
                raise _CallbackLeaseLost from exc
            raise _CallbackLeaseLost
        finally:
            pending = [task for task in (processing, renewal) if not task.done()]
            for task in pending:
                task.cancel()
            # Gather both tasks, including a renewal that failed in the same
            # event-loop turn as a successful claim, so no background task
            # exception is left unobserved.
            await asyncio.gather(processing, renewal, return_exceptions=True)

    async def _renew(self, lease: CallbackLease) -> None:
        interval = max(0.05, self._limits.lease_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            await self._presentations.renew_callback(
                lease,
                lease_seconds=self._limits.lease_seconds,
            )

    async def _process(self, lease: CallbackLease) -> None:
        context = await self._binding_resolver.resolve_capabilities(
            binding_id=lease.binding_id,
            workload=WorkloadIdentity(
                subject="multirag-interaction-callback-worker",
                binding_id=lease.binding_id,
                binding_generation=lease.binding_generation,
            ),
        )
        if context is None or context.binding_id != lease.binding_id or context.binding_generation != lease.binding_generation or not context.enabled:
            await self._presentations.reject_callback(
                lease,
                code=InteractionCallbackWorkerErrorCode.BINDING_REVOKED.value,
                terminal=True,
            )
            return
        if context.provider != lease.actor.provider:
            await self._presentations.reject_callback(
                lease,
                code=InteractionCallbackWorkerErrorCode.PROVIDER_MISMATCH.value,
                terminal=False,
            )
            return
        try:
            trusted = await self._actor_resolver.resolve_actor(
                context=context,
                actor=lease.actor,
                expected_provider_account_id=lease.provider_account_id,
            )
        except ChannelIdentityResolutionError as exc:
            await self._handle_identity_rejection(lease, exc.code)
            return
        principal = trusted.principal
        if principal is None:
            await self._presentations.reject_callback(
                lease,
                code=InteractionCallbackWorkerErrorCode.AUTHORIZATION_REJECTED.value,
                terminal=True,
            )
            return
        try:
            await self._submit(lease, principal)
        except InteractionStateError as exc:
            if exc.code is InteractionErrorCode.REMOTE_RESULT_UNKNOWN:
                raise
            await self._presentations.reject_callback(
                lease,
                code=exc.code.value,
                terminal=exc.code not in _REOPEN_INTERACTION_CODES,
            )
            return
        await self._presentations.mark_callback_claimed(lease)

    async def _submit(self, lease: CallbackLease, principal: Principal) -> None:
        # Both ACCEPTED and DUPLICATE are successful durable claims.  A crash
        # after U14 commit but before mark_callback_claimed safely returns the
        # DUPLICATE path on the next receipt attempt.
        await self._interactions.submit_response(
            principal=principal,
            interaction_id=lease.interaction_id,
            revision=lease.revision,
            response_idempotency_key=lease.idempotency_key,
            input_responses=lease.input_responses,
        )

    async def _handle_identity_rejection(
        self,
        lease: CallbackLease,
        code: str,
    ) -> None:
        if code in _TRANSIENT_IDENTITY_CODES:
            raise ConnectionError("identity reauthorization is temporarily unavailable")
        await self._presentations.reject_callback(
            lease,
            code=code,
            terminal=code not in _REOPEN_IDENTITY_CODES,
        )

    async def _retry(
        self,
        lease: CallbackLease,
        *,
        code: str,
        failure: BaseException,
    ) -> None:
        logging.error(
            "Channel interaction callback processing failed (type=%s, revision=%s, attempt=%s)",
            type(failure).__name__,
            lease.revision,
            lease.attempt,
        )
        delay = min(
            self._limits.retry_max_seconds,
            2 ** min(lease.attempt, 5),
        )
        try:
            await self._presentations.retry_callback(
                lease,
                code=code,
                delay_seconds=delay,
            )
        except Exception as exc:
            logging.error(
                "Channel interaction callback retry persistence failed (type=%s, revision=%s, attempt=%s)",
                type(exc).__name__,
                lease.revision,
                lease.attempt,
            )


async def run_interaction_callback_worker(
    *,
    processor: InteractionCallbackPoller,
    owner: str,
    poll_seconds: float,
    stopping: asyncio.Event,
) -> None:
    """Cooperative API-local poll loop; lifecycle composition lives elsewhere."""

    if not owner.strip() or len(owner) > 64 or poll_seconds <= 0:
        raise ValueError("interaction callback poll configuration is invalid")
    while not stopping.is_set():
        try:
            processed = await processor.run_once(owner=owner)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logging.error(
                "Channel interaction callback poll failed (type=%s)",
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
    "CallbackActorResolver",
    "CallbackInteractionStore",
    "CallbackPresentationStore",
    "InteractionCallbackPoller",
    "InteractionCallbackProcessor",
    "InteractionCallbackWorkerErrorCode",
    "InteractionCallbackWorkerLimits",
    "run_interaction_callback_worker",
]
