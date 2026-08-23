"""Framework-neutral contracts for authoritative enterprise subjects."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable

from api.identity.principal import EnterpriseSubject, VerifiedEnterpriseSubjectEvidence
from api.identity.providers.contracts import ProviderIdentity

_MAX_PLATFORM_ID = 32
_MAX_PROVIDER = 64
_MAX_SUBJECT_TYPE = 64
_MAX_SUBJECT = 255
_MAX_ISSUER = 128
_MAX_ISSUER_TENANT = 255
_MAX_SOURCE_REVISION = 255
_SUBJECT_TYPES = frozenset({"employee_no", "talent_id", "workcode"})


class EnterpriseSubjectResolutionStatus(StrEnum):
    """Closed outcomes returned by every enterprise-subject resolver."""

    RESOLVED = "resolved"
    NOT_FOUND = "not_found"
    AMBIGUOUS = "ambiguous"
    UNAVAILABLE = "unavailable"
    INACTIVE = "inactive"


class EnterpriseSubjectIssuerTenantSource(StrEnum):
    """How a resolver's configured authority obtains its issuer tenant."""

    PROVIDER_IDENTITY = "provider_identity"
    FIXED = "fixed"


class EnterpriseSubjectProofSource(StrEnum):
    """Authority for the proof time carried by a resolver result."""

    PROVIDER_IDENTITY = "provider_identity"
    RESOLVER = "resolver"


class EnterpriseSubjectErrorCode(StrEnum):
    """Stable, non-identifying failures at the I5 composition boundary."""

    INPUT_INVALID = "ENTERPRISE_SUBJECT_INPUT_INVALID"
    PROOF_INVALID = "ENTERPRISE_SUBJECT_PROOF_INVALID"
    AUTHORITY_INVALID = "ENTERPRISE_SUBJECT_AUTHORITY_INVALID"
    RESOLVER_FAILED = "ENTERPRISE_SUBJECT_RESOLVER_FAILED"
    RESOLUTION_INVALID = "ENTERPRISE_SUBJECT_RESOLUTION_INVALID"
    REPOSITORY_UNAVAILABLE = "ENTERPRISE_SUBJECT_REPOSITORY_UNAVAILABLE"
    PERSISTENCE_INVALID = "ENTERPRISE_SUBJECT_PERSISTENCE_INVALID"


@dataclass(frozen=True, slots=True)
class EnterpriseSubjectSlot:
    """One tenant-local resolver slot, independent of its current value."""

    subject_type: str
    issuer: str
    issuer_tenant: str = field(repr=False)

    def __post_init__(self) -> None:
        if not valid_enterprise_subject_slot(self):
            raise ValueError("enterprise subject slot is invalid")


@dataclass(frozen=True, slots=True)
class EnterpriseSubjectAuthority:
    """Server-configured namespace and proof rules for one resolver."""

    provider: str
    subject_type: str
    issuer: str
    issuer_tenant_source: EnterpriseSubjectIssuerTenantSource
    proof_source: EnterpriseSubjectProofSource
    fixed_issuer_tenant: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not valid_enterprise_subject_authority(self):
            raise ValueError("enterprise subject authority is invalid")


@dataclass(frozen=True, slots=True)
class EnterpriseSubjectResolution:
    """Resolver or persistence result with a closed five-state shape."""

    status: EnterpriseSubjectResolutionStatus
    subject: EnterpriseSubject | None = field(default=None, repr=False)
    verified_at: datetime | None = field(default=None, repr=False)
    source_revision: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not valid_enterprise_subject_resolution(self):
            raise ValueError("enterprise subject resolution is invalid")


