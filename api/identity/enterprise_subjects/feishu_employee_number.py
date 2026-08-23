"""Built-in enterprise-subject resolver for Feishu employee numbers."""

from __future__ import annotations

from api.identity.enterprise_subjects.contracts import (
    EnterpriseSubjectAuthority,
    EnterpriseSubjectIssuerTenantSource,
    EnterpriseSubjectProofSource,
    EnterpriseSubjectResolution,
    EnterpriseSubjectResolutionStatus,
)
from api.identity.principal import EnterpriseSubject
from api.identity.providers.contracts import ProviderDirectoryStatus, ProviderIdentity

_AUTHORITY = EnterpriseSubjectAuthority(
    provider="feishu",
    subject_type="employee_no",
    issuer="feishu_contact",
    issuer_tenant_source=EnterpriseSubjectIssuerTenantSource.PROVIDER_IDENTITY,
    proof_source=EnterpriseSubjectProofSource.PROVIDER_IDENTITY,
)


class FeishuEmployeeNumberResolver:
    """Project an exact Contact ``employee_no`` without semantic conversion.

    Leading zeroes, case, and other valid identifier characters are preserved.
    This resolver never assumes that a Feishu employee number is an OA
    ``workcode`` or another vendor-specific identifier.
    """

    @property
    def authority(self) -> EnterpriseSubjectAuthority:
        return _AUTHORITY

    async def resolve(
        self,
        provider_identity: ProviderIdentity,
    ) -> EnterpriseSubjectResolution:
        if not _valid_provider_proof(provider_identity):
            return EnterpriseSubjectResolution(
                status=EnterpriseSubjectResolutionStatus.UNAVAILABLE,
            )
        if provider_identity.provider_status is not ProviderDirectoryStatus.ACTIVE:
            return EnterpriseSubjectResolution(
                status=EnterpriseSubjectResolutionStatus.INACTIVE,
                verified_at=provider_identity.verified_at,
            )
        if provider_identity.employee_no is None:
            return EnterpriseSubjectResolution(
                status=EnterpriseSubjectResolutionStatus.NOT_FOUND,
                verified_at=provider_identity.verified_at,
            )
        if not _valid_text(provider_identity.employee_no, max_length=255):
            return EnterpriseSubjectResolution(
                status=EnterpriseSubjectResolutionStatus.UNAVAILABLE,
            )
        subject = EnterpriseSubject(
            subject_type="employee_no",
            subject=provider_identity.employee_no,
            issuer="feishu_contact",
            issuer_tenant=provider_identity.provider_tenant_key,
            verified_at=provider_identity.verified_at,
        )
        return EnterpriseSubjectResolution(
            status=EnterpriseSubjectResolutionStatus.RESOLVED,
            subject=subject,
            verified_at=provider_identity.verified_at,
        )


def _valid_provider_proof(value: object) -> bool:
    if not isinstance(value, ProviderIdentity):
        return False
    try:
        return (
            value.provider == "feishu"
            and _valid_text(value.provider_tenant_key, max_length=255)
            and _valid_text(value.provider_account_id, max_length=32)
            and _valid_text(value.provider_user_id, max_length=255)
            and (value.employee_no is None or _valid_text(value.employee_no, max_length=255))
            and value.verified_at.tzinfo is not None
            and value.verified_at.utcoffset() is not None
            and isinstance(value.provider_status, ProviderDirectoryStatus)
        )
    except AttributeError:
        return False


def _valid_text(value: object, *, max_length: int) -> bool:
    return type(value) is str and value == value.strip() and bool(value) and len(value) <= max_length


__all__ = ["FeishuEmployeeNumberResolver"]
