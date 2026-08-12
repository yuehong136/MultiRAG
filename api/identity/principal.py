"""Framework-neutral platform Principal contracts and construction rules.

The canonical Principal belongs to the identity domain.  HTTP/JWT, Channel,
MCP, and ORM adapters may construct it only from server-verified evidence; the
domain object itself never carries an ORM row, tenant role, permission set, or
provider credential.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from api.identity.contracts import (
    IdentityResolutionResult,
    IdentityResolutionStatus,
)

_MAX_PLATFORM_ID = 32
_MAX_PROVIDER = 64
_MAX_SUBJECT_TYPE = 64
_MAX_SUBJECT = 255
_MAX_ISSUER = 128
_MAX_ISSUER_TENANT = 255
_MAX_DISPLAY_NAME = 512
_ENTERPRISE_SUBJECT_TYPES = frozenset({"employee_no", "talent_id", "workcode"})


class AuthenticationSource(StrEnum):
    """Credential boundary that authenticated the current caller."""

    WEB_SESSION = "web_session"
    SDK_API_TOKEN = "sdk_api_token"
    ENTERPRISE_IDENTITY = "enterprise_identity"


class IdentityAssurance(StrEnum):
    """Strongest server-verified identity evidence carried by a Principal."""

    AUTHENTICATED = "authenticated"
    DIRECTORY_VERIFIED = "directory_verified"
    ENTERPRISE_VERIFIED = "enterprise_verified"


class PrincipalErrorCode(StrEnum):
    INPUT_INVALID = "PRINCIPAL_INPUT_INVALID"
    AUTHENTICATION_INVALID = "PRINCIPAL_AUTHENTICATION_INVALID"
    TENANT_REQUIRED = "PRINCIPAL_TENANT_REQUIRED"
    TENANT_MISMATCH = "PRINCIPAL_TENANT_MISMATCH"
    MEMBERSHIP_INACTIVE = "PRINCIPAL_MEMBERSHIP_INACTIVE"
    IDENTITY_INACTIVE = "PRINCIPAL_IDENTITY_INACTIVE"
    ASSURANCE_INVALID = "PRINCIPAL_ASSURANCE_INVALID"
    CONTEXT_MISSING = "PRINCIPAL_CONTEXT_MISSING"
    CONTEXT_CONFLICT = "PRINCIPAL_CONTEXT_CONFLICT"


class PrincipalBuildError(ValueError):
    """Stable, non-PII failure raised when Principal evidence is inconsistent."""

    def __init__(self, code: PrincipalErrorCode) -> None:
        self.code = code
        super().__init__("principal construction rejected")


def _valid_text(value: object, *, max_length: int) -> bool:
    return type(value) is str and 0 < len(value.strip()) and len(value) <= max_length


def _aware(value: object) -> bool:
    return isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None


def _require_aware_time(value: object) -> datetime:
    if not _aware(value):
        raise PrincipalBuildError(PrincipalErrorCode.ASSURANCE_INVALID)
    assert isinstance(value, datetime)
    return value


@dataclass(frozen=True, slots=True)
class AuthenticationContext:
    """Facts proven at the current authentication boundary.

    ``validated_at`` is when MultiRAG verified this request credential.
    ``authenticated_at`` is a real upstream human-authentication time and must
    remain ``None`` when the credential does not prove one.  Likewise,
    ``assurance_verified_at`` records the age of directory/enterprise evidence;
    a cache hit must not refresh it.
    """

    source: AuthenticationSource
    assurance: IdentityAssurance
    validated_at: datetime
    authenticated_at: datetime | None = None
    assurance_verified_at: datetime | None = None
    provider: str | None = None
    external_identity_id: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if type(self.source) is not AuthenticationSource or type(self.assurance) is not IdentityAssurance:
            raise PrincipalBuildError(PrincipalErrorCode.AUTHENTICATION_INVALID)
        if not _aware(self.validated_at):
            raise PrincipalBuildError(PrincipalErrorCode.AUTHENTICATION_INVALID)
        for evidence_time in (self.authenticated_at, self.assurance_verified_at):
            if evidence_time is not None and (not _aware(evidence_time) or evidence_time > self.validated_at):
                raise PrincipalBuildError(PrincipalErrorCode.AUTHENTICATION_INVALID)

        if self.source in {AuthenticationSource.WEB_SESSION, AuthenticationSource.SDK_API_TOKEN}:
            if self.assurance is not IdentityAssurance.AUTHENTICATED or self.provider is not None or self.external_identity_id is not None or self.assurance_verified_at is not None:
                raise PrincipalBuildError(PrincipalErrorCode.ASSURANCE_INVALID)
            return

        if self.source is AuthenticationSource.ENTERPRISE_IDENTITY:
            if (
                self.assurance not in {IdentityAssurance.DIRECTORY_VERIFIED, IdentityAssurance.ENTERPRISE_VERIFIED}
                or not _valid_text(self.provider, max_length=_MAX_PROVIDER)
                or not _valid_text(self.external_identity_id, max_length=_MAX_PLATFORM_ID)
                or self.assurance_verified_at is None
            ):
                raise PrincipalBuildError(PrincipalErrorCode.ASSURANCE_INVALID)
            return

        raise PrincipalBuildError(PrincipalErrorCode.AUTHENTICATION_INVALID)


@dataclass(frozen=True, slots=True)
class EnterpriseSubject:
    """Minimal verified enterprise business subject; never a provider token."""

    subject_type: str
    subject: str = field(repr=False)
    issuer: str
    issuer_tenant: str = field(repr=False)
    verified_at: datetime

    def __post_init__(self) -> None:
        if (
            not _valid_text(self.subject_type, max_length=_MAX_SUBJECT_TYPE)
            or self.subject_type not in _ENTERPRISE_SUBJECT_TYPES
            or not _valid_text(self.subject, max_length=_MAX_SUBJECT)
            or not _valid_text(self.issuer, max_length=_MAX_ISSUER)
            or not _valid_text(self.issuer_tenant, max_length=_MAX_ISSUER_TENANT)
            or not _aware(self.verified_at)
        ):
            raise PrincipalBuildError(PrincipalErrorCode.INPUT_INVALID)


@dataclass(frozen=True, slots=True)
class AuthenticatedActor:
    """Authentication adapter output before a tenant membership is selected."""

    platform_user_id: str = field(repr=False)
    display_name: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        if not _valid_text(self.platform_user_id, max_length=_MAX_PLATFORM_ID) or (self.display_name != "" and not _valid_text(self.display_name, max_length=_MAX_DISPLAY_NAME)):
            raise PrincipalBuildError(PrincipalErrorCode.INPUT_INVALID)


@dataclass(frozen=True, slots=True)
class TenantMembershipEvidence:
    """Server-loaded live membership coordinates; role is deliberately absent."""

    platform_user_id: str = field(repr=False)
    tenant_id: str

    def __post_init__(self) -> None:
        if not _valid_text(self.platform_user_id, max_length=_MAX_PLATFORM_ID):
            raise PrincipalBuildError(PrincipalErrorCode.INPUT_INVALID)
        if not _valid_text(self.tenant_id, max_length=_MAX_PLATFORM_ID):
            raise PrincipalBuildError(PrincipalErrorCode.TENANT_REQUIRED)


@dataclass(frozen=True, slots=True)
class VerifiedEnterpriseSubjectEvidence:
    """I5-facing evidence binding a subject to platform user and tenant."""

    platform_user_id: str = field(repr=False)
    tenant_id: str
    enterprise_subject: EnterpriseSubject = field(repr=False)

    def __post_init__(self) -> None:
        if (
            not _valid_text(self.platform_user_id, max_length=_MAX_PLATFORM_ID)
            or not _valid_text(self.tenant_id, max_length=_MAX_PLATFORM_ID)
            or not isinstance(self.enterprise_subject, EnterpriseSubject)
        ):
            raise PrincipalBuildError(PrincipalErrorCode.INPUT_INVALID)


@dataclass(frozen=True, slots=True, init=False)
class Principal:
    """Canonical tenant-bound platform identity used by MultiRAG runtimes."""

    platform_user_id: str = field(repr=False)
    tenant_id: str
    authentication: AuthenticationContext
    enterprise_subject: EnterpriseSubject | None = field(default=None, repr=False)
    display_name: str = field(default="", repr=False)

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        """Reject direct construction; callers must present builder evidence."""

        raise PrincipalBuildError(PrincipalErrorCode.CONTEXT_MISSING)

    def _validate(self) -> None:
        actor = AuthenticatedActor(
            platform_user_id=self.platform_user_id,
            display_name=self.display_name,
        )
        del actor
        if not _valid_text(self.tenant_id, max_length=_MAX_PLATFORM_ID):
            raise PrincipalBuildError(PrincipalErrorCode.TENANT_REQUIRED)
        if not isinstance(self.authentication, AuthenticationContext):
            raise PrincipalBuildError(PrincipalErrorCode.AUTHENTICATION_INVALID)
        if self.enterprise_subject is not None and not isinstance(self.enterprise_subject, EnterpriseSubject):
            raise PrincipalBuildError(PrincipalErrorCode.INPUT_INVALID)
        if self.authentication.assurance is IdentityAssurance.ENTERPRISE_VERIFIED:
            if self.enterprise_subject is None:
                raise PrincipalBuildError(PrincipalErrorCode.ASSURANCE_INVALID)
        elif self.enterprise_subject is not None:
            raise PrincipalBuildError(PrincipalErrorCode.ASSURANCE_INVALID)

    @property
    def id(self) -> str:
        """Compatibility alias for existing route consumers."""

        return self.platform_user_id

    @property
    def nickname(self) -> str:
        """Compatibility alias; new code should use ``display_name``."""

        return self.display_name


def _new_principal(
    *,
    platform_user_id: str,
    tenant_id: str,
    authentication: AuthenticationContext,
    enterprise_subject: EnterpriseSubject | None,
    display_name: str,
) -> Principal:
    principal = object.__new__(Principal)
    object.__setattr__(principal, "platform_user_id", platform_user_id)
    object.__setattr__(principal, "tenant_id", tenant_id)
    object.__setattr__(principal, "authentication", authentication)
    object.__setattr__(principal, "enterprise_subject", enterprise_subject)
    object.__setattr__(principal, "display_name", display_name)
    principal._validate()
    return principal


def _assemble_principal(
    *,
    actor: AuthenticatedActor,
    membership: TenantMembershipEvidence,
    authentication: AuthenticationContext,
    enterprise_subject_evidence: VerifiedEnterpriseSubjectEvidence | None = None,
) -> Principal:
    if actor.platform_user_id != membership.platform_user_id:
        raise PrincipalBuildError(PrincipalErrorCode.CONTEXT_CONFLICT)

    enterprise_subject: EnterpriseSubject | None = None
    if enterprise_subject_evidence is not None:
        if enterprise_subject_evidence.platform_user_id != actor.platform_user_id or enterprise_subject_evidence.tenant_id != membership.tenant_id:
            raise PrincipalBuildError(PrincipalErrorCode.CONTEXT_CONFLICT)
        enterprise_subject = enterprise_subject_evidence.enterprise_subject
        if authentication.assurance is not IdentityAssurance.ENTERPRISE_VERIFIED or authentication.assurance_verified_at != enterprise_subject.verified_at:
            raise PrincipalBuildError(PrincipalErrorCode.ASSURANCE_INVALID)
    elif authentication.assurance is IdentityAssurance.ENTERPRISE_VERIFIED:
        raise PrincipalBuildError(PrincipalErrorCode.ASSURANCE_INVALID)

    return _new_principal(
        platform_user_id=actor.platform_user_id,
        tenant_id=membership.tenant_id,
        authentication=authentication,
        enterprise_subject=enterprise_subject,
        display_name=actor.display_name,
    )


def build_principal_from_authenticated_actor(
    *,
    actor: AuthenticatedActor,
    membership: TenantMembershipEvidence,
    authentication: AuthenticationContext,
) -> Principal:
    """Build the legacy Web/API owner-context Principal.

    Enterprise credentials must use ``build_principal_from_resolved_identity``
    so an arbitrary actor cannot be paired with an unrelated provider identity.
    """

    if authentication.source not in {AuthenticationSource.WEB_SESSION, AuthenticationSource.SDK_API_TOKEN} or authentication.assurance is not IdentityAssurance.AUTHENTICATED:
        raise PrincipalBuildError(PrincipalErrorCode.AUTHENTICATION_INVALID)
    return _assemble_principal(
        actor=actor,
        membership=membership,
        authentication=authentication,
    )


def build_principal_from_resolved_identity(
    *,
    result: IdentityResolutionResult,
    authentication: AuthenticationContext,
    enterprise_subject_evidence: VerifiedEnterpriseSubjectEvidence | None = None,
) -> Principal:
    """Promote only an I3 RESOLVED snapshot into a tenant-bound Principal."""

    if result.status is not IdentityResolutionStatus.RESOLVED:
        raise PrincipalBuildError(PrincipalErrorCode.IDENTITY_INACTIVE)
    if result.error_code is not None or result.provisioning_action is not None or result.provider_verification_required:
        raise PrincipalBuildError(PrincipalErrorCode.CONTEXT_CONFLICT)
    identity = result.identity
    membership = result.membership
    if identity is None or membership is None:
        raise PrincipalBuildError(PrincipalErrorCode.CONTEXT_MISSING)
    if identity.state != "active":
        raise PrincipalBuildError(PrincipalErrorCode.IDENTITY_INACTIVE)
    identity_verified_at = _require_aware_time(identity.verified_at)
    if identity_verified_at > authentication.validated_at:
        raise PrincipalBuildError(PrincipalErrorCode.ASSURANCE_INVALID)
    if identity.user_id != membership.user_id or identity.tenant_id != membership.tenant_id:
        raise PrincipalBuildError(PrincipalErrorCode.CONTEXT_CONFLICT)
    if membership.role not in {"owner", "admin", "normal"}:
        raise PrincipalBuildError(PrincipalErrorCode.MEMBERSHIP_INACTIVE)
    if authentication.source is not AuthenticationSource.ENTERPRISE_IDENTITY or authentication.provider != identity.provider or authentication.external_identity_id != identity.id:
        raise PrincipalBuildError(PrincipalErrorCode.AUTHENTICATION_INVALID)
    if authentication.assurance is IdentityAssurance.DIRECTORY_VERIFIED and authentication.assurance_verified_at != identity_verified_at:
        raise PrincipalBuildError(PrincipalErrorCode.ASSURANCE_INVALID)

    display_name = next(
        (value for key, value in identity.attributes if key == "display_name" and value is not None),
        "",
    )
    return _assemble_principal(
        actor=AuthenticatedActor(
            platform_user_id=identity.user_id,
            display_name=display_name,
        ),
        membership=TenantMembershipEvidence(
            platform_user_id=membership.user_id,
            tenant_id=membership.tenant_id,
        ),
        authentication=authentication,
        enterprise_subject_evidence=enterprise_subject_evidence,
    )


__all__ = [
    "AuthenticatedActor",
    "AuthenticationContext",
    "AuthenticationSource",
    "EnterpriseSubject",
    "IdentityAssurance",
    "Principal",
    "PrincipalBuildError",
    "PrincipalErrorCode",
    "TenantMembershipEvidence",
    "VerifiedEnterpriseSubjectEvidence",
    "build_principal_from_authenticated_actor",
    "build_principal_from_resolved_identity",
]
