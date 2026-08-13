"""Framework-neutral contracts for verified Channel identity onboarding.

The public plan/result projections deliberately omit tenant, provider tenant,
account, channel, and credential values.  Those values remain available only
inside the sealed, in-process intent passed from the orchestration service to
the privileged repository adapter.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable

from api.identity.contracts import IdentityErrorCode, ProviderContext, ProvisioningMode
from api.identity.providers.contracts import FeishuDomain, ProviderErrorCode
from api.identity.validation import valid_provider_context

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_DATABASE_REVISION = (1 << 63) - 1


class ChannelOnboardingAction(StrEnum):
    CREATE_PROVIDER_TENANT = "create_provider_tenant"
    CREATE_PROVIDER_ACCOUNT = "create_provider_account"
    MARK_PROVIDER_ACCOUNT_HEALTHY = "mark_provider_account_healthy"
    CREATE_TENANT_POLICY = "create_tenant_policy"
    CREATE_CHANNEL_LINK = "create_channel_link"


class ChannelOnboardingStatus(StrEnum):
    APPLIED = "applied"
    UNCHANGED = "unchanged"


class ChannelOnboardingError(RuntimeError):
    """Sanitized failure carrying only an existing stable identity code."""

    code: IdentityErrorCode | ProviderErrorCode

    def __init__(self, code: object) -> None:
        if not isinstance(code, IdentityErrorCode | ProviderErrorCode):
            code = IdentityErrorCode.ASSERTION_INVALID
        self.code = code
        super().__init__(code.value)


@dataclass(frozen=True, slots=True)
class ChannelOnboardingRequest:
    channel_id: str = field(repr=False)
    mode: ProvisioningMode
    link_code_ttl_seconds: int

    def __post_init__(self) -> None:
        if not _valid_text(self.channel_id, max_length=32) or not isinstance(self.mode, ProvisioningMode) or type(self.link_code_ttl_seconds) is not int or not 60 <= self.link_code_ttl_seconds <= 900:
            raise ChannelOnboardingError(IdentityErrorCode.ASSERTION_INVALID)


@dataclass(frozen=True, slots=True)
class ChannelOnboardingSourceSnapshot:
    """Non-secret revision fingerprint captured before provider verification."""

    channel_id: str = field(repr=False)
    tenant_id: str = field(repr=False)
    provider: str
    channel_generation: int = field(repr=False)
    public_config_digest: str = field(repr=False)
    secret_version: int = field(repr=False)
    secret_envelope_digest: str = field(repr=False)
    provider_account_key: str = field(repr=False)
    domain: FeishuDomain = field(repr=False)

    def __post_init__(self) -> None:
        if (
            not _valid_text(self.channel_id, max_length=32)
            or not _valid_text(self.tenant_id, max_length=32)
            or self.provider != "feishu"
            or not _valid_revision(self.channel_generation)
            or not _valid_digest(self.public_config_digest)
            or not _valid_revision(self.secret_version)
            or not _valid_digest(self.secret_envelope_digest)
            or not _valid_text(self.provider_account_key, max_length=255)
            or not isinstance(self.domain, FeishuDomain)
        ):
            raise ChannelOnboardingError(IdentityErrorCode.ASSERTION_INVALID)


@dataclass(frozen=True, slots=True)
class FeishuChannelOnboardingSource:
    """Short-lived decrypted source; never retained by a plan."""

    snapshot: ChannelOnboardingSourceSnapshot = field(repr=False)
    app_secret: str = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot, ChannelOnboardingSourceSnapshot) or not _valid_text(self.app_secret, max_length=4_096):
            raise ChannelOnboardingError(ProviderErrorCode.CREDENTIAL_UNAVAILABLE)


@dataclass(frozen=True, slots=True)
class VerifiedFeishuInstallation:
    """Fresh Auth V3 plus Tenant V2 proof, stripped of access tokens."""

    provider: str
    provider_tenant_key: str = field(repr=False)
    provider_account_key: str = field(repr=False)
    verified_at: datetime = field(repr=False)

    def __post_init__(self) -> None:
        if (
            self.provider != "feishu"
            or not _valid_text(self.provider_tenant_key, max_length=255)
            or not _valid_text(self.provider_account_key, max_length=255)
            or not _valid_timestamp(self.verified_at)
        ):
            raise ChannelOnboardingError(ProviderErrorCode.ASSERTION_INVALID)


@dataclass(frozen=True, slots=True)
class VerifiedChannelOnboardingIntent:
    """In-process authority created only after live provider verification."""

    source: ChannelOnboardingSourceSnapshot = field(repr=False)
    proof: VerifiedFeishuInstallation = field(repr=False)
    mode: ProvisioningMode
    link_code_ttl_seconds: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.source, ChannelOnboardingSourceSnapshot)
            or not isinstance(self.proof, VerifiedFeishuInstallation)
            or self.source.provider != self.proof.provider
            or self.source.provider_account_key != self.proof.provider_account_key
            or not isinstance(self.mode, ProvisioningMode)
            or type(self.link_code_ttl_seconds) is not int
            or not 60 <= self.link_code_ttl_seconds <= 900
        ):
            raise ChannelOnboardingError(IdentityErrorCode.ASSERTION_INVALID)


@dataclass(frozen=True, slots=True, weakref_slot=True)
class ChannelOnboardingPlan:
    """Safe dry-run projection plus a service-instance-bound private intent."""

    actions: tuple[ChannelOnboardingAction, ...]
    mode: ProvisioningMode
    link_code_ttl_seconds: int
    _intent: VerifiedChannelOnboardingIntent = field(repr=False)
    _seal: object = field(repr=False)

    def __post_init__(self) -> None:
        if (
            type(self.actions) is not tuple
            or any(not isinstance(action, ChannelOnboardingAction) for action in self.actions)
            or len(set(self.actions)) != len(self.actions)
            or not isinstance(self.mode, ProvisioningMode)
            or type(self.link_code_ttl_seconds) is not int
            or not 60 <= self.link_code_ttl_seconds <= 900
            or not isinstance(self._intent, VerifiedChannelOnboardingIntent)
        ):
            raise ChannelOnboardingError(IdentityErrorCode.ASSERTION_INVALID)


@dataclass(frozen=True, slots=True)
class ChannelOnboardingResult:
    status: ChannelOnboardingStatus
    actions: tuple[ChannelOnboardingAction, ...]
    mode: ProvisioningMode
    policy_revision: int
    provider_account_revision: int
    context: ProviderContext = field(repr=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.status, ChannelOnboardingStatus)
            or type(self.actions) is not tuple
            or any(not isinstance(action, ChannelOnboardingAction) for action in self.actions)
            or len(set(self.actions)) != len(self.actions)
            or not isinstance(self.mode, ProvisioningMode)
            or not _valid_revision(self.policy_revision)
            or not _valid_revision(self.provider_account_revision)
            or not valid_provider_context(self.context)
            or self.context.provider_account_revision != self.provider_account_revision
        ):
            raise ChannelOnboardingError(IdentityErrorCode.REPOSITORY_UNAVAILABLE)


@runtime_checkable
class ChannelOnboardingCredentialSource(Protocol):
    async def load(self, channel_id: str) -> FeishuChannelOnboardingSource: ...


@runtime_checkable
class FeishuInstallationVerifier(Protocol):
    async def verify(self, source: FeishuChannelOnboardingSource) -> VerifiedFeishuInstallation: ...


@runtime_checkable
class VerifiedChannelOnboardingRepository(Protocol):
    async def preview(self, intent: VerifiedChannelOnboardingIntent) -> tuple[ChannelOnboardingAction, ...]: ...

    async def apply(self, intent: VerifiedChannelOnboardingIntent) -> ChannelOnboardingResult: ...


@runtime_checkable
class IdentityLinkCodeCodecFactory(Protocol):
    """Narrow readiness seam; production must construct the real codec."""

    def __call__(self) -> object: ...


def _valid_text(value: object, *, max_length: int) -> bool:
    return type(value) is str and bool(value.strip()) and len(value) <= max_length


def _valid_revision(value: object) -> bool:
    return type(value) is int and 1 <= value <= _MAX_DATABASE_REVISION


def _valid_digest(value: object) -> bool:
    return type(value) is str and _DIGEST_RE.fullmatch(value) is not None


def _valid_timestamp(value: object) -> bool:
    return isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None
