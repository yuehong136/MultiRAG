"""Closed orchestration tests for EIM-I8 reconciliation."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from beartype.roar import BeartypeCallHintParamViolation

import api.identity.reconciliation.service as reconciliation_service_module
from api.identity.contracts import ProviderContext
from api.identity.providers.contracts import (
    EnterpriseIdentityProvider,
    ExternalIdentityAssertion,
    ProviderDirectoryStatus,
    ProviderErrorCode,
    ProviderIdentity,
    ProviderIdentityResult,
    ProviderIdentityStatus,
)
from api.identity.reconciliation.contracts import (
    ReconciliationApplyOutcome,
    ReconciliationApplyResult,
    ReconciliationErrorCode,
    ReconciliationLease,
    ReconciliationObservation,
    ReconciliationProviderStatus,
    ReconciliationRepositoryError,
)
from api.identity.reconciliation.service import (
    IdentityReconciliationLimits,
    IdentityReconciliationService,
    ReconciliationProviderRegistry,
    ReconciliationRunStatus,
    ReconciliationSeedStatus,
)
from api.identity.reconciliation.telemetry import (
    InMemoryReconciliationTelemetry,
    ReconciliationTelemetryEvent,
)

NOW = datetime(2026, 8, 24, 1, 2, 3, tzinfo=UTC)


def _context() -> ProviderContext:
    return ProviderContext(
        tenant_id="tenant-sensitive",
        provider="feishu",
        provider_tenant_key="tenant-key-sensitive",
        provider_account_id="account-sensitive",
        provider_account_key="account-key-sensitive",
        provider_account_revision=7,
    )


def _lease(
    *,
    failures: int = 2,
    lease_until: datetime | None = None,
) -> ReconciliationLease:
    return ReconciliationLease(
        checkpoint_id="checkpoint-sensitive",
        target_id="target-sensitive",
        owner="owner-sensitive",
        attempt=11,
        context=_context(),
        external_identity_id="identity-sensitive",
        subject_value="provider-user-sensitive",
        identity_revision=3,
        consecutive_failures=failures,
        lease_until=lease_until or NOW + timedelta(seconds=30),
    )


def _limits() -> IdentityReconciliationLimits:
    return IdentityReconciliationLimits(
        lease_seconds=30,
        probe_safety_margin_seconds=5.0,
        probe_interval_seconds=1.0,
        cycle_interval_seconds=600,
        active_window_seconds=3_600,
        backoff_initial_seconds=5,
        backoff_max_seconds=30,
        not_found_confirmation_seconds=600,
        degrade_after_failures=3,
        max_tighten_per_cycle=10,
    )


def _resolved_identity(*, provider_user_id: str = "provider-user-sensitive") -> ProviderIdentity:
    return ProviderIdentity(
        provider="feishu",
        provider_tenant_key="tenant-key-sensitive",
        provider_account_id="account-sensitive",
        provider_user_id=provider_user_id,
        verified_at=NOW,
        provider_status=ProviderDirectoryStatus.ACTIVE,
    )


class _Repository:
    def __init__(
        self,
        *,
        lease: ReconciliationLease | None = None,
        applied: ReconciliationApplyResult | None = None,
    ) -> None:
        self.lease = lease
        self.applied = applied or ReconciliationApplyResult(
            outcome=ReconciliationApplyOutcome.APPLIED,
            identity_changed=False,
            account_revision=7,
        )
        self.events: list[str] = []
        self.observation: ReconciliationObservation | None = None
        self.delay_seconds: int | None = None

    async def seed_checkpoints(self, *, due_at: datetime) -> int:
        assert due_at == NOW
        self.events.append("seed")
        return 2

    async def claim_next(
        self,
        *,
        owner: str,
        lease_seconds: int,
        probe_interval_seconds: float,
        active_since: datetime,
        cycle_interval_seconds: int,
    ) -> ReconciliationLease | None:
        assert owner == "worker-safe"
        assert lease_seconds == 30
        assert probe_interval_seconds == 1.0
        assert active_since == NOW - timedelta(seconds=3_600)
        assert cycle_interval_seconds == 600
        self.events.append("claim")
        return self.lease

    async def apply(
        self,
        *,
        lease: ReconciliationLease,
        observation: ReconciliationObservation,
        probe_interval_seconds: float,
        unavailable_delay_seconds: int,
        not_found_confirmation_seconds: int,
        cycle_interval_seconds: int,
        degrade_after_failures: int,
        max_tighten_per_cycle: int,
    ) -> ReconciliationApplyResult:
        assert lease is self.lease
        assert probe_interval_seconds == 1.0
        assert not_found_confirmation_seconds == 600
        assert cycle_interval_seconds == 600
        assert degrade_after_failures == 3
        assert max_tighten_per_cycle == 10
        self.events.append("apply")
        self.observation = observation
        self.delay_seconds = unavailable_delay_seconds
        return self.applied


class _Provider:
    def __init__(
        self,
        result: ProviderIdentityResult,
        events: list[str],
    ) -> None:
        self.result = result
        self.events = events
        self.invalidated = False

    async def resolve(
        self,
        context: ProviderContext,
        assertion: ExternalIdentityAssertion,
    ) -> ProviderIdentityResult:
        del context, assertion
        raise AssertionError("foreground resolve must not be used")

    async def refresh(
        self,
        context: ProviderContext,
        provider_user_id: str,
    ) -> ProviderIdentityResult:
        del context, provider_user_id
        raise AssertionError("cached refresh must not be used")

    async def reconcile(
        self,
        context: ProviderContext,
        provider_user_id: str,
    ) -> ProviderIdentityResult:
        assert context == _context()
        assert provider_user_id == "provider-user-sensitive"
        self.events.append("provider")
        return self.result

    async def invalidate(self, context: ProviderContext) -> None:
        assert context == _context()
        self.events.append("invalidate")
        self.invalidated = True


class _Registry:
    def __init__(self, provider: EnterpriseIdentityProvider | None) -> None:
        self.provider = provider

    def get(self, provider: str) -> EnterpriseIdentityProvider | None:
        assert provider == "feishu"
        return self.provider


def _service(
    repository: _Repository,
    provider: _Provider | None,
    *,
    limits: IdentityReconciliationLimits | None = None,
    now: Callable[[], datetime] = lambda: NOW,
    telemetry: InMemoryReconciliationTelemetry | None = None,
) -> IdentityReconciliationService:
    registry = _Registry(cast(EnterpriseIdentityProvider | None, provider))
    return IdentityReconciliationService(
        repository,
        cast(ReconciliationProviderRegistry, registry),
        limits or _limits(),
        now=now,
        telemetry=telemetry or InMemoryReconciliationTelemetry(),
    )


async def test_seed_and_idle_are_bounded_and_do_not_probe_provider() -> None:
    repository = _Repository()
    telemetry = InMemoryReconciliationTelemetry()
    service = _service(repository, None, telemetry=telemetry)

    seeded = await service.seed_checkpoints()
    idle = await service.reconcile_one(owner="worker-safe")

    assert seeded.status is ReconciliationSeedStatus.SEEDED
    assert seeded.checkpoint_count == 2
    assert idle.status is ReconciliationRunStatus.IDLE
    assert repository.events == ["seed", "claim"]
    assert telemetry.snapshot().counters == {
        ReconciliationTelemetryEvent.CHECKPOINTS_SEEDED: 2,
        ReconciliationTelemetryEvent.CLAIM_IDLE: 1,
    }


async def test_resolved_probe_runs_between_short_repository_calls() -> None:
    repository = _Repository(
        lease=_lease(),
        applied=ReconciliationApplyResult(
            outcome=ReconciliationApplyOutcome.APPLIED,
            identity_changed=True,
            account_revision=8,
        ),
    )
    provider = _Provider(
        ProviderIdentityResult(
            status=ProviderIdentityStatus.RESOLVED,
            identity=_resolved_identity(),
        ),
        repository.events,
    )

    result = await _service(repository, provider).reconcile_one(owner="worker-safe")

    assert result.status is ReconciliationRunStatus.COMPLETED
    assert result.provider_status is ReconciliationProviderStatus.RESOLVED
    assert result.apply_outcome is ReconciliationApplyOutcome.APPLIED
    assert repository.events == ["claim", "provider", "apply", "invalidate"]
    assert repository.observation == ReconciliationObservation(
        status=ReconciliationProviderStatus.RESOLVED,
        observed_at=NOW,
        verified_at=NOW,
    )
    # Backoff is based on durable consecutive failures, not the fencing token.
    assert repository.delay_seconds == 20
    assert provider.invalidated is True


@pytest.mark.parametrize(
    ("status", "error", "expected"),
    [
        (
            ProviderIdentityStatus.INACTIVE,
            ProviderErrorCode.INACTIVE,
            ReconciliationProviderStatus.INACTIVE,
        ),
        (
            ProviderIdentityStatus.NOT_FOUND,
            ProviderErrorCode.NOT_FOUND,
            ReconciliationProviderStatus.NOT_FOUND,
        ),
        (
            ProviderIdentityStatus.NOT_IN_SCOPE,
            ProviderErrorCode.NOT_IN_SCOPE,
            ReconciliationProviderStatus.NOT_IN_SCOPE,
        ),
        (
            ProviderIdentityStatus.UNAVAILABLE,
            ProviderErrorCode.PROVIDER_UNAVAILABLE,
            ReconciliationProviderStatus.UNAVAILABLE,
        ),
        (
            ProviderIdentityStatus.CONFLICT,
            ProviderErrorCode.LINK_CONFLICT,
            ReconciliationProviderStatus.CONFLICT,
        ),
        (
            ProviderIdentityStatus.INVALID,
            ProviderErrorCode.ASSERTION_INVALID,
            ReconciliationProviderStatus.INVALID,
        ),
    ],
)
async def test_provider_status_vocabulary_maps_without_raw_payload(
    status: ProviderIdentityStatus,
    error: ProviderErrorCode,
    expected: ReconciliationProviderStatus,
) -> None:
    repository = _Repository(lease=_lease())
    provider = _Provider(
        ProviderIdentityResult(
            status=status,
            error_code=error,
            retryable=status is ProviderIdentityStatus.UNAVAILABLE,
        ),
        repository.events,
    )

    result = await _service(repository, provider).reconcile_one(owner="worker-safe")

    assert result.provider_status is expected
    assert repository.observation is not None
    assert repository.observation.status is expected


@pytest.mark.parametrize(
    "result",
    [
        ProviderIdentityResult(
            status=ProviderIdentityStatus.RESOLVED,
            identity=_resolved_identity(provider_user_id="different-user-sensitive"),
        ),
        ProviderIdentityResult(
            status=ProviderIdentityStatus.RESOLVED,
            identity=replace(
                _resolved_identity(),
                provider="different-provider",
            ),
        ),
        ProviderIdentityResult(
            status=ProviderIdentityStatus.RESOLVED,
            identity=replace(
                _resolved_identity(),
                provider_tenant_key="different-tenant-sensitive",
            ),
        ),
        ProviderIdentityResult(
            status=ProviderIdentityStatus.RESOLVED,
            identity=replace(
                _resolved_identity(),
                provider_account_id="different-account-sensitive",
            ),
        ),
        ProviderIdentityResult(
            status=ProviderIdentityStatus.RESOLVED,
            identity=replace(
                _resolved_identity(),
                verified_at=NOW + timedelta(microseconds=1),
            ),
        ),
        ProviderIdentityResult(
            status=ProviderIdentityStatus.RESOLVED,
            identity=_resolved_identity(),
            from_cache=True,
        ),
        ProviderIdentityResult(
            status=ProviderIdentityStatus.NOT_FOUND,
            error_code=ProviderErrorCode.INACTIVE,
        ),
        ProviderIdentityResult(
            status=ProviderIdentityStatus.NOT_FOUND,
            identity=_resolved_identity(),
            error_code=ProviderErrorCode.NOT_FOUND,
        ),
    ],
)
async def test_malformed_or_stale_provider_result_fails_closed(
    result: ProviderIdentityResult,
) -> None:
    repository = _Repository(lease=_lease())
    provider = _Provider(result, repository.events)

    reconciled = await _service(repository, provider).reconcile_one(
        owner="worker-safe",
    )

    assert reconciled.provider_status is ReconciliationProviderStatus.INVALID
    assert repository.observation is not None
    assert repository.observation.safe_error_code == (ReconciliationErrorCode.PROVIDER_RESULT_INVALID.value)


async def test_missing_reconciliation_capability_fails_closed() -> None:
    repository = _Repository(lease=_lease())
    service = _service(repository, None)

    result = await service.reconcile_one(owner="worker-safe")

    assert result.provider_status is ReconciliationProviderStatus.INVALID
    assert repository.events == ["claim", "apply"]


async def test_provider_exception_becomes_retryable_unavailable_observation() -> None:
    repository = _Repository(lease=_lease(failures=20))
    provider = _Provider(
        ProviderIdentityResult(status=ProviderIdentityStatus.UNAVAILABLE),
        repository.events,
    )

    async def fail(
        context: ProviderContext,
        provider_user_id: str,
    ) -> ProviderIdentityResult:
        del context, provider_user_id
        raise RuntimeError("raw-provider-payload-sensitive")

    provider.reconcile = fail  # type: ignore[method-assign]
    result = await _service(repository, provider).reconcile_one(owner="worker-safe")

    assert result.provider_status is ReconciliationProviderStatus.UNAVAILABLE
    assert repository.observation is not None
    assert repository.observation.safe_error_code == ProviderErrorCode.PROVIDER_UNAVAILABLE.value
    assert repository.delay_seconds == 30
    assert "raw-provider-payload-sensitive" not in repr(result)


async def test_expired_lease_skips_provider_and_attempts_unavailable_backoff() -> None:
    repository = _Repository(
        lease=_lease(lease_until=NOW - timedelta(microseconds=1)),
    )
    provider = _Provider(
        ProviderIdentityResult(
            status=ProviderIdentityStatus.RESOLVED,
            identity=_resolved_identity(),
        ),
        repository.events,
    )

    result = await _service(repository, provider).reconcile_one(
        owner="worker-safe",
    )

    assert result.provider_status is ReconciliationProviderStatus.UNAVAILABLE
    assert repository.events == ["claim", "apply"]
    assert repository.observation == ReconciliationObservation(
        status=ReconciliationProviderStatus.UNAVAILABLE,
        observed_at=NOW,
        safe_error_code=ProviderErrorCode.PROVIDER_UNAVAILABLE.value,
    )
    assert repository.delay_seconds == 20


async def test_cold_provider_path_times_out_before_lease_safety_margin() -> None:
    repository = _Repository(
        lease=_lease(lease_until=NOW + timedelta(seconds=5.02)),
    )
    provider = _Provider(
        ProviderIdentityResult(status=ProviderIdentityStatus.UNAVAILABLE),
        repository.events,
    )
    provider_cancelled = asyncio.Event()

    async def block_in_credential_path(
        context: ProviderContext,
        provider_user_id: str,
    ) -> ProviderIdentityResult:
        del context, provider_user_id
        repository.events.append("provider")
        try:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")
        finally:
            provider_cancelled.set()

    provider.reconcile = block_in_credential_path  # type: ignore[method-assign]

    result = await _service(repository, provider).reconcile_one(
        owner="worker-safe",
    )

    assert provider_cancelled.is_set()
    assert result.provider_status is ReconciliationProviderStatus.UNAVAILABLE
    assert repository.events == ["claim", "provider", "apply"]
    assert repository.observation is not None
    assert repository.observation.status is ReconciliationProviderStatus.UNAVAILABLE
    assert repository.delay_seconds == 20


async def test_far_future_deadline_is_capped_by_nominal_lease_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _Repository(
        lease=_lease(lease_until=NOW + timedelta(hours=1)),
    )
    provider = _Provider(
        ProviderIdentityResult(
            status=ProviderIdentityStatus.RESOLVED,
            identity=_resolved_identity(),
        ),
        repository.events,
    )
    captured_budgets: list[float] = []

    @asynccontextmanager
    async def capture_timeout(delay: float | None) -> AsyncIterator[None]:
        assert delay is not None
        captured_budgets.append(delay)
        yield

    monkeypatch.setattr(
        reconciliation_service_module,
        "async_timeout",
        capture_timeout,
    )

    result = await _service(repository, provider).reconcile_one(
        owner="worker-safe",
    )

    assert result.provider_status is ReconciliationProviderStatus.RESOLVED
    assert captured_budgets == [25.0]


async def test_provider_cancellation_propagates_without_applying_lease() -> None:
    repository = _Repository(lease=_lease())
    provider = _Provider(
        ProviderIdentityResult(status=ProviderIdentityStatus.UNAVAILABLE),
        repository.events,
    )

    async def cancel(
        context: ProviderContext,
        provider_user_id: str,
    ) -> ProviderIdentityResult:
        del context, provider_user_id
        raise asyncio.CancelledError

    provider.reconcile = cancel  # type: ignore[method-assign]
    with pytest.raises(asyncio.CancelledError):
        await _service(repository, provider).reconcile_one(owner="worker-safe")

    assert repository.events == ["claim"]


async def test_repository_failure_is_closed_and_does_not_escape_details() -> None:
    class _FailingRepository(_Repository):
        async def claim_next(
            self,
            *,
            owner: str,
            lease_seconds: int,
            probe_interval_seconds: float,
            active_since: datetime,
            cycle_interval_seconds: int,
        ) -> ReconciliationLease | None:
            del owner, lease_seconds, probe_interval_seconds, active_since, cycle_interval_seconds
            raise ReconciliationRepositoryError(
                ReconciliationErrorCode.REPOSITORY_UNAVAILABLE,
            )

    result = await _service(_FailingRepository(), None).reconcile_one(
        owner="worker-safe",
    )

    assert result.status is ReconciliationRunStatus.REPOSITORY_UNAVAILABLE
    assert result.safe_error_code == ReconciliationErrorCode.REPOSITORY_UNAVAILABLE.value
    assert "tenant-sensitive" not in repr(result)


def test_telemetry_rejects_open_labels_and_never_stores_identity_values() -> None:
    telemetry = InMemoryReconciliationTelemetry()
    telemetry.increment(ReconciliationTelemetryEvent.CLAIM_IDLE)

    with pytest.raises((ValueError, BeartypeCallHintParamViolation)):
        telemetry.increment(cast(ReconciliationTelemetryEvent, "tenant-sensitive"))

    snapshot = telemetry.snapshot()
    assert snapshot.counters == {ReconciliationTelemetryEvent.CLAIM_IDLE: 1}
    assert "tenant-sensitive" not in repr(snapshot)


def test_limits_reject_invalid_backoff_and_confirmation_windows() -> None:
    with pytest.raises(ValueError, match="backoff"):
        replace(_limits(), backoff_initial_seconds=31)
    with pytest.raises(ValueError, match="confirmation"):
        replace(_limits(), not_found_confirmation_seconds=599)
    with pytest.raises(ValueError, match="probe safety margin"):
        replace(_limits(), probe_safety_margin_seconds=30.0)
    for interval in (False, 0.09, 60.01, float("nan"), float("inf")):
        with pytest.raises(
            (ValueError, BeartypeCallHintParamViolation),
            match=r"probe interval|probe_interval_seconds",
        ):
            replace(_limits(), probe_interval_seconds=interval)
