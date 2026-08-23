"""U15 callback worker re-authorizes durable receipts before U14 claim."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest

from api.channel_execution.errors import ChannelIdentityResolutionError
from api.channel_execution.interaction_presentations import CallbackLease
from api.channel_execution.interaction_worker import (
    InteractionCallbackProcessor,
    InteractionCallbackWorkerLimits,
    run_interaction_callback_worker,
)
from api.channel_execution.models import (
    ChannelActor,
    ExecutionTargetRef,
    TrustedChannelContext,
    WorkloadIdentity,
)
from api.identity.contracts import (
    ExternalIdentityRecord,
    IdentityResolutionResult,
    IdentityResolutionStatus,
    UserMembershipRecord,
)
from api.identity.mcp_interactions.contracts import (
    InteractionErrorCode,
    InteractionStateError,
    ResponseClaim,
    ResponseClaimStatus,
)
from api.identity.principal import (
    AuthenticationContext,
    AuthenticationSource,
    IdentityAssurance,
    Principal,
    build_principal_from_resolved_identity,
)


def _principal() -> Principal:
    now = datetime.now(UTC)
    identity = ExternalIdentityRecord(
        id="external-identity",
        tenant_id="tenant-a",
        user_id="user-a",
        provider="feishu",
        provider_tenant_key="tenant-key",
        subject_type="user_id",
        subject_value="provider-user",
        state="active",
        verified_at=now,
        last_seen_at=now,
        identity_revision=3,
    )
    return build_principal_from_resolved_identity(
        result=IdentityResolutionResult(
            status=IdentityResolutionStatus.RESOLVED,
            identity=identity,
            membership=UserMembershipRecord(
                user_id="user-a",
                tenant_id="tenant-a",
                role="normal",
            ),
        ),
        authentication=AuthenticationContext(
            source=AuthenticationSource.ENTERPRISE_IDENTITY,
            assurance=IdentityAssurance.DIRECTORY_VERIFIED,
            validated_at=now,
            assurance_verified_at=now,
            provider="feishu",
            external_identity_id=identity.id,
        ),
    )


def _context(*, provider: str = "feishu", enabled: bool = True) -> TrustedChannelContext:
    return TrustedChannelContext(
        binding_id="binding-a",
        tenant_id="tenant-a",
        target=ExecutionTargetRef(
            target_type="multirag.canvas_agent",
            target_id="agent-a",
            revision_id="release-a",
        ),
        enabled=enabled,
        binding_generation=7,
        provider=provider,
    )


def _actor(*, provider: str = "feishu") -> ChannelActor:
    return ChannelActor.model_validate(
        {
            "provider": provider,
            "subject": "open-id-secret",
            "conversation": "chat-secret",
            "identity": {
                "provider": provider,
                "provider_tenant_key": "tenant-key",
                "identifiers": [
                    {"kind": "open_id", "value": "open-id-secret"},
                    {"kind": "user_id", "value": "provider-user"},
                ],
            },
        }
    )


def _lease(*, actor: ChannelActor | None = None, attempt: int = 1) -> CallbackLease:
    return CallbackLease(
        receipt_id="receipt-secret",
        owner="owner-secret",
        attempt=attempt,
        presentation_id="presentation-secret",
        binding_id="binding-a",
        binding_generation=7,
        provider_account_id="provider-account-secret",
        interaction_id="interaction-secret",
        revision=2,
        actor=actor or _actor(),
        action="accept",
        input_responses={"request-secret": {"action": "accept", "content": {"days": 2}}},
        idempotency_key="event-digest-secret",
    )


class _Presentations:
    def __init__(self, lease: CallbackLease | None) -> None:
        self.lease = lease
        self.leased = 0
        self.marked: list[CallbackLease] = []
        self.rejected: list[tuple[CallbackLease, str, bool]] = []
        self.retried: list[tuple[CallbackLease, str, int]] = []
        self.reconciled: list[int] = []
        self.renewed = asyncio.Event()
        self.renew_error: Exception | None = None

    async def lease_callback(
        self,
        *,
        owner: str,
        lease_seconds: int,
    ) -> CallbackLease | None:
        assert owner == "worker-a"
        assert lease_seconds == 1
        self.leased += 1
        selected, self.lease = self.lease, None
        return selected

    async def mark_callback_claimed(self, lease: CallbackLease) -> None:
        self.marked.append(lease)

    async def reject_callback(
        self,
        lease: CallbackLease,
        *,
        code: str,
        terminal: bool = False,
    ) -> None:
        self.rejected.append((lease, code, terminal))

    async def retry_callback(
        self,
        lease: CallbackLease,
        *,
        code: str,
        delay_seconds: int,
    ) -> None:
        self.retried.append((lease, code, delay_seconds))

    async def renew_callback(
        self,
        lease: CallbackLease,
        *,
        lease_seconds: int,
    ) -> None:
        del lease, lease_seconds
        self.renewed.set()
        if self.renew_error is not None:
            raise self.renew_error

    async def reconcile(self, *, limit: int = 20) -> int:
        self.reconciled.append(limit)
        return 0


class _BindingResolver:
    def __init__(self, context: TrustedChannelContext | None = None) -> None:
        self.context = _context() if context is None else context
        self.calls: list[tuple[str, WorkloadIdentity]] = []

    async def resolve_capabilities(
        self,
        *,
        binding_id: str,
        workload: WorkloadIdentity,
    ) -> TrustedChannelContext | None:
        self.calls.append((binding_id, workload))
        return self.context


class _ActorResolver:
    def __init__(self, *, error_code: str | None = None) -> None:
        self.error_code = error_code
        self.calls: list[tuple[TrustedChannelContext, ChannelActor, str | None]] = []

    async def resolve_actor(
        self,
        *,
        context: TrustedChannelContext,
        actor: ChannelActor,
        expected_provider_account_id: str | None,
    ) -> TrustedChannelContext:
        self.calls.append((context, actor, expected_provider_account_id))
        if self.error_code is not None:
            raise ChannelIdentityResolutionError(self.error_code)
        principal = _principal()
        return replace(
            context,
            principal_id=principal.platform_user_id,
            principal=principal,
        )


class _Interactions:
    def __init__(
        self,
        *,
        status: ResponseClaimStatus = ResponseClaimStatus.ACCEPTED,
        error: Exception | None = None,
        block: bool = False,
    ) -> None:
        self.status = status
        self.error = error
        self.block = block
        self.calls: list[dict[str, Any]] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def submit_response(self, **kwargs: Any) -> ResponseClaim:
        self.calls.append(kwargs)
        self.started.set()
        try:
            if self.block:
                await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        if self.error is not None:
            raise self.error
        return ResponseClaim(
            interaction_id="interaction-secret",
            revision=2,
            status=self.status,
        )


def _processor(
    *,
    presentations: _Presentations,
    interactions: _Interactions | None = None,
    binding_resolver: _BindingResolver | None = None,
    actor_resolver: _ActorResolver | None = None,
) -> InteractionCallbackProcessor:
    return InteractionCallbackProcessor(
        presentations=presentations,  # type: ignore[arg-type]
        interactions=interactions or _Interactions(),  # type: ignore[arg-type]
        binding_resolver=binding_resolver or _BindingResolver(),
        actor_resolver=actor_resolver or _ActorResolver(),
        limits=InteractionCallbackWorkerLimits(
            lease_seconds=1,
            retry_max_seconds=60,
            reconcile_limit=9,
        ),
    )


@pytest.mark.parametrize(
    "status",
    [ResponseClaimStatus.ACCEPTED, ResponseClaimStatus.DUPLICATE],
)
async def test_current_binding_actor_and_account_are_reverified_before_claim(
    status: ResponseClaimStatus,
) -> None:
    lease = _lease()
    presentations = _Presentations(lease)
    interactions = _Interactions(status=status)
    binding = _BindingResolver()
    actor = _ActorResolver()

    processed = await _processor(
        presentations=presentations,
        interactions=interactions,
        binding_resolver=binding,
        actor_resolver=actor,
    ).run_once(owner="worker-a")

    assert processed == 1
    assert presentations.marked == [lease]
    assert presentations.rejected == []
    assert presentations.retried == []
    assert binding.calls[0][0] == "binding-a"
    assert binding.calls[0][1] == WorkloadIdentity(
        subject="multirag-interaction-callback-worker",
        binding_id="binding-a",
        binding_generation=7,
    )
    assert actor.calls[0][1] is lease.actor
    assert actor.calls[0][2] == "provider-account-secret"
    assert interactions.calls[0]["interaction_id"] == "interaction-secret"
    assert interactions.calls[0]["revision"] == 2
    assert interactions.calls[0]["response_idempotency_key"] == "event-digest-secret"


@pytest.mark.parametrize("context", [None, _context(enabled=False)])
async def test_missing_or_disabled_current_binding_terminally_rejects(
    context: TrustedChannelContext | None,
) -> None:
    lease = _lease()
    presentations = _Presentations(lease)
    binding = _BindingResolver()
    binding.context = context
    actor = _ActorResolver()
    interactions = _Interactions()

    await _processor(
        presentations=presentations,
        interactions=interactions,
        binding_resolver=binding,
        actor_resolver=actor,
    ).run_once(owner="worker-a")

    assert presentations.rejected == [
        (lease, "INTERACTION_BINDING_REVOKED", True),
    ]
    assert actor.calls == []
    assert interactions.calls == []


async def test_provider_field_mismatch_reopens_form_without_identity_network() -> None:
    lease = _lease(actor=_actor(provider="other-provider"))
    presentations = _Presentations(lease)
    actor = _ActorResolver()

    await _processor(
        presentations=presentations,
        actor_resolver=actor,
    ).run_once(owner="worker-a")

    assert presentations.rejected == [
        (lease, "INTERACTION_PROVIDER_MISMATCH", False),
    ]
    assert actor.calls == []


@pytest.mark.parametrize(
    ("code", "terminal"),
    [
        ("IDENTITY_ASSERTION_INVALID", False),
        ("IDENTITY_REVISION_CONFLICT", True),
        ("IDENTITY_INACTIVE", True),
    ],
)
async def test_deterministic_identity_rejection_rerenders_form_or_terminal(
    code: str,
    terminal: bool,
) -> None:
    lease = _lease()
    presentations = _Presentations(lease)

    await _processor(
        presentations=presentations,
        actor_resolver=_ActorResolver(error_code=code),
    ).run_once(owner="worker-a")

    assert presentations.rejected == [(lease, code, terminal)]
    assert presentations.retried == []


async def test_transient_identity_failure_is_retried_with_durable_backoff() -> None:
    lease = _lease(attempt=3)
    presentations = _Presentations(lease)

    await _processor(
        presentations=presentations,
        actor_resolver=_ActorResolver(error_code="IDENTITY_PROVIDER_UNAVAILABLE"),
    ).run_once(owner="worker-a")

    assert presentations.rejected == []
    assert presentations.retried == [
        (lease, "INTERACTION_CALLBACK_UNAVAILABLE", 8),
    ]


@pytest.mark.parametrize(
    ("code", "terminal"),
    [
        (InteractionErrorCode.RESPONSE_INVALID, False),
        (InteractionErrorCode.ACTOR_MISMATCH, False),
        (InteractionErrorCode.REVISION_CONFLICT, True),
        (InteractionErrorCode.EXPIRED, True),
    ],
)
async def test_u14_deterministic_rejection_rerenders_form_or_terminal(
    code: InteractionErrorCode,
    terminal: bool,
) -> None:
    lease = _lease()
    presentations = _Presentations(lease)

    await _processor(
        presentations=presentations,
        interactions=_Interactions(error=InteractionStateError(code)),
    ).run_once(owner="worker-a")

    assert presentations.rejected == [(lease, code.value, terminal)]
    assert presentations.marked == []


async def test_slow_submission_renews_callback_lease_until_claimed() -> None:
    lease = _lease()
    presentations = _Presentations(lease)
    interactions = _Interactions(block=True)
    task = asyncio.create_task(
        _processor(
            presentations=presentations,
            interactions=interactions,
        ).run_once(owner="worker-a")
    )

    await asyncio.wait_for(interactions.started.wait(), timeout=1)
    await asyncio.wait_for(presentations.renewed.wait(), timeout=1)
    interactions.release.set()
    assert await task == 1
    assert presentations.marked == [lease]


async def test_renewal_failure_cancels_processing_without_stale_write() -> None:
    lease = _lease()
    presentations = _Presentations(lease)
    presentations.renew_error = ConnectionError("renewal-secret")
    interactions = _Interactions(block=True)

    processed = await _processor(
        presentations=presentations,
        interactions=interactions,
    ).run_once(owner="worker-a")

    assert processed == 1
    assert interactions.cancelled.is_set()
    assert presentations.marked == []
    assert presentations.rejected == []
    assert presentations.retried == []


async def test_idle_tick_reconciles_then_returns_for_poll_sleep() -> None:
    presentations = _Presentations(None)

    processed = await _processor(
        presentations=presentations,
    ).run_once(owner="worker-a")

    assert processed == 0
    assert presentations.reconciled == [9]


async def test_unknown_failure_logs_type_only_and_schedules_retry(
    caplog: pytest.LogCaptureFixture,
) -> None:
    lease = _lease()
    presentations = _Presentations(lease)
    caplog.set_level(logging.ERROR)

    await _processor(
        presentations=presentations,
        interactions=_Interactions(error=RuntimeError("actor-and-token-secret")),
    ).run_once(owner="worker-a")

    assert presentations.retried == [
        (lease, "INTERACTION_CALLBACK_UNAVAILABLE", 2),
    ]
    rendered = caplog.text
    assert "RuntimeError" in rendered
    assert "actor-and-token-secret" not in rendered
    assert "open-id-secret" not in rendered
    assert "event-digest-secret" not in rendered


async def test_poll_worker_stops_cooperatively_after_idle_tick() -> None:
    stopping = asyncio.Event()

    class _Processor:
        calls = 0

        async def run_once(self, *, owner: str) -> int:
            assert owner == "worker-a"
            self.calls += 1
            stopping.set()
            return 0

    processor = _Processor()

    await run_interaction_callback_worker(
        processor=processor,  # type: ignore[arg-type]
        owner="worker-a",
        poll_seconds=0.01,
        stopping=stopping,
    )

    assert processor.calls == 1
