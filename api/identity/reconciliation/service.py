"""Framework-neutral orchestration for durable identity reconciliation.

Repository calls own short transactions.  In particular, the provider probe
is performed only after ``claim_next`` has returned and before ``apply`` opens
its fenced transaction; no network request can hold a database transaction.
"""

from __future__ import annotations

import asyncio
import math
import re
from asyncio import timeout as async_timeout
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol, runtime_checkable

from api.identity.contracts import ProviderContext
from api.identity.providers.contracts import (
    EnterpriseIdentityProvider,
    ProviderDirectoryStatus,
    ProviderErrorCode,
    ProviderIdentity,
    ProviderIdentityResult,
    ProviderIdentityStatus,
    ReconciliationIdentityProvider,
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
from api.identity.reconciliation.telemetry import (
    NOOP_RECONCILIATION_TELEMETRY,
    ReconciliationTelemetry,
    ReconciliationTelemetryEvent,
)


@runtime_checkable
class IdentityReconciliationRepository(Protocol):
    async def seed_checkpoints(self, *, due_at: datetime) -> int: ...

    async def claim_next(
        self,
        *,
        owner: str,
        lease_seconds: int,
        probe_interval_seconds: float,
        active_since: datetime,
        cycle_interval_seconds: int,
    ) -> ReconciliationLease | None: ...

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
    ) -> ReconciliationApplyResult: ...


@runtime_checkable
class ReconciliationProviderRegistry(Protocol):
    """Injected view of the shared request-path provider registry."""

    def get(self, provider: str) -> EnterpriseIdentityProvider | None: ...


@runtime_checkable
class InvalidatableIdentityProvider(Protocol):
    async def invalidate(self, context: ProviderContext) -> None: ...


@dataclass(frozen=True, slots=True)
class IdentityReconciliationLimits:
    lease_seconds: int
    probe_safety_margin_seconds: float
    probe_interval_seconds: float
    cycle_interval_seconds: int
    active_window_seconds: int
    backoff_initial_seconds: int
    backoff_max_seconds: int
    not_found_confirmation_seconds: int
    degrade_after_failures: int
    max_tighten_per_cycle: int

    def __post_init__(self) -> None:
        values = (
            self.lease_seconds,
            self.cycle_interval_seconds,
            self.active_window_seconds,
            self.backoff_initial_seconds,
            self.backoff_max_seconds,
            self.not_found_confirmation_seconds,
            self.degrade_after_failures,
            self.max_tighten_per_cycle,
        )
        if any(type(value) is not int or value <= 0 for value in values):
            raise ValueError("identity reconciliation limits must be positive integers")
        if (
            type(self.probe_safety_margin_seconds) not in {float, int}
            or not math.isfinite(self.probe_safety_margin_seconds)
            or self.probe_safety_margin_seconds <= 0
            or self.probe_safety_margin_seconds >= self.lease_seconds
        ):
            raise ValueError(
                "identity reconciliation probe safety margin must be positive and shorter than its lease",
            )
        if type(self.probe_interval_seconds) not in {float, int} or not math.isfinite(self.probe_interval_seconds) or not 0.1 <= self.probe_interval_seconds <= 60.0:
            raise ValueError(
                "identity reconciliation probe interval must be between 0.1 and 60 seconds",
            )
        if self.backoff_initial_seconds > self.backoff_max_seconds:
            raise ValueError("identity reconciliation backoff limits are invalid")
        if self.not_found_confirmation_seconds < self.cycle_interval_seconds:
            raise ValueError("identity reconciliation confirmation window is invalid")


class ReconciliationSeedStatus(StrEnum):
    SEEDED = "seeded"
    REPOSITORY_UNAVAILABLE = "repository_unavailable"


@dataclass(frozen=True, slots=True)
class ReconciliationSeedResult:
    status: ReconciliationSeedStatus
    checkpoint_count: int = 0
    safe_error_code: str | None = field(default=None, repr=False)


class ReconciliationRunStatus(StrEnum):
    IDLE = "idle"
    COMPLETED = "completed"
    REPOSITORY_UNAVAILABLE = "repository_unavailable"


