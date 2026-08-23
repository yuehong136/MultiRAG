"""Framework-neutral EIM-I8 reconciliation contracts.

The DTOs intentionally omit provider natural keys and hide every identifier
from ``repr`` so a worker exception cannot turn a directory lookup into a log
of tenant or employee identifiers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from api.identity.contracts import ProviderContext

_SAFE_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
_ACCOUNT_REF_RE = re.compile(r"^[0-9a-f]{32}$")


class ReconciliationProviderStatus(StrEnum):
    """Bounded result vocabulary accepted from a provider adapter."""

    RESOLVED = "resolved"
    INACTIVE = "inactive"
    NOT_FOUND = "not_found"
    NOT_IN_SCOPE = "not_in_scope"
    UNAVAILABLE = "unavailable"
    CONFLICT = "conflict"
    INVALID = "invalid"


class ReconciliationErrorCode(StrEnum):
    """Low-cardinality, non-sensitive persistence and worker error codes."""

    REPOSITORY_UNAVAILABLE = "IDENTITY_RECONCILIATION_REPOSITORY_UNAVAILABLE"
    LEASE_CONFLICT = "IDENTITY_RECONCILIATION_LEASE_CONFLICT"
    FENCE_CONFLICT = "IDENTITY_RECONCILIATION_FENCE_CONFLICT"
    PROVIDER_UNAVAILABLE = "IDENTITY_RECONCILIATION_PROVIDER_UNAVAILABLE"
    PROVIDER_RESULT_INVALID = "IDENTITY_RECONCILIATION_PROVIDER_RESULT_INVALID"
    PROVIDER_CONFLICT = "IDENTITY_RECONCILIATION_PROVIDER_CONFLICT"
    TIGHTEN_CIRCUIT_OPEN = "IDENTITY_RECONCILIATION_TIGHTEN_CIRCUIT_OPEN"


class ReconciliationApplyOutcome(StrEnum):
    APPLIED = "applied"
    CONFIRMATION_PENDING = "confirmation_pending"
    RETRY_SCHEDULED = "retry_scheduled"
    TARGET_REJECTED = "target_rejected"
    CIRCUIT_OPEN = "circuit_open"
    FENCE_REJECTED = "fence_rejected"


class ReconciliationRepositoryError(RuntimeError):
    """Fail-closed repository failure without database or identity details."""

    def __init__(self, code: ReconciliationErrorCode) -> None:
        self.code = code
        super().__init__("identity reconciliation repository operation rejected")


@dataclass(frozen=True, slots=True)
class ReconciliationObservation:
    """One bounded provider observation; raw response data is never accepted."""

    status: ReconciliationProviderStatus
    observed_at: datetime
    verified_at: datetime | None = None
    safe_error_code: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        _require_aware(self.observed_at, "observed_at")
        if self.status is ReconciliationProviderStatus.RESOLVED:
            if self.verified_at is None:
                raise ValueError("resolved reconciliation requires verified_at")
            _require_aware(self.verified_at, "verified_at")
            if self.verified_at > self.observed_at:
                raise ValueError("verified_at cannot be later than observed_at")
        elif self.verified_at is not None:
            raise ValueError("only resolved reconciliation may carry verified_at")
        if self.status in {
            ReconciliationProviderStatus.UNAVAILABLE,
            ReconciliationProviderStatus.INVALID,
        }:
            if self.safe_error_code is None or _SAFE_CODE_RE.fullmatch(self.safe_error_code) is None:
                raise ValueError("provider failure requires a safe error code")
        elif self.safe_error_code is not None:
            raise ValueError("successful provider observation cannot carry an error code")


@dataclass(frozen=True, slots=True)
class ReconciliationLease:
    """Fenced work item returned after a short claim transaction."""

    checkpoint_id: str = field(repr=False)
    target_id: str = field(repr=False)
    owner: str = field(repr=False)
    attempt: int
    context: ProviderContext = field(repr=False)
    external_identity_id: str = field(repr=False)
    subject_value: str = field(repr=False)
    identity_revision: int
    consecutive_failures: int
    lease_until: datetime

    def __post_init__(self) -> None:
        if self.attempt < 1 or self.identity_revision < 1 or self.consecutive_failures < 0:
            raise ValueError("reconciliation lease revisions are invalid")
        if not all(
            isinstance(value, str) and value and value == value.strip()
            for value in (
                self.checkpoint_id,
                self.target_id,
                self.owner,
                self.external_identity_id,
                self.subject_value,
            )
        ):
            raise ValueError("reconciliation lease identifiers are invalid")
        _require_aware(self.lease_until, "lease_until")


@dataclass(frozen=True, slots=True)
class ReconciliationApplyResult:
    outcome: ReconciliationApplyOutcome
    identity_changed: bool
    account_revision: int
    next_attempt_at: datetime | None = None
    safe_error_code: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class ReconciliationAdminSnapshot:
    """Tenant-authorized projection with no provider-account natural key."""

    account_ref: str = field(repr=False)
    provider: str
    health_state: str
    cycle_active: bool
    lease_active: bool
    last_completed_at: datetime | None
    last_success_at: datetime | None
    next_run_at: datetime
    processed_count: int
    tightened_count: int
    error_count: int
    consecutive_failures: int
    safe_error_code: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if type(self.account_ref) is not str or _ACCOUNT_REF_RE.fullmatch(self.account_ref) is None:
            raise ValueError("reconciliation account reference is invalid")


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


__all__ = [
    "ReconciliationAdminSnapshot",
    "ReconciliationApplyOutcome",
    "ReconciliationApplyResult",
    "ReconciliationErrorCode",
    "ReconciliationLease",
    "ReconciliationObservation",
    "ReconciliationProviderStatus",
    "ReconciliationRepositoryError",
]
