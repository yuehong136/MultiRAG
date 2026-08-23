"""Authoritative enterprise-subject resolution and persistence boundary."""

from api.identity.enterprise_subjects.contracts import (
    EnterpriseSubjectAuthority,
    EnterpriseSubjectErrorCode,
    EnterpriseSubjectEvidenceService,
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
)
from api.identity.enterprise_subjects.feishu_employee_number import FeishuEmployeeNumberResolver
from api.identity.enterprise_subjects.service import EnterpriseSubjectService

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
    "EnterpriseSubjectService",
    "EnterpriseSubjectServiceResult",
    "EnterpriseSubjectSlot",
    "FeishuEmployeeNumberResolver",
    "VerifiedEnterpriseSubjectCommand",
]