@dataclass(frozen=True, slots=True)
class ReconciliationRunResult:
    status: ReconciliationRunStatus
    provider_status: ReconciliationProviderStatus | None = None
    apply_outcome: ReconciliationApplyOutcome | None = None
    safe_error_code: str | None = field(default=None, repr=False)


class IdentityReconciliationService:
    """Claim and reconcile one recently-active, already-linked identity."""

    def __init__(
        self,
        repository: IdentityReconciliationRepository,
        provider_registry: ReconciliationProviderRegistry,
        limits: IdentityReconciliationLimits,
        *,
        now: Callable[[], datetime] | None = None,
        telemetry: ReconciliationTelemetry = NOOP_RECONCILIATION_TELEMETRY,
    ) -> None:
        self._repository = repository
        self._provider_registry = provider_registry
        self._limits = limits
        self._now = now or (lambda: datetime.now(tz=UTC))
        self._telemetry = telemetry

    async def seed_checkpoints(self) -> ReconciliationSeedResult:
        try:
            checkpoint_count = await self._repository.seed_checkpoints(
                due_at=self._aware_now(),
            )
        except ReconciliationRepositoryError as exc:
            self._telemetry.increment(
                ReconciliationTelemetryEvent.REPOSITORY_UNAVAILABLE,
            )
            return ReconciliationSeedResult(
                status=ReconciliationSeedStatus.REPOSITORY_UNAVAILABLE,
                safe_error_code=exc.code.value,
            )
        if type(checkpoint_count) is not int or checkpoint_count < 0:
            self._telemetry.increment(
                ReconciliationTelemetryEvent.REPOSITORY_UNAVAILABLE,
            )
            return ReconciliationSeedResult(
                status=ReconciliationSeedStatus.REPOSITORY_UNAVAILABLE,
                safe_error_code=(ReconciliationErrorCode.REPOSITORY_UNAVAILABLE.value),
            )
        self._telemetry.increment(
            ReconciliationTelemetryEvent.CHECKPOINTS_SEEDED,
            count=checkpoint_count,
        )
        return ReconciliationSeedResult(
            status=ReconciliationSeedStatus.SEEDED,
            checkpoint_count=checkpoint_count,
        )

    async def reconcile_one(self, *, owner: str) -> ReconciliationRunResult:
        """Reconcile one leased target, leaving retries to durable state."""

        if type(owner) is not str or not owner.strip() or len(owner) > 64:
            raise ValueError("identity reconciliation owner is invalid")
        claimed_at = self._aware_now()
        try:
            lease = await self._repository.claim_next(
                owner=owner,
                lease_seconds=self._limits.lease_seconds,
                probe_interval_seconds=self._limits.probe_interval_seconds,
                active_since=claimed_at - timedelta(seconds=self._limits.active_window_seconds),
                cycle_interval_seconds=self._limits.cycle_interval_seconds,
            )
        except ReconciliationRepositoryError as exc:
            self._telemetry.increment(
                ReconciliationTelemetryEvent.REPOSITORY_UNAVAILABLE,
            )
            return ReconciliationRunResult(
                status=ReconciliationRunStatus.REPOSITORY_UNAVAILABLE,
                safe_error_code=exc.code.value,
            )
        if lease is None:
            self._telemetry.increment(ReconciliationTelemetryEvent.CLAIM_IDLE)
            return ReconciliationRunResult(status=ReconciliationRunStatus.IDLE)
        if not isinstance(lease, ReconciliationLease):
            self._telemetry.increment(
                ReconciliationTelemetryEvent.REPOSITORY_UNAVAILABLE,
            )
            return ReconciliationRunResult(
                status=ReconciliationRunStatus.REPOSITORY_UNAVAILABLE,
                safe_error_code=(ReconciliationErrorCode.REPOSITORY_UNAVAILABLE.value),
            )

        provider = self._provider(lease)
        observation = await self._observe(provider, lease)
        self._telemetry.increment(_OBSERVATION_EVENTS[observation.status])

        try:
            applied = await self._repository.apply(
                lease=lease,
                observation=observation,
                probe_interval_seconds=self._limits.probe_interval_seconds,
                unavailable_delay_seconds=self._backoff_seconds(
                    lease.consecutive_failures,
                ),
                not_found_confirmation_seconds=(self._limits.not_found_confirmation_seconds),
                cycle_interval_seconds=self._limits.cycle_interval_seconds,
                degrade_after_failures=self._limits.degrade_after_failures,
                max_tighten_per_cycle=self._limits.max_tighten_per_cycle,
            )
        except ReconciliationRepositoryError as exc:
            self._telemetry.increment(
                ReconciliationTelemetryEvent.REPOSITORY_UNAVAILABLE,
            )
            return ReconciliationRunResult(
                status=ReconciliationRunStatus.REPOSITORY_UNAVAILABLE,
                provider_status=observation.status,
                safe_error_code=exc.code.value,
            )
        if not _valid_apply_result(applied):
            self._telemetry.increment(
                ReconciliationTelemetryEvent.REPOSITORY_UNAVAILABLE,
            )
            return ReconciliationRunResult(
                status=ReconciliationRunStatus.REPOSITORY_UNAVAILABLE,
                provider_status=observation.status,
                safe_error_code=(ReconciliationErrorCode.REPOSITORY_UNAVAILABLE.value),
            )
        self._telemetry.increment(_APPLY_EVENTS[applied.outcome])
        await self._invalidate_after_change(provider, lease, applied)
        return ReconciliationRunResult(
            status=ReconciliationRunStatus.COMPLETED,
            provider_status=observation.status,
            apply_outcome=applied.outcome,
            safe_error_code=applied.safe_error_code,
        )

    def _provider(
        self,
        lease: ReconciliationLease,
    ) -> ReconciliationIdentityProvider | None:
        try:
            provider = self._provider_registry.get(lease.context.provider)
        except Exception:
            return None
        if not isinstance(provider, ReconciliationIdentityProvider):
            return None
        return provider

    async def _observe(
        self,
        provider: ReconciliationIdentityProvider | None,
        lease: ReconciliationLease,
    ) -> ReconciliationObservation:
        if provider is None:
            return _invalid_observation(self._aware_now())
        probe_started_at = self._aware_now()
        lease_remaining_seconds = min(
            (lease.lease_until - probe_started_at).total_seconds(),
            float(self._limits.lease_seconds),
        )
        probe_budget_seconds = lease_remaining_seconds - self._limits.probe_safety_margin_seconds
        if not math.isfinite(probe_budget_seconds) or probe_budget_seconds <= 0:
            return _unavailable_observation(probe_started_at)
        try:
            async with async_timeout(probe_budget_seconds):
                # This outer boundary includes credential/KMS resolution,
                # tokens, tenant/contact calls, and provider-side limiters.
                result = await provider.reconcile(
                    lease.context,
                    lease.subject_value,
                )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            return _unavailable_observation(self._aware_now())
        except Exception:
            return _unavailable_observation(self._aware_now())
        observed_at = self._aware_now()
        return _validate_provider_result(result, lease, observed_at)

    def _backoff_seconds(self, consecutive_failures: int) -> int:
        shift = min(max(consecutive_failures, 0), 62)
        delay = self._limits.backoff_initial_seconds * (1 << shift)
        return min(delay, self._limits.backoff_max_seconds)

    async def _invalidate_after_change(
        self,
        provider: ReconciliationIdentityProvider | None,
        lease: ReconciliationLease,
        applied: ReconciliationApplyResult,
    ) -> None:
        if provider is None or not (applied.identity_changed or applied.account_revision != lease.context.provider_account_revision):
            return
        if not isinstance(provider, InvalidatableIdentityProvider):
            return
        try:
            await provider.invalidate(lease.context)
        except asyncio.CancelledError:
            raise
        except Exception:
            # The durable DB fence is authoritative.  Cache invalidation is
            # best effort and receives no identity-shaped telemetry or logs.
            return

    def _aware_now(self) -> datetime:
        value = self._now()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("identity reconciliation clock must be timezone-aware")
        return value


