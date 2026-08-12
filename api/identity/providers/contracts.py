"""Framework-neutral contracts for enterprise directory providers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable

from api.identity.contracts import ProviderContext


class ProviderIdentityStatus(StrEnum):
    RESOLVED = "resolved"
    NOT_FOUND = "not_found"
    NOT_IN_SCOPE = "not_in_scope"
    INACTIVE = "inactive"
    UNAVAILABLE = "unavailable"
    INVALID = "invalid"
    CONFLICT = "conflict"


class ProviderIdentifierKind(StrEnum):
    OPEN_ID = "open_id"
    USER_ID = "user_id"
    UNION_ID = "union_id"


class FeishuDomain(StrEnum):
    FEISHU = "feishu"
    LARK = "lark"


class ProviderDirectoryStatus(StrEnum):
    ACTIVE = "active"


class FeishuClientFailure(StrEnum):
    TRANSPORT = "transport"
    RESPONSE_INVALID = "response_invalid"


class ProviderErrorCode(StrEnum):
    ASSERTION_INVALID = "IDENTITY_ASSERTION_INVALID"
    PROVIDER_MISMATCH = "IDENTITY_PROVIDER_MISMATCH"
    TENANT_MISMATCH = "IDENTITY_TENANT_MISMATCH"
    NOT_IN_SCOPE = "IDENTITY_NOT_IN_SCOPE"
    NOT_FOUND = "IDENTITY_NOT_FOUND"
    INACTIVE = "IDENTITY_INACTIVE"
    PROVIDER_UNAVAILABLE = "IDENTITY_PROVIDER_UNAVAILABLE"
    LINK_CONFLICT = "IDENTITY_LINK_CONFLICT"
    CREDENTIAL_UNAVAILABLE = "IDENTITY_PROVIDER_CREDENTIAL_UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class ExternalIdentityIdentifier:
    kind: ProviderIdentifierKind
    value: str = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ProviderIdentifierKind) or type(self.value) is not str or not self.value.strip() or len(self.value) > 255:
            raise ValueError("provider identifier is invalid")


@dataclass(frozen=True, slots=True)
class ExternalIdentityAssertion:
    provider: str
    provider_tenant_key: str | None = field(default=None, repr=False)
    identifiers: tuple[ExternalIdentityIdentifier, ...] = field(default=(), repr=False)

    def __post_init__(self) -> None:
        if type(self.provider) is not str or not self.provider.strip() or len(self.provider) > 64:
            raise ValueError("provider assertion is invalid")
        if self.provider_tenant_key is not None and (type(self.provider_tenant_key) is not str or not self.provider_tenant_key.strip() or len(self.provider_tenant_key) > 255):
            raise ValueError("provider assertion is invalid")
        if type(self.identifiers) is not tuple or not 1 <= len(self.identifiers) <= 3:
            raise ValueError("provider assertion is invalid")


@dataclass(frozen=True, slots=True)
class FeishuProviderCredential:
    provider_account_id: str = field(repr=False)
    app_id: str = field(repr=False)
    app_secret: str = field(repr=False)
    credential_generation: int
    domain: FeishuDomain = FeishuDomain.FEISHU

    def __post_init__(self) -> None:
        if (
            type(self.provider_account_id) is not str
            or not self.provider_account_id.strip()
            or len(self.provider_account_id) > 32
            or type(self.app_id) is not str
            or not self.app_id.strip()
            or len(self.app_id) > 255
            or type(self.app_secret) is not str
            or not self.app_secret.strip()
            or len(self.app_secret) > 4_096
            or type(self.credential_generation) is not int
            or not 1 <= self.credential_generation <= (1 << 63) - 1
            or not isinstance(self.domain, FeishuDomain)
        ):
            raise ValueError("provider credential is invalid")


class ProviderCredentialError(RuntimeError):
    """Safe credential resolution failure without secret material."""

    def __init__(self, code: ProviderErrorCode) -> None:
        self.code = code
        super().__init__(code.value)


class FeishuDirectoryClientError(RuntimeError):
    """Sanitized SDK adapter failure; never carries an upstream response."""

    def __init__(self, failure: FeishuClientFailure) -> None:
        self.failure = failure
        super().__init__(failure.value)


@dataclass(frozen=True, slots=True)
class FeishuTenantTokenResponse:
    http_status: int
    code: int
    tenant_access_token: str | None = field(default=None, repr=False)
    expires_in_seconds: int | None = None


@dataclass(frozen=True, slots=True)
class FeishuDirectoryUser:
    user_id: str = field(repr=False)
    open_id: str | None = field(default=None, repr=False)
    union_id: str | None = field(default=None, repr=False)
    employee_no: str | None = field(default=None, repr=False)
    display_name: str | None = field(default=None, repr=False)
    is_frozen: bool = False
    is_resigned: bool = False
    is_activated: bool = False
    is_exited: bool = False
    is_unjoin: bool = False


@dataclass(frozen=True, slots=True)
class FeishuGetUserResponse:
    http_status: int
    code: int
    user: FeishuDirectoryUser | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class FeishuTenantResponse:
    http_status: int
    code: int
    tenant_key: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class ProviderIdentity:
    provider: str
    provider_tenant_key: str = field(repr=False)
    provider_account_id: str = field(repr=False)
    provider_user_id: str = field(repr=False)
    verified_at: datetime
    open_id: str | None = field(default=None, repr=False)
    union_id: str | None = field(default=None, repr=False)
    employee_no: str | None = field(default=None, repr=False)
    display_name: str | None = field(default=None, repr=False)
    provider_status: ProviderDirectoryStatus = ProviderDirectoryStatus.ACTIVE

    def __post_init__(self) -> None:
        if self.verified_at.tzinfo is None or self.verified_at.utcoffset() is None:
            raise ValueError("provider identity proof time must be timezone-aware")


@dataclass(frozen=True, slots=True)
class ProviderIdentityResult:
    status: ProviderIdentityStatus
    identity: ProviderIdentity | None = field(default=None, repr=False)
    error_code: ProviderErrorCode | None = None
    retryable: bool = False
    from_cache: bool = False


@runtime_checkable
class ProviderCredentialResolver(Protocol):
    async def resolve(self, context: ProviderContext) -> FeishuProviderCredential: ...


@runtime_checkable
class FeishuDirectoryClient(Protocol):
    async def fetch_tenant_token(
        self,
        credential: FeishuProviderCredential,
    ) -> FeishuTenantTokenResponse: ...

    async def get_user(
        self,
        credential: FeishuProviderCredential,
        *,
        tenant_access_token: str,
        identifier_type: str,
        identifier_value: str,
    ) -> FeishuGetUserResponse: ...

    async def get_tenant(
        self,
        credential: FeishuProviderCredential,
        *,
        tenant_access_token: str,
    ) -> FeishuTenantResponse: ...


@runtime_checkable
class EnterpriseIdentityProvider(Protocol):
    async def resolve(
        self,
        context: ProviderContext,
        assertion: ExternalIdentityAssertion,
    ) -> ProviderIdentityResult: ...

    async def refresh(
        self,
        context: ProviderContext,
        provider_user_id: str,
    ) -> ProviderIdentityResult: ...
