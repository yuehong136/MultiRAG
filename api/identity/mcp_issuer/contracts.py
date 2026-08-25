"""Framework-neutral EIM-A2 issuer contracts and production policy."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from urllib.parse import urlsplit

from api.identity.principal import Principal

MAX_TOKEN_BYTES = 4096
MAX_TOKEN_TTL_SECONDS = 300
TOKEN_TYPE = "at+jwt"
TOKEN_USE = "mcp_access"
ALGORITHM = "ES256"
ENTERPRISE_ACR = "urn:multirag:assurance:enterprise-verified"
ALLOWED_REQUESTED_CLAIMS = frozenset({"enterprise_subject", "provider_identity"})


class IssuanceErrorCode(StrEnum):
    SUBJECT_NOT_PLATFORM_PRINCIPAL = "subject_not_platform_principal"
    REQUESTED_SCOPE_NOT_REGISTERED = "requested_scope_not_registered"
    REQUESTED_SCOPE_NOT_GRANTED = "requested_scope_not_granted"
    REQUESTED_CLAIM_NOT_ALLOWED = "requested_claim_not_allowed"
    TENANT_NOT_SERVER_BOUND = "tenant_not_server_bound"
    ASSURANCE_NOT_VERIFIED = "assurance_not_verified"
    RESOURCE_NOT_REGISTERED = "resource_not_registered"
    AGENT_INVALID = "agent_invalid"
    REQUEST_INVALID = "request_invalid"
    AUTH_TIME_INVALID = "auth_time_invalid"
    JTI_INVALID = "jti_invalid"
    TOKEN_TOO_LARGE = "token_too_large"
    SIGNING_FAILED = "signing_failed"


class McpTokenIssuanceError(ValueError):
    """Stable non-sensitive refusal from the A2 issuer."""

    def __init__(self, code: IssuanceErrorCode) -> None:
        self.code = code
        super().__init__("MCP access token issuance rejected")


def _canonical_https_uri(value: object) -> bool:
    if type(value) is not str or not value or value != value.strip() or not value.isascii() or len(value) > 4096:
        return False
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.netloc or parsed.hostname is None:
        return False
    if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        return False
    try:
        _ = parsed.port
    except ValueError:
        return False
    return True


def _nonempty_text(value: object, *, max_length: int = 128) -> bool:
    return type(value) is str and 1 <= len(value) <= max_length and value.isascii() and all("!" <= char <= "~" for char in value)


def _scope_set(value: object, *, allow_empty: bool = False) -> bool:
    if type(value) is not frozenset or (not value and not allow_empty):
        return False
    return all(_nonempty_text(item) and " " not in item and '"' not in item and "\\" not in item for item in value)


@dataclass(frozen=True, slots=True)
class EnterpriseSubjectRequirement:
    subject_type: str
    issuer: str
    issuer_tenant: str = field(repr=False)

    def __post_init__(self) -> None:
        if (
            self.subject_type not in {"employee_no", "talent_id", "workcode"}
            or type(self.issuer) is not str
            or not self.issuer.strip()
            or len(self.issuer) > 255
            or type(self.issuer_tenant) is not str
            or not self.issuer_tenant.strip()
            or len(self.issuer_tenant) > 255
        ):
            raise ValueError("invalid enterprise subject requirement")


@dataclass(frozen=True, slots=True)
class McpResourceProfile:
    name: str
    audience: str
    registered_scopes: frozenset[str]
    allow_provider_identity: bool = False
    enterprise_subject_requirement: EnterpriseSubjectRequirement | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if (
            not _nonempty_text(self.name, max_length=64)
            or not _canonical_https_uri(self.audience)
            or not _scope_set(self.registered_scopes)
            or type(self.allow_provider_identity) is not bool
            or (self.enterprise_subject_requirement is not None and not isinstance(self.enterprise_subject_requirement, EnterpriseSubjectRequirement))
        ):
            raise ValueError("invalid MCP resource profile")

    @property
    def allowed_requested_claims(self) -> frozenset[str]:
        claims: set[str] = set()
        if self.allow_provider_identity:
            claims.add("provider_identity")
        if self.enterprise_subject_requirement is not None:
            claims.add("enterprise_subject")
        return frozenset(claims)


@dataclass(frozen=True, slots=True)
class McpIssuerProfile:
    issuer: str
    client_id: str
    ttl_seconds: int
    jwks_cache_ttl_seconds: int
    resources: tuple[McpResourceProfile, ...]

    def __post_init__(self) -> None:
        names = [resource.name for resource in self.resources]
        audiences = [resource.audience for resource in self.resources]
        if (
            not _canonical_https_uri(self.issuer)
            or not _nonempty_text(self.client_id)
            or type(self.ttl_seconds) is not int
            or not 1 <= self.ttl_seconds <= MAX_TOKEN_TTL_SECONDS
            or type(self.jwks_cache_ttl_seconds) is not int
            or self.jwks_cache_ttl_seconds <= 0
            or not self.resources
            or any(not isinstance(resource, McpResourceProfile) for resource in self.resources)
            or len(names) != len(set(names))
            or len(audiences) != len(set(audiences))
        ):
            raise ValueError("invalid MCP issuer profile")

    def resource(self, name: str) -> McpResourceProfile | None:
        return next((resource for resource in self.resources if resource.name == name), None)


@dataclass(frozen=True, slots=True)
class McpAccessTokenRequest:
    principal: Principal = field(repr=False)
    agent_id: str
    resource_name: str
    requested_scopes: frozenset[str]
    requested_claims: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if (
            not isinstance(self.principal, Principal)
            or not _nonempty_text(self.agent_id)
            or not _nonempty_text(self.resource_name, max_length=64)
            or not _scope_set(self.requested_scopes)
            or type(self.requested_claims) is not frozenset
            or any(not _nonempty_text(claim, max_length=64) for claim in self.requested_claims)
        ):
            raise McpTokenIssuanceError(IssuanceErrorCode.REQUEST_INVALID)


@dataclass(frozen=True, slots=True)
class McpAccessGrant:
    """Server-authoritative policy intersection supplied by the future P3 seam."""

    allowed_scopes: frozenset[str]

    def __post_init__(self) -> None:
        if not _scope_set(self.allowed_scopes, allow_empty=True):
            raise McpTokenIssuanceError(IssuanceErrorCode.REQUEST_INVALID)


@dataclass(frozen=True, slots=True)
class IssuancePolicyFacts:
    """Facts evaluated before signing; A2 maps A1 issuance vectors here."""

    subject_is_platform_principal: bool
    tenant_is_server_bound: bool
    registered_scopes: frozenset[str]
    allowed_scopes: frozenset[str]
    requested_scopes: frozenset[str]
    requested_claims: frozenset[str]
    requires_assurance: bool
    assurance_verified: bool
    allowed_requested_claims: frozenset[str] = ALLOWED_REQUESTED_CLAIMS


@dataclass(frozen=True, slots=True)
class IssuancePolicyDecision:
    allowed: bool
    failure_reason: str | None = None


def evaluate_issuance_policy(facts: IssuancePolicyFacts) -> IssuancePolicyDecision:
    """Evaluate the frozen A1 issuance order without creating a token."""

    if not facts.subject_is_platform_principal:
        code = IssuanceErrorCode.SUBJECT_NOT_PLATFORM_PRINCIPAL
    elif not facts.requested_scopes.issubset(facts.registered_scopes):
        code = IssuanceErrorCode.REQUESTED_SCOPE_NOT_REGISTERED
    elif not facts.requested_scopes.issubset(facts.allowed_scopes):
        code = IssuanceErrorCode.REQUESTED_SCOPE_NOT_GRANTED
    elif not facts.requested_claims.issubset(facts.allowed_requested_claims):
        code = IssuanceErrorCode.REQUESTED_CLAIM_NOT_ALLOWED
    elif not facts.tenant_is_server_bound:
        code = IssuanceErrorCode.TENANT_NOT_SERVER_BOUND
    elif facts.requires_assurance and not facts.assurance_verified:
        code = IssuanceErrorCode.ASSURANCE_NOT_VERIFIED
    else:
        return IssuancePolicyDecision(allowed=True)
    return IssuancePolicyDecision(allowed=False, failure_reason=code.value)


@dataclass(frozen=True, slots=True)
class IssuedMcpAccessToken:
    compact: str = field(repr=False)
    expires_at: datetime
    scopes: frozenset[str]
    kid: str


__all__ = [
    "ALGORITHM",
    "ALLOWED_REQUESTED_CLAIMS",
    "ENTERPRISE_ACR",
    "MAX_TOKEN_BYTES",
    "TOKEN_TYPE",
    "TOKEN_USE",
    "EnterpriseSubjectRequirement",
    "IssuanceErrorCode",
    "IssuancePolicyDecision",
    "IssuancePolicyFacts",
    "IssuedMcpAccessToken",
    "McpAccessGrant",
    "McpAccessTokenRequest",
    "McpIssuerProfile",
    "McpResourceProfile",
    "McpTokenIssuanceError",
    "evaluate_issuance_policy",
]