def _validate_provider_result(
    result: object,
    lease: ReconciliationLease,
    observed_at: datetime,
) -> ReconciliationObservation:
    if not isinstance(result, ProviderIdentityResult):
        return _invalid_observation(observed_at)
    if not isinstance(result.status, ProviderIdentityStatus):
        return _invalid_observation(observed_at)
    if type(result.from_cache) is not bool or result.from_cache:
        return _invalid_observation(observed_at)
    if type(result.retryable) is not bool:
        return _invalid_observation(observed_at)
    if result.error_code is not None and not isinstance(
        result.error_code,
        ProviderErrorCode,
    ):
        return _invalid_observation(observed_at)

    if result.status is ProviderIdentityStatus.RESOLVED:
        if (
            result.error_code is not None
            or result.retryable
            or not _valid_resolved_identity(
                result.identity,
                lease,
                observed_at,
            )
        ):
            return _invalid_observation(observed_at)
        assert result.identity is not None
        return ReconciliationObservation(
            status=ReconciliationProviderStatus.RESOLVED,
            observed_at=observed_at,
            verified_at=result.identity.verified_at,
        )

    if result.identity is not None:
        return _invalid_observation(observed_at)
    expected_errors = _EXPECTED_PROVIDER_ERRORS.get(result.status)
    if expected_errors is None or result.error_code not in expected_errors:
        return _invalid_observation(observed_at)
    if result.status is not ProviderIdentityStatus.UNAVAILABLE and result.retryable:
        return _invalid_observation(observed_at)

    status = _PROVIDER_STATUS_MAP[result.status]
    safe_error_code = None
    if status in {
        ReconciliationProviderStatus.UNAVAILABLE,
        ReconciliationProviderStatus.INVALID,
    }:
        assert result.error_code is not None
        safe_error_code = result.error_code.value
    return ReconciliationObservation(
        status=status,
        observed_at=observed_at,
        safe_error_code=safe_error_code,
    )


