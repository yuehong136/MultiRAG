"""Framework-neutral enterprise identity contracts for EIM-I3."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable


class ProvisioningMode(StrEnum):
    PREPROVISIONED = "preprovisioned"
    LINK_ONLY = "link_only"
    JIT = "jit"


class ProvisioningAction(StrEnum):
    BIND_PREPROVISIONED = "bind_preprovisioned"
    REQUIRE_LINK = "require_link"
    CREATE_NORMAL_MEMBER = "create_normal_member"


class ProviderAliasType(StrEnum):
    OPEN_ID = "open_id"
    UNION_ID = "union_id"


class IdentityResolutionStatus(StrEnum):
    RESOLVED = "resolved"
    MISSING = "missing"
    INACTIVE = "inactive"
    CONFLICT = "conflict"


class IdentityErrorCode(StrEnum):
    ASSERTION_INVALID = "IDENTITY_ASSERTION_INVALID"
    PROVIDER_MISMATCH = "IDENTITY_PROVIDER_MISMATCH"
    TENANT_MISMATCH = "IDENTITY_TENANT_MISMATCH"
    INACTIVE = "IDENTITY_INACTIVE"
    LINK_REQUIRED = "IDENTITY_LINK_REQUIRED"
    LINK_CONFLICT = "IDENTITY_LINK_CONFLICT"
    NOT_FOUND = "IDENTITY_NOT_FOUND"
    REVISION_CONFLICT = "IDENTITY_REVISION_CONFLICT"
    TRANSITION_INVALID = "IDENTITY_TRANSITION_INVALID"
    OWNERSHIP_CONFLICT = "IDENTITY_OWNERSHIP_CONFLICT"
    POLICY_UNAVAILABLE = "IDENTITY_POLICY_UNAVAILABLE"
    REPOSITORY_UNAVAILABLE = "IDENTITY_REPOSITORY_UNAVAILABLE"


class InsertOutcome(StrEnum):
    CREATED = "created"
    EXISTING = "existing"


class CasOutcome(StrEnum):
    APPLIED = "applied"
    NOT_FOUND = "not_found"
    REVISION_CONFLICT = "revision_conflict"
    INVALID_TRANSITION = "invalid_transition"


@dataclass(frozen=True, slots=True)
class ProviderContext:
    """Server-built scope for one verified provider installation."""

    tenant_id: str
    provider: str
    provider_tenant_key: str = field(repr=False)
    provider_account_id: str = field(repr=False)
    provider_account_key: str = field(repr=False)
    provider_account_revision: int
    provider_account_last_scope_change_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class AliasKey:
    alias_type: ProviderAliasType
    alias_value: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class ProviderAccountRecord:
    id: str = field(repr=False)
    tenant_id: str
    provider: str
    provider_tenant_key: str = field(repr=False)
    provider_account_key: str = field(repr=False)
    identity_revision: int
    identity_health_state: str
    identity_health_error_code: str | None = field(default=None, repr=False)
    last_scope_change_at: datetime | None = None
    last_directory_event_at: datetime | None = None

    def context(self) -> ProviderContext:
        return ProviderContext(
            tenant_id=self.tenant_id,
            provider=self.provider,
            provider_tenant_key=self.provider_tenant_key,
            provider_account_id=self.id,
            provider_account_key=self.provider_account_key,
            provider_account_revision=self.identity_revision,
            provider_account_last_scope_change_at=self.last_scope_change_at,
        )


@dataclass(frozen=True, slots=True)
class ProviderTenantRecord:
    id: str = field(repr=False)
    tenant_id: str
    provider: str
    provider_tenant_key: str = field(repr=False)
    verified_at: datetime


@dataclass(frozen=True, slots=True)
class ExternalIdentityRecord:
    id: str = field(repr=False)
    tenant_id: str
    user_id: str = field(repr=False)
    provider: str
    provider_tenant_key: str = field(repr=False)
    subject_type: str
    subject_value: str = field(repr=False)
    state: str
    verified_at: datetime | None
    last_seen_at: datetime | None
    identity_revision: int
    attributes: tuple[tuple[str, str | None], ...] = field(default=(), repr=False)


@dataclass(frozen=True, slots=True)
class UserMembershipRecord:
    user_id: str = field(repr=False)
    tenant_id: str
    role: str


@dataclass(frozen=True, slots=True)
class IdentityResolutionRequest:
    context: ProviderContext
    alias: AliasKey


@dataclass(frozen=True, slots=True)
class IdentityResolutionSnapshot:
    """One database-authoritative account, identity, and membership snapshot.

    ``None`` for the snapshot means the supplied ProviderContext is invalid.
    ``identity=None`` means the account is valid but the alias is not linked.
    """

    account: ProviderAccountRecord
    identity: ExternalIdentityRecord | None
    membership: UserMembershipRecord | None
    alias_verified_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class IdentityResolutionResult:
    status: IdentityResolutionStatus
    error_code: IdentityErrorCode | None = None
    identity: ExternalIdentityRecord | None = field(default=None, repr=False)
    membership: UserMembershipRecord | None = field(default=None, repr=False)
    provisioning_action: ProvisioningAction | None = None
    provisioning_policy_revision: int | None = None
    provider_verification_required: bool = False


@dataclass(frozen=True, slots=True)
class ProvisioningDecision:
    action: ProvisioningAction
    member_role: str | None = None


@dataclass(frozen=True, slots=True)
class ProvisioningPolicySnapshot:
    """Database-authoritative tenant provisioning policy generation."""

    tenant_id: str = field(repr=False)
    mode: ProvisioningMode
    revision: int
    link_code_ttl_seconds: int
    changed_at: datetime


@dataclass(frozen=True, slots=True)
class InsertResult:
    outcome: InsertOutcome
    record: ExternalIdentityRecord = field(repr=False)


@dataclass(frozen=True, slots=True)
class ProviderTenantInsertResult:
    outcome: InsertOutcome
    record: ProviderTenantRecord = field(repr=False)


@dataclass(frozen=True, slots=True)
class ProviderAccountInsertResult:
    outcome: InsertOutcome
    record: ProviderAccountRecord = field(repr=False)


@dataclass(frozen=True, slots=True)
class ExternalIdentityInsert:
    context: ProviderContext
    user_id: str = field(repr=False)
    subject_type: str
    subject_value: str = field(repr=False)
    attributes: tuple[tuple[str, str | None], ...] = field(default=(), repr=False)


@dataclass(frozen=True, slots=True)
class ExternalIdentityAliasInsert:
    context: ProviderContext
    external_identity_id: str = field(repr=False)
    alias_type: ProviderAliasType
    alias_value: str = field(repr=False)
    verified_at: datetime


@dataclass(frozen=True, slots=True)
class VerifiedProviderTenantOnboarding:
    """Persistence command accepted only after external ownership verification."""

    tenant_id: str
    provider: str
    provider_tenant_key: str = field(repr=False)
    verified_at: datetime


@dataclass(frozen=True, slots=True)
class VerifiedProviderAccountOnboarding:
    """Persistence command accepted only after installation verification."""

    tenant_id: str
    provider: str
    provider_tenant_key: str = field(repr=False)
    provider_account_key: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class IdentityStateTransition:
    context: ProviderContext
    external_identity_id: str = field(repr=False)
    expected_revision: int
    target_state: str


@dataclass(frozen=True, slots=True)
class VerifiedIdentityActivation:
    context: ProviderContext
    external_identity_id: str = field(repr=False)
    expected_revision: int
    verified_at: datetime


@dataclass(frozen=True, slots=True)
class ProviderAccountHealthCAS:
    context: ProviderContext
    target_health_state: str
    error_code: str | None = field(default=None, repr=False)
    last_scope_change_at: datetime | None = None
    last_directory_event_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class CasResult:
    outcome: CasOutcome
    revision: int | None = None


@runtime_checkable
class IdentityLookupRepository(Protocol):
    async def get_provider_account(self, context: ProviderContext, *, for_update: bool = False) -> ProviderAccountRecord | None: ...

    async def resolve_identity(self, request: IdentityResolutionRequest) -> IdentityResolutionSnapshot | None: ...


@runtime_checkable
class IdentityMutationRepository(Protocol):
    """Ordinary mutation surface; cannot assert fresh provider verification."""

    async def insert_identity(self, command: ExternalIdentityInsert) -> InsertResult: ...

    async def cas_identity_state(self, command: IdentityStateTransition) -> CasResult: ...


@runtime_checkable
class VerifiedIdentityMutationRepository(Protocol):
    """Mutations restricted to callers holding fresh provider verification."""

    async def insert_alias(self, command: ExternalIdentityAliasInsert) -> InsertResult: ...

    async def activate_verified_identity(self, command: VerifiedIdentityActivation) -> CasResult: ...


@runtime_checkable
class ProviderAccountControlRepository(Protocol):
    """Provider-control surface; ordinary identity provisioning never receives it."""

    async def cas_provider_account_health(self, command: ProviderAccountHealthCAS) -> CasResult: ...


@runtime_checkable
class IdentityRepository(
    IdentityLookupRepository,
    IdentityMutationRepository,
    VerifiedIdentityMutationRepository,
    ProviderAccountControlRepository,
    Protocol,
):
    """Complete persistence port; services should depend on its narrow sub-protocols."""


@runtime_checkable
class VerifiedOwnershipRepository(Protocol):
    """Privileged onboarding surface; ordinary identity services never receive it."""

    async def insert_verified_provider_tenant(
        self,
        command: VerifiedProviderTenantOnboarding,
    ) -> ProviderTenantInsertResult: ...

    async def insert_verified_provider_account(
        self,
        command: VerifiedProviderAccountOnboarding,
    ) -> ProviderAccountInsertResult: ...


@runtime_checkable
class ProvisioningPolicyResolver(Protocol):
    async def get_policy(
        self,
        tenant_id: str,
    ) -> ProvisioningPolicySnapshot | None: ...