@dataclass(frozen=True, slots=True)
class VerifiedEnterpriseSubjectCommand:
    """Validated resolver outcome ready for transactional persistence."""

    platform_user_id: str = field(repr=False)
    tenant_id: str = field(repr=False)
    slot: EnterpriseSubjectSlot = field(repr=False)
    resolution: EnterpriseSubjectResolution = field(repr=False)

    def __post_init__(self) -> None:
        if (
            not _valid_text(self.platform_user_id, max_length=_MAX_PLATFORM_ID)
            or not _valid_text(self.tenant_id, max_length=_MAX_PLATFORM_ID)
            or not valid_enterprise_subject_slot(self.slot)
            or not valid_enterprise_subject_resolution(self.resolution)
            or (self.resolution.subject is not None and not _subject_matches_slot(self.resolution.subject, self.slot))
        ):
            raise ValueError("enterprise subject persistence command is invalid")


@dataclass(frozen=True, slots=True)
class EnterpriseSubjectServiceResult:
    """Safe I5 result consumed by Principal-building adapters.

    ``fatal`` separates an infrastructure or invariant failure from a normal
    resolver ``UNAVAILABLE`` outcome.  Only a non-fatal ``RESOLVED`` result may
    carry evidence, and that evidence must have been reconstructed from the
    repository's readback.
    """

    status: EnterpriseSubjectResolutionStatus
    evidence: VerifiedEnterpriseSubjectEvidence | None = field(default=None, repr=False)
    error_code: EnterpriseSubjectErrorCode | None = None
    fatal: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.status, EnterpriseSubjectResolutionStatus) or type(self.fatal) is not bool:
            raise ValueError("enterprise subject service result is invalid")
        if self.status is EnterpriseSubjectResolutionStatus.RESOLVED:
            if not isinstance(self.evidence, VerifiedEnterpriseSubjectEvidence) or self.error_code is not None or self.fatal:
                raise ValueError("enterprise subject service result is invalid")
            return
        if self.evidence is not None:
            raise ValueError("enterprise subject service result is invalid")
        if self.fatal:
            if self.status is not EnterpriseSubjectResolutionStatus.UNAVAILABLE or not isinstance(self.error_code, EnterpriseSubjectErrorCode):
                raise ValueError("enterprise subject service result is invalid")
        elif self.error_code is not None:
            raise ValueError("enterprise subject service result is invalid")


class EnterpriseSubjectRepositoryError(RuntimeError):
    """Sanitized persistence failure without subject or database material."""

    def __init__(self, code: EnterpriseSubjectErrorCode) -> None:
        if not isinstance(code, EnterpriseSubjectErrorCode) or code not in (
            EnterpriseSubjectErrorCode.REPOSITORY_UNAVAILABLE,
            EnterpriseSubjectErrorCode.PERSISTENCE_INVALID,
        ):
            code = EnterpriseSubjectErrorCode.REPOSITORY_UNAVAILABLE
        self.code = code
        super().__init__(code.value)


@runtime_checkable
class EnterpriseSubjectResolver(Protocol):
    """Pluggable Feishu/OA/HR resolver without vendor logic in the service."""

    @property
    def authority(self) -> EnterpriseSubjectAuthority: ...

    async def resolve(
        self,
        provider_identity: ProviderIdentity,
    ) -> EnterpriseSubjectResolution: ...


@runtime_checkable
class EnterpriseSubjectRepository(Protocol):
    """Narrow transactional port for applying one verified resolver outcome."""

    async def persist_resolution(
        self,
        command: VerifiedEnterpriseSubjectCommand,
    ) -> EnterpriseSubjectResolution: ...


@runtime_checkable
class EnterpriseSubjectEvidenceService(Protocol):
    """Channel-facing I5 seam; no ORM or provider credentials cross it."""

    async def resolve(
        self,
        *,
        platform_user_id: str,
        tenant_id: str,
        provider_identity: ProviderIdentity,
    ) -> EnterpriseSubjectServiceResult: ...