def _valid_resolved_identity(
    identity: ProviderIdentity | None,
    lease: ReconciliationLease,
    observed_at: datetime,
) -> bool:
    if not isinstance(identity, ProviderIdentity):
        return False
    verified_at = identity.verified_at
    return (
        identity.provider == lease.context.provider
        and identity.provider_tenant_key == lease.context.provider_tenant_key
        and identity.provider_account_id == lease.context.provider_account_id
        and identity.provider_user_id == lease.subject_value
        and identity.provider_status is ProviderDirectoryStatus.ACTIVE
        and verified_at.tzinfo is not None
        and verified_at.utcoffset() is not None
        and verified_at <= observed_at
    )


def _valid_apply_result(result: object) -> bool:
    if not isinstance(result, ReconciliationApplyResult):
        return False
    if not isinstance(result.outcome, ReconciliationApplyOutcome) or type(result.identity_changed) is not bool or type(result.account_revision) is not int or result.account_revision < 1:
        return False
    if result.next_attempt_at is not None and (not isinstance(result.next_attempt_at, datetime) or result.next_attempt_at.tzinfo is None or result.next_attempt_at.utcoffset() is None):
        return False
    return result.safe_error_code is None or (type(result.safe_error_code) is str and _SAFE_ERROR_CODE_RE.fullmatch(result.safe_error_code) is not None)


def _invalid_observation(observed_at: datetime) -> ReconciliationObservation:
    return ReconciliationObservation(
        status=ReconciliationProviderStatus.INVALID,
        observed_at=observed_at,
        safe_error_code=ReconciliationErrorCode.PROVIDER_RESULT_INVALID.value,
    )


def _unavailable_observation(observed_at: datetime) -> ReconciliationObservation:
    return ReconciliationObservation(
        status=ReconciliationProviderStatus.UNAVAILABLE,
        observed_at=observed_at,
        safe_error_code=ProviderErrorCode.PROVIDER_UNAVAILABLE.value,
    )


