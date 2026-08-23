"""Validation-gated orchestration for enterprise-subject resolution."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from api.identity.enterprise_subjects.contracts import (
    EnterpriseSubjectAuthority,
    EnterpriseSubjectErrorCode,
    EnterpriseSubjectIssuerTenantSource,
    EnterpriseSubjectProofSource,
    EnterpriseSubjectRepository,
    EnterpriseSubjectRepositoryError,
    EnterpriseSubjectResolution,
    EnterpriseSubjectResolutionStatus,
    EnterpriseSubjectResolver,
    EnterpriseSubjectServiceResult,
    EnterpriseSubjectSlot,
    VerifiedEnterpriseSubjectCommand,
    valid_enterprise_subject_authority,
    valid_enterprise_subject_resolution,
)
from api.identity.principal import EnterpriseSubject, VerifiedEnterpriseSubjectEvidence
from api.identity.providers.contracts import ProviderDirectoryStatus, ProviderIdentity


class EnterpriseSubjectService:
    """Resolve, validate, persist, and read back one enterprise subject.

    The resolver is allowed to report only the closed five-state contract.  A
    resolved value is not promoted to Principal evidence until the repository
    returns a matching active readback.
    """

    def __init__(
        self,
        resolver: EnterpriseSubjectResolver,
        repository: EnterpriseSubjectRepository,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if not isinstance(resolver, EnterpriseSubjectResolver) or not isinstance(repository, EnterpriseSubjectRepository):
            raise ValueError("enterprise subject service dependency is invalid")
        try:
            authority = resolver.authority
        except Exception:
            raise ValueError("enterprise subject resolver authority is invalid") from None
        if not valid_enterprise_subject_authority(authority) or not callable(now):
            raise ValueError("enterprise subject service configuration is invalid")
        self._resolver = resolver
        self._repository = repository
        self._authority = authority
        self._now = now

    async def resolve(
        self,
        *,
        platform_user_id: str,
        tenant_id: str,
        provider_identity: ProviderIdentity,
    ) -> EnterpriseSubjectServiceResult:
        if not _valid_text(platform_user_id, max_length=32) or not _valid_text(tenant_id, max_length=32):
            return _fatal(EnterpriseSubjectErrorCode.INPUT_INVALID)
        if not _valid_provider_identity(provider_identity):
            return _fatal(EnterpriseSubjectErrorCode.PROOF_INVALID)
        if provider_identity.provider != self._authority.provider:
            return _fatal(EnterpriseSubjectErrorCode.AUTHORITY_INVALID)

        try:
            provider_now = self._now()
        except Exception:
            return _fatal(EnterpriseSubjectErrorCode.PROOF_INVALID)
        if not _valid_proof_time(provider_identity.verified_at, now=provider_now):
            return _fatal(EnterpriseSubjectErrorCode.PROOF_INVALID)

        slot = _slot_for(self._authority, provider_identity)
        if slot is None:
            return _fatal(EnterpriseSubjectErrorCode.AUTHORITY_INVALID)

        try:
            resolution = await self._resolver.resolve(provider_identity)
        except Exception:
            return _fatal(EnterpriseSubjectErrorCode.RESOLVER_FAILED)
        try:
            resolution_now = self._now()
        except Exception:
            return _fatal(EnterpriseSubjectErrorCode.PROOF_INVALID)
        if not _valid_resolver_result(
            resolution,
            authority=self._authority,
            slot=slot,
            provider_identity=provider_identity,
            now=resolution_now,
        ):
            return _fatal(EnterpriseSubjectErrorCode.RESOLUTION_INVALID)
        if resolution.status is EnterpriseSubjectResolutionStatus.UNAVAILABLE:
            return _normal(resolution.status)

        try:
            command = VerifiedEnterpriseSubjectCommand(
                platform_user_id=platform_user_id,
                tenant_id=tenant_id,
                slot=slot,
                resolution=resolution,
            )
            persisted = await self._repository.persist_resolution(command)
        except EnterpriseSubjectRepositoryError as exc:
            return _fatal(exc.code)
        except Exception:
            return _fatal(EnterpriseSubjectErrorCode.REPOSITORY_UNAVAILABLE)

        try:
            persistence_now = self._now()
        except Exception:
            return _fatal(EnterpriseSubjectErrorCode.PROOF_INVALID)
        if not _valid_persistence_result(
            persisted,
            command=command,
            now=persistence_now,
        ):
            return _fatal(EnterpriseSubjectErrorCode.PERSISTENCE_INVALID)
        if resolution.status is not EnterpriseSubjectResolutionStatus.RESOLVED:
            return _normal(resolution.status)
        if persisted.status is not EnterpriseSubjectResolutionStatus.RESOLVED:
            return _normal(persisted.status)

        assert persisted.subject is not None
        return EnterpriseSubjectServiceResult(
            status=EnterpriseSubjectResolutionStatus.RESOLVED,
            evidence=VerifiedEnterpriseSubjectEvidence(
                platform_user_id=platform_user_id,
                tenant_id=tenant_id,
                enterprise_subject=persisted.subject,
            ),
        )


def _slot_for(
    authority: EnterpriseSubjectAuthority,
    provider_identity: ProviderIdentity,
) -> EnterpriseSubjectSlot | None:
    issuer_tenant: str | None
    if authority.issuer_tenant_source is EnterpriseSubjectIssuerTenantSource.PROVIDER_IDENTITY:
        issuer_tenant = provider_identity.provider_tenant_key
    else:
        issuer_tenant = authority.fixed_issuer_tenant
    if issuer_tenant is None:
        return None
    try:
        return EnterpriseSubjectSlot(
            subject_type=authority.subject_type,
            issuer=authority.issuer,
            issuer_tenant=issuer_tenant,
        )
    except (TypeError, ValueError):
        return None


def _valid_resolver_result(
    resolution: object,
    *,
    authority: EnterpriseSubjectAuthority,
    slot: EnterpriseSubjectSlot,
    provider_identity: ProviderIdentity,
    now: datetime,
) -> bool:
    if not valid_enterprise_subject_resolution(resolution):
        return False
    assert isinstance(resolution, EnterpriseSubjectResolution)
    if resolution.status is EnterpriseSubjectResolutionStatus.UNAVAILABLE:
        return True
    assert resolution.verified_at is not None
    if authority.proof_source is EnterpriseSubjectProofSource.PROVIDER_IDENTITY:
        if resolution.verified_at != provider_identity.verified_at:
            return False
    elif not _valid_proof_time(resolution.verified_at, now=now):
        return False
    return resolution.subject is None or _subject_matches_slot(resolution.subject, slot)


def _valid_persistence_result(
    persisted: object,
    *,
    command: VerifiedEnterpriseSubjectCommand,
    now: datetime,
) -> bool:
    if not valid_enterprise_subject_resolution(persisted):
        return False
    assert isinstance(persisted, EnterpriseSubjectResolution)
    source = command.resolution
    if source.status is not EnterpriseSubjectResolutionStatus.RESOLVED:
        return persisted == source
    if persisted.status is EnterpriseSubjectResolutionStatus.UNAVAILABLE:
        return False
    if persisted.status is not EnterpriseSubjectResolutionStatus.RESOLVED:
        if persisted.status not in {
            EnterpriseSubjectResolutionStatus.AMBIGUOUS,
            EnterpriseSubjectResolutionStatus.INACTIVE,
        }:
            return False
        return (
            persisted.subject is None
            and persisted.verified_at is not None
            and source.verified_at is not None
            and persisted.verified_at >= source.verified_at
            and _valid_proof_time(persisted.verified_at, now=now)
        )

    if source.subject is None or persisted.subject is None:
        return False
    if not _subject_matches_slot(persisted.subject, command.slot):
        return False
    if persisted.subject.subject != source.subject.subject:
        return False
    if persisted.verified_at is None or source.verified_at is None or persisted.verified_at < source.verified_at:
        return False
    return _valid_proof_time(persisted.verified_at, now=now)


def _valid_provider_identity(value: object) -> bool:
    if not isinstance(value, ProviderIdentity):
        return False
    try:
        return (
            _valid_text(value.provider, max_length=64)
            and _valid_text(value.provider_tenant_key, max_length=255)
            and _valid_text(value.provider_account_id, max_length=32)
            and _valid_text(value.provider_user_id, max_length=255)
            and _aware(value.verified_at)
            and isinstance(value.provider_status, ProviderDirectoryStatus)
        )
    except AttributeError:
        return False


def _subject_matches_slot(subject: EnterpriseSubject, slot: EnterpriseSubjectSlot) -> bool:
    try:
        return subject.subject_type == slot.subject_type and subject.issuer == slot.issuer and subject.issuer_tenant == slot.issuer_tenant
    except AttributeError:
        return False


def _valid_proof_time(value: object, *, now: datetime) -> bool:
    if not isinstance(value, datetime) or not _aware(value) or not _aware(now):
        return False
    return value <= now


def _normal(status: EnterpriseSubjectResolutionStatus) -> EnterpriseSubjectServiceResult:
    return EnterpriseSubjectServiceResult(status=status)


def _fatal(code: EnterpriseSubjectErrorCode) -> EnterpriseSubjectServiceResult:
    return EnterpriseSubjectServiceResult(
        status=EnterpriseSubjectResolutionStatus.UNAVAILABLE,
        error_code=code,
        fatal=True,
    )


def _valid_text(value: object, *, max_length: int) -> bool:
    return type(value) is str and value == value.strip() and bool(value) and len(value) <= max_length


def _aware(value: object) -> bool:
    return isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None


__all__ = ["EnterpriseSubjectService"]