def valid_enterprise_subject_authority(value: object) -> bool:
    if not isinstance(value, EnterpriseSubjectAuthority):
        return False
    try:
        valid_coordinates = (
            _valid_text(value.provider, max_length=_MAX_PROVIDER)
            and value.subject_type in _SUBJECT_TYPES
            and _valid_text(value.subject_type, max_length=_MAX_SUBJECT_TYPE)
            and _valid_text(value.issuer, max_length=_MAX_ISSUER)
            and isinstance(value.issuer_tenant_source, EnterpriseSubjectIssuerTenantSource)
            and isinstance(value.proof_source, EnterpriseSubjectProofSource)
        )
    except (AttributeError, TypeError):
        return False
    if not valid_coordinates:
        return False
    try:
        if value.issuer_tenant_source is EnterpriseSubjectIssuerTenantSource.PROVIDER_IDENTITY:
            return value.fixed_issuer_tenant is None
        return _valid_text(value.fixed_issuer_tenant, max_length=_MAX_ISSUER_TENANT)
    except (AttributeError, TypeError):
        return False


def valid_enterprise_subject_slot(value: object) -> bool:
    if not isinstance(value, EnterpriseSubjectSlot):
        return False
    try:
        return (
            value.subject_type in _SUBJECT_TYPES
            and _valid_text(value.subject_type, max_length=_MAX_SUBJECT_TYPE)
            and _valid_text(value.issuer, max_length=_MAX_ISSUER)
            and _valid_text(value.issuer_tenant, max_length=_MAX_ISSUER_TENANT)
        )
    except (AttributeError, TypeError):
        return False


def valid_enterprise_subject_resolution(value: object) -> bool:
    if not isinstance(value, EnterpriseSubjectResolution):
        return False
    try:
        status = value.status
        subject = value.subject
        verified_at = value.verified_at
        source_revision = value.source_revision
    except AttributeError:
        return False
    if not isinstance(status, EnterpriseSubjectResolutionStatus):
        return False
    if source_revision is not None and not _valid_text(source_revision, max_length=_MAX_SOURCE_REVISION):
        return False
    if status is EnterpriseSubjectResolutionStatus.RESOLVED:
        return isinstance(subject, EnterpriseSubject) and _valid_subject(subject) and _aware(verified_at) and subject.verified_at == verified_at
    if subject is not None:
        return False
    if status is EnterpriseSubjectResolutionStatus.UNAVAILABLE:
        return verified_at is None and source_revision is None
    return _aware(verified_at)


def _subject_matches_slot(
    subject: EnterpriseSubject,
    slot: EnterpriseSubjectSlot,
) -> bool:
    try:
        return subject.subject_type == slot.subject_type and subject.issuer == slot.issuer and subject.issuer_tenant == slot.issuer_tenant
    except AttributeError:
        return False


def _valid_subject(value: object) -> bool:
    if not isinstance(value, EnterpriseSubject):
        return False
    try:
        return (
            value.subject_type in _SUBJECT_TYPES
            and _valid_text(value.subject_type, max_length=_MAX_SUBJECT_TYPE)
            and _valid_text(value.subject, max_length=_MAX_SUBJECT)
            and _valid_text(value.issuer, max_length=_MAX_ISSUER)
            and _valid_text(value.issuer_tenant, max_length=_MAX_ISSUER_TENANT)
            and _aware(value.verified_at)
        )
    except (AttributeError, TypeError):
        return False


def _valid_text(value: object, *, max_length: int) -> bool:
    return type(value) is str and value == value.strip() and bool(value) and len(value) <= max_length


def _aware(value: object) -> bool:
    return isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None


__all__ = [
    "EnterpriseSubjectAuthority",
    "EnterpriseSubjectErrorCode",
    "EnterpriseSubjectEvidenceService",
    "EnterpriseSubjectIssuerTenantSource",
    "EnterpriseSubjectProofSource",
    "EnterpriseSubjectRepository",
    "EnterpriseSubjectRepositoryError",
    "EnterpriseSubjectResolution",
    "EnterpriseSubjectResolutionStatus",
    "EnterpriseSubjectResolver",
    "EnterpriseSubjectServiceResult",
    "EnterpriseSubjectSlot",
    "VerifiedEnterpriseSubjectCommand",
    "valid_enterprise_subject_authority",
    "valid_enterprise_subject_resolution",
    "valid_enterprise_subject_slot",
]
