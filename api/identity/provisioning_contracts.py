"""Framework-neutral contracts for transactional enterprise provisioning."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable

from api.identity.contracts import (
    AliasKey,
    ExternalIdentityRecord,
    IdentityErrorCode,
    IdentityResolutionRequest,
    IdentityResolutionResult,
    ProviderAliasType,
    ProviderContext,
    ProvisioningAction,
    ProvisioningMode,
    ProvisioningPolicySnapshot,
    UserMembershipRecord,
)
from api.identity.principal import Principal
from api.identity.providers.contracts import ProviderIdentityResult


class ProvisioningStatus(StrEnum):
    RESOLVED = "resolved"
    REJECTED = "rejected"


class ProvisioningOutcome(StrEnum):
    PREPROVISIONED_BOUND = "preprovisioned_bound"
    LINK_CODE_BOUND = "link_code_bound"
    JIT_CREATED = "jit_created"
    ALREADY_BOUND = "already_bound"


class LinkCodeIssueStatus(StrEnum):
    ISSUED = "issued"
    REJECTED = "rejected"


class ProvisioningPolicyWriteOutcome(StrEnum):
    CREATED = "created"
    APPLIED = "applied"
    EXISTS = "exists"
    NOT_FOUND = "not_found"
    REVISION_CONFLICT = "revision_conflict"


@dataclass(frozen=True, slots=True)
class ProvisionIdentityRequest:
    """I3 plan plus I4 proof; target ownership is never caller-selected."""

    resolution_request: IdentityResolutionRequest = field(repr=False)
    resolution: IdentityResolutionResult
    provider_result: ProviderIdentityResult = field(repr=False)
    link_code: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class ReverifyResolvedIdentityRequest:
    """Fresh I4 proof for one strict I3 active-identity snapshot."""

    resolution_request: IdentityResolutionRequest = field(repr=False)
    resolution: IdentityResolutionResult
    provider_result: ProviderIdentityResult = field(repr=False)


@dataclass(frozen=True, slots=True)
class LinkCodeIssueRequest:
    """Issue a grant for the authenticated Principal in this exact account."""

    context: ProviderContext = field(repr=False)
    principal: Principal = field(repr=False)


@dataclass(frozen=True, slots=True)
class VerifiedProvisioningAlias:
    alias_type: ProviderAliasType
    alias_value: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class VerifiedProvisioningCommand:
    """Sanitized write command derived only from an I3 plan and I4 proof."""

    context: ProviderContext = field(repr=False)
    asserted_alias: AliasKey = field(repr=False)
    action: ProvisioningAction | None
    policy_revision: int | None
    subject_value: str = field(repr=False)
    aliases: tuple[VerifiedProvisioningAlias, ...] = field(repr=False)
    verified_at: datetime
    jit_nickname: str = field(repr=False)
    request_digest_key_id: str = field(repr=False)
    request_digest: str = field(repr=False)
    link_code_key_id: str | None = field(default=None, repr=False)
    link_code_digest: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class LinkCodeIssueCommand:
    """Digest-only grant persistence command; raw code never reaches storage."""

    context: ProviderContext = field(repr=False)
    target_user_id: str = field(repr=False)
    digest_key_id: str = field(repr=False)
    code_digest: str = field(repr=False)
    policy_revision: int
    provider_account_revision: int
    provider_account_last_scope_change_at: datetime | None
    issued_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class LinkCodeGrantRecord:
    id: str = field(repr=False)
    issued_at: datetime
    expires_at: datetime
    policy_revision: int
    provider_account_revision: int
    provider_account_last_scope_change_at: datetime | None


@dataclass(frozen=True, slots=True)
class LinkCodeIssueResult:
    status: LinkCodeIssueStatus
    error_code: IdentityErrorCode | None = None
    code: str | None = field(default=None, repr=False)
    expires_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ProvisioningResult:
    status: ProvisioningStatus
    outcome: ProvisioningOutcome | None = None
    error_code: IdentityErrorCode | None = None
    identity: ExternalIdentityRecord | None = field(default=None, repr=False)
    membership: UserMembershipRecord | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class ProvisioningPolicyCreate:
    tenant_id: str = field(repr=False)
    mode: ProvisioningMode
    link_code_ttl_seconds: int
    changed_at: datetime


@dataclass(frozen=True, slots=True)
class ProvisioningPolicyUpdate:
    tenant_id: str = field(repr=False)
    expected_revision: int
    mode: ProvisioningMode
    link_code_ttl_seconds: int
    changed_at: datetime


@dataclass(frozen=True, slots=True)
class ProvisioningPolicyWriteResult:
    outcome: ProvisioningPolicyWriteOutcome
    snapshot: ProvisioningPolicySnapshot | None = field(default=None, repr=False)


class ProvisioningRepositoryError(RuntimeError):
    """Safe persistence failure without provider identifiers or SQL text."""

    def __init__(self, code: IdentityErrorCode) -> None:
        self.code = code
        super().__init__(code.value)


@runtime_checkable
class IdentityProvisioningRepository(Protocol):
    """Atomic I6 use cases; no generic CRUD or transaction controls."""

    async def provision_verified_identity(
        self,
        command: VerifiedProvisioningCommand,
    ) -> ProvisioningResult: ...

    async def issue_link_code(
        self,
        command: LinkCodeIssueCommand,
    ) -> LinkCodeGrantRecord: ...


@runtime_checkable
class ProvisioningPolicyAdministrationRepository(Protocol):
    """Explicit onboarding/CAS capability kept away from ordinary services."""

    async def create_policy(
        self,
        command: ProvisioningPolicyCreate,
    ) -> ProvisioningPolicyWriteResult: ...

    async def cas_policy(
        self,
        command: ProvisioningPolicyUpdate,
    ) -> ProvisioningPolicyWriteResult: ...


__all__ = [
    "IdentityProvisioningRepository",
    "LinkCodeGrantRecord",
    "LinkCodeIssueCommand",
    "LinkCodeIssueRequest",
    "LinkCodeIssueResult",
    "LinkCodeIssueStatus",
    "ProvisionIdentityRequest",
    "ProvisioningOutcome",
    "ProvisioningPolicyAdministrationRepository",
    "ProvisioningPolicyCreate",
    "ProvisioningPolicyUpdate",
    "ProvisioningPolicyWriteOutcome",
    "ProvisioningPolicyWriteResult",
    "ProvisioningRepositoryError",
    "ProvisioningResult",
    "ProvisioningStatus",
    "ReverifyResolvedIdentityRequest",
    "VerifiedProvisioningAlias",
    "VerifiedProvisioningCommand",
]