_PROVIDER_STATUS_MAP = {
    ProviderIdentityStatus.INACTIVE: ReconciliationProviderStatus.INACTIVE,
    ProviderIdentityStatus.NOT_FOUND: ReconciliationProviderStatus.NOT_FOUND,
    ProviderIdentityStatus.NOT_IN_SCOPE: ReconciliationProviderStatus.NOT_IN_SCOPE,
    ProviderIdentityStatus.UNAVAILABLE: ReconciliationProviderStatus.UNAVAILABLE,
    ProviderIdentityStatus.CONFLICT: ReconciliationProviderStatus.CONFLICT,
    ProviderIdentityStatus.INVALID: ReconciliationProviderStatus.INVALID,
}

_SAFE_ERROR_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")

_EXPECTED_PROVIDER_ERRORS = {
    ProviderIdentityStatus.INACTIVE: frozenset({ProviderErrorCode.INACTIVE}),
    ProviderIdentityStatus.NOT_FOUND: frozenset({ProviderErrorCode.NOT_FOUND}),
    ProviderIdentityStatus.NOT_IN_SCOPE: frozenset({ProviderErrorCode.NOT_IN_SCOPE}),
    ProviderIdentityStatus.UNAVAILABLE: frozenset(
        {
            ProviderErrorCode.PROVIDER_UNAVAILABLE,
            ProviderErrorCode.CREDENTIAL_UNAVAILABLE,
        },
    ),
    ProviderIdentityStatus.CONFLICT: frozenset({ProviderErrorCode.LINK_CONFLICT}),
    ProviderIdentityStatus.INVALID: frozenset(
        {
            ProviderErrorCode.ASSERTION_INVALID,
            ProviderErrorCode.PROVIDER_MISMATCH,
            ProviderErrorCode.TENANT_MISMATCH,
        },
    ),
}

_OBSERVATION_EVENTS = {
    ReconciliationProviderStatus.RESOLVED: ReconciliationTelemetryEvent.OBSERVED_RESOLVED,
    ReconciliationProviderStatus.INACTIVE: ReconciliationTelemetryEvent.OBSERVED_INACTIVE,
    ReconciliationProviderStatus.NOT_FOUND: ReconciliationTelemetryEvent.OBSERVED_NOT_FOUND,
    ReconciliationProviderStatus.NOT_IN_SCOPE: ReconciliationTelemetryEvent.OBSERVED_NOT_IN_SCOPE,
    ReconciliationProviderStatus.UNAVAILABLE: ReconciliationTelemetryEvent.OBSERVED_UNAVAILABLE,
    ReconciliationProviderStatus.CONFLICT: ReconciliationTelemetryEvent.OBSERVED_CONFLICT,
    ReconciliationProviderStatus.INVALID: ReconciliationTelemetryEvent.OBSERVED_INVALID,
}

_APPLY_EVENTS = {
    ReconciliationApplyOutcome.APPLIED: ReconciliationTelemetryEvent.APPLY_APPLIED,
    ReconciliationApplyOutcome.CONFIRMATION_PENDING: ReconciliationTelemetryEvent.APPLY_CONFIRMATION_PENDING,
    ReconciliationApplyOutcome.RETRY_SCHEDULED: ReconciliationTelemetryEvent.APPLY_RETRY_SCHEDULED,
    ReconciliationApplyOutcome.TARGET_REJECTED: ReconciliationTelemetryEvent.APPLY_TARGET_REJECTED,
    ReconciliationApplyOutcome.CIRCUIT_OPEN: ReconciliationTelemetryEvent.APPLY_CIRCUIT_OPEN,
    ReconciliationApplyOutcome.FENCE_REJECTED: ReconciliationTelemetryEvent.APPLY_FENCE_REJECTED,
}


__all__ = [
    "IdentityReconciliationLimits",
    "IdentityReconciliationRepository",
    "IdentityReconciliationService",
    "ReconciliationProviderRegistry",
    "ReconciliationRunResult",
    "ReconciliationRunStatus",
    "ReconciliationSeedResult",
    "ReconciliationSeedStatus",
]
