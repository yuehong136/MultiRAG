"""Closed unit tests for framework-neutral EIM-I5 enterprise subjects."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, fields, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from api.identity.enterprise_subjects import (
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
    EnterpriseSubjectService,
    EnterpriseSubjectServiceResult,
    EnterpriseSubjectSlot,
    FeishuEmployeeNumberResolver,
    VerifiedEnterpriseSubjectCommand,
)
from api.identity.principal import EnterpriseSubject
from api.identity.providers.contracts import ProviderDirectoryStatus, ProviderIdentity

_NOW = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
_SECRET_MARKERS = {
    "tenant-secret",
    "account-secret",
    "provider-user-secret",
    "platform-user-secret",
    "employee-secret",
    "revision-secret",
}


def _provider_identity(**changes: Any) -> ProviderIdentity:
    return replace(
        ProviderIdentity(
            provider="feishu",
            provider_tenant_key="tenant-secret",
            provider_account_id="account-secret",
            provider_user_id="provider-user-secret",
            verified_at=_NOW,
            employee_no="00AbC-employee-secret",
            provider_status=ProviderDirectoryStatus.ACTIVE,
        ),
        **changes,
    )


def _subject(**changes: Any) -> EnterpriseSubject:
    return replace(
        EnterpriseSubject(
            subject_type="employee_no",
            subject="00AbC-employee-secret",
            issuer="feishu_contact",
            issuer_tenant="tenant-secret",
            verified_at=_NOW,
        ),
        **changes,
    )


def _resolved(**changes: Any) -> EnterpriseSubjectResolution:
    return replace(
        EnterpriseSubjectResolution(
            status=EnterpriseSubjectResolutionStatus.RESOLVED,
            subject=_subject(),
            verified_at=_NOW,
            source_revision="revision-secret",
        ),
        **changes,
    )


class _Resolver:
    def __init__(
        self,
        result: EnterpriseSubjectResolution | BaseException,
        *,
        authority: EnterpriseSubjectAuthority | None = None,
    ) -> None:
        self._result = result
        self._authority = authority or FeishuEmployeeNumberResolver().authority

    @property
    def authority(self) -> EnterpriseSubjectAuthority:
        return self._authority

    async def resolve(self, provider_identity: ProviderIdentity) -> EnterpriseSubjectResolution:
        del provider_identity
        if isinstance(self._result, BaseException):
            raise self._result
        return self._result


class _Repository:
    def __init__(
        self,
        result: EnterpriseSubjectResolution | BaseException,
    ) -> None:
        self._result = result
        self.commands: list[VerifiedEnterpriseSubjectCommand] = []

    async def persist_resolution(
        self,
        command: VerifiedEnterpriseSubjectCommand,
    ) -> EnterpriseSubjectResolution:
        self.commands.append(command)
        if isinstance(self._result, BaseException):
            raise self._result
        return self._result


def _service(
    resolver_result: EnterpriseSubjectResolution | BaseException,
    repository_result: EnterpriseSubjectResolution | BaseException,
    *,
    authority: EnterpriseSubjectAuthority | None = None,
) -> tuple[EnterpriseSubjectService, _Repository]:
    resolver = _Resolver(resolver_result, authority=authority)
    repository = _Repository(repository_result)
    return (
        EnterpriseSubjectService(resolver, repository, now=lambda: _NOW),
        repository,
    )


def test_resolution_status_is_the_closed_five_state_contract() -> None:
    assert {status.value for status in EnterpriseSubjectResolutionStatus} == {
        "resolved",
        "not_found",
        "ambiguous",
        "unavailable",
        "inactive",
    }


def test_contracts_are_frozen_slotted_and_redact_identity_material() -> None:
    slot = EnterpriseSubjectSlot(
        subject_type="employee_no",
        issuer="feishu_contact",
        issuer_tenant="tenant-secret",
    )
    command = VerifiedEnterpriseSubjectCommand(
        platform_user_id="platform-user-secret",
        tenant_id="tenant-secret",
        slot=slot,
        resolution=_resolved(),
    )
    service_result = EnterpriseSubjectServiceResult(
        status=EnterpriseSubjectResolutionStatus.UNAVAILABLE,
    )
    values = (slot, _resolved(), command, service_result)

    for value in values:
        assert value.__dataclass_params__.frozen is True  # type: ignore[union-attr]
        assert hasattr(type(value), "__slots__")
        with pytest.raises(FrozenInstanceError):
            setattr(value, fields(value)[0].name, "changed")
        rendered = repr(value)
        assert all(marker not in rendered for marker in _SECRET_MARKERS)

    assert {item.name for item in fields(VerifiedEnterpriseSubjectCommand)} == {
        "platform_user_id",
        "tenant_id",
        "slot",
        "resolution",
    }


def test_resolver_and_repository_spis_are_runtime_checkable() -> None:
    resolver = FeishuEmployeeNumberResolver()
    repository = _Repository(_resolved())
    service = EnterpriseSubjectService(resolver, repository, now=lambda: _NOW)

    assert isinstance(resolver, EnterpriseSubjectResolver)
    assert isinstance(repository, EnterpriseSubjectRepository)
    assert isinstance(service, EnterpriseSubjectEvidenceService)


@pytest.mark.asyncio
async def test_feishu_resolver_preserves_employee_number_and_contact_proof_exactly() -> None:
    identity = _provider_identity(employee_no="00aBc-09")

    result = await FeishuEmployeeNumberResolver().resolve(identity)

    assert result.status is EnterpriseSubjectResolutionStatus.RESOLVED
    assert result.subject is not None
    assert result.subject.subject_type == "employee_no"
    assert result.subject.subject == "00aBc-09"
    assert result.subject.issuer == "feishu_contact"
    assert result.subject.issuer_tenant == identity.provider_tenant_key
    assert result.subject.verified_at == identity.verified_at
    assert not hasattr(result.subject, "workcode")


@pytest.mark.asyncio
async def test_feishu_resolver_reports_missing_employee_number_without_guessing() -> None:
    identity = _provider_identity(employee_no=None)

    result = await FeishuEmployeeNumberResolver().resolve(identity)

    assert result == EnterpriseSubjectResolution(
        status=EnterpriseSubjectResolutionStatus.NOT_FOUND,
        verified_at=identity.verified_at,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("employee_no", [" employee", "employee ", "", " ", "x" * 256])
async def test_feishu_resolver_rejects_noncanonical_employee_numbers(employee_no: str) -> None:
    result = await FeishuEmployeeNumberResolver().resolve(
        _provider_identity(employee_no=employee_no),
    )

    assert result.status is EnterpriseSubjectResolutionStatus.UNAVAILABLE
    assert result.subject is None


@pytest.mark.asyncio
async def test_service_promotes_only_repository_readback_to_verified_evidence() -> None:
    persisted_subject = _subject()
    persisted = EnterpriseSubjectResolution(
        status=EnterpriseSubjectResolutionStatus.RESOLVED,
        subject=persisted_subject,
        verified_at=_NOW,
        source_revision="persisted-revision",
    )
    service, repository = _service(_resolved(), persisted)

    result = await service.resolve(
        platform_user_id="platform-user-secret",
        tenant_id="tenant-1",
        provider_identity=_provider_identity(),
    )

    assert result.status is EnterpriseSubjectResolutionStatus.RESOLVED
    assert result.evidence is not None
    assert result.evidence.enterprise_subject is persisted_subject
    assert result.evidence.platform_user_id == "platform-user-secret"
    assert result.evidence.tenant_id == "tenant-1"
    assert len(repository.commands) == 1
    assert repository.commands[0].slot == EnterpriseSubjectSlot(
        subject_type="employee_no",
        issuer="feishu_contact",
        issuer_tenant="tenant-secret",
    )


@pytest.mark.asyncio
async def test_service_persists_negative_outcomes_but_unavailable_is_zero_write() -> None:
    not_found = EnterpriseSubjectResolution(
        status=EnterpriseSubjectResolutionStatus.NOT_FOUND,
        verified_at=_NOW,
    )
    service, repository = _service(
        not_found,
        not_found,
    )
    result = await service.resolve(
        platform_user_id="platform-user-secret",
        tenant_id="tenant-1",
        provider_identity=_provider_identity(),
    )

    assert result.status is EnterpriseSubjectResolutionStatus.NOT_FOUND
    assert result.evidence is None
    assert result.fatal is False
    assert len(repository.commands) == 1

    unavailable_service, unavailable_repository = _service(
        EnterpriseSubjectResolution(
            status=EnterpriseSubjectResolutionStatus.UNAVAILABLE,
        ),
        _resolved(),
    )
    unavailable = await unavailable_service.resolve(
        platform_user_id="platform-user-secret",
        tenant_id="tenant-1",
        provider_identity=_provider_identity(),
    )

    assert unavailable.status is EnterpriseSubjectResolutionStatus.UNAVAILABLE
    assert unavailable.fatal is False
    assert unavailable_repository.commands == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "persisted_status"),
    [
        (EnterpriseSubjectResolutionStatus.AMBIGUOUS, EnterpriseSubjectResolutionStatus.AMBIGUOUS),
        (EnterpriseSubjectResolutionStatus.INACTIVE, EnterpriseSubjectResolutionStatus.INACTIVE),
    ],
)
async def test_service_preserves_rejecting_negative_statuses(
    status: EnterpriseSubjectResolutionStatus,
    persisted_status: EnterpriseSubjectResolutionStatus,
) -> None:
    resolution = EnterpriseSubjectResolution(status=status, verified_at=_NOW)
    service, _ = _service(
        resolution,
        EnterpriseSubjectResolution(status=persisted_status, verified_at=_NOW),
    )

    result = await service.resolve(
        platform_user_id="platform-user-secret",
        tenant_id="tenant-1",
        provider_identity=_provider_identity(),
    )

    assert result.status is status
    assert result.evidence is None
    assert result.fatal is False


@pytest.mark.asyncio
async def test_stale_negative_against_newer_active_mapping_preserves_resolver_outcome() -> None:
    not_found = EnterpriseSubjectResolution(
        status=EnterpriseSubjectResolutionStatus.NOT_FOUND,
        verified_at=_NOW,
    )
    service, repository = _service(
        not_found,
        not_found,
    )

    result = await service.resolve(
        platform_user_id="platform-user-secret",
        tenant_id="tenant-1",
        provider_identity=_provider_identity(),
    )

    assert len(repository.commands) == 1
    assert result.status is EnterpriseSubjectResolutionStatus.NOT_FOUND
    assert result.fatal is False


@pytest.mark.asyncio
async def test_service_rejects_future_or_mismatched_provider_proof() -> None:
    service, repository = _service(_resolved(), _resolved())

    future = await service.resolve(
        platform_user_id="platform-user-secret",
        tenant_id="tenant-1",
        provider_identity=_provider_identity(verified_at=_NOW + timedelta(seconds=1)),
    )

    assert future.fatal is True
    assert future.error_code is EnterpriseSubjectErrorCode.PROOF_INVALID
    assert repository.commands == []

    mismatched = replace(_resolved(), verified_at=_NOW - timedelta(seconds=1), subject=_subject(verified_at=_NOW - timedelta(seconds=1)))
    mismatch_service, mismatch_repository = _service(mismatched, mismatched)
    mismatch = await mismatch_service.resolve(
        platform_user_id="platform-user-secret",
        tenant_id="tenant-1",
        provider_identity=_provider_identity(),
    )

    assert mismatch.fatal is True
    assert mismatch.error_code is EnterpriseSubjectErrorCode.RESOLUTION_INVALID
    assert mismatch_repository.commands == []


@pytest.mark.asyncio
async def test_service_maps_resolver_and_repository_exceptions_to_safe_fatal_results() -> None:
    resolver_service, resolver_repository = _service(
        RuntimeError("employee-secret resolver detail"),
        _resolved(),
    )
    resolver_result = await resolver_service.resolve(
        platform_user_id="platform-user-secret",
        tenant_id="tenant-1",
        provider_identity=_provider_identity(),
    )

    assert resolver_result.fatal is True
    assert resolver_result.error_code is EnterpriseSubjectErrorCode.RESOLVER_FAILED
    assert "employee-secret" not in repr(resolver_result)
    assert resolver_repository.commands == []

    repository_service, _ = _service(
        _resolved(),
        EnterpriseSubjectRepositoryError(EnterpriseSubjectErrorCode.REPOSITORY_UNAVAILABLE),
    )
    repository_result = await repository_service.resolve(
        platform_user_id="platform-user-secret",
        tenant_id="tenant-1",
        provider_identity=_provider_identity(),
    )

    assert repository_result.fatal is True
    assert repository_result.error_code is EnterpriseSubjectErrorCode.REPOSITORY_UNAVAILABLE


@pytest.mark.asyncio
async def test_service_rejects_malformed_or_authority_drifting_results() -> None:
    malformed = object.__new__(EnterpriseSubjectResolution)
    object.__setattr__(malformed, "status", EnterpriseSubjectResolutionStatus.RESOLVED)
    object.__setattr__(malformed, "subject", None)
    object.__setattr__(malformed, "verified_at", _NOW)
    object.__setattr__(malformed, "source_revision", None)
    malformed_service, malformed_repository = _service(malformed, _resolved())

    malformed_result = await malformed_service.resolve(
        platform_user_id="platform-user-secret",
        tenant_id="tenant-1",
        provider_identity=_provider_identity(),
    )

    assert malformed_result.fatal is True
    assert malformed_result.error_code is EnterpriseSubjectErrorCode.RESOLUTION_INVALID
    assert malformed_repository.commands == []

    drifting = _resolved(
        subject=_subject(issuer="oa_vendor"),
    )
    drift_service, drift_repository = _service(drifting, drifting)
    drift_result = await drift_service.resolve(
        platform_user_id="platform-user-secret",
        tenant_id="tenant-1",
        provider_identity=_provider_identity(),
    )

    assert drift_result.fatal is True
    assert drift_result.error_code is EnterpriseSubjectErrorCode.RESOLUTION_INVALID
    assert drift_repository.commands == []


@pytest.mark.asyncio
async def test_fixed_external_authority_uses_its_own_namespace_without_vendor_branches() -> None:
    proof_time = _NOW - timedelta(minutes=30)
    authority = EnterpriseSubjectAuthority(
        provider="feishu",
        subject_type="workcode",
        issuer="configured_hr",
        issuer_tenant_source=EnterpriseSubjectIssuerTenantSource.FIXED,
        proof_source=EnterpriseSubjectProofSource.RESOLVER,
        fixed_issuer_tenant="hr-tenant",
    )
    subject = EnterpriseSubject(
        subject_type="workcode",
        subject="HR-0007",
        issuer="configured_hr",
        issuer_tenant="hr-tenant",
        verified_at=proof_time,
    )
    resolution = EnterpriseSubjectResolution(
        status=EnterpriseSubjectResolutionStatus.RESOLVED,
        subject=subject,
        verified_at=proof_time,
    )
    service, repository = _service(resolution, resolution, authority=authority)

    result = await service.resolve(
        platform_user_id="platform-user-secret",
        tenant_id="tenant-1",
        provider_identity=_provider_identity(),
    )

    assert result.status is EnterpriseSubjectResolutionStatus.RESOLVED
    assert result.evidence is not None
    assert result.evidence.enterprise_subject.subject_type == "workcode"
    assert repository.commands[0].slot.issuer_tenant == "hr-tenant"


@pytest.mark.asyncio
async def test_resolver_owned_proof_is_checked_against_post_resolution_time() -> None:
    resolver_proof = _NOW + timedelta(seconds=1)
    authority = EnterpriseSubjectAuthority(
        provider="feishu",
        subject_type="workcode",
        issuer="configured_hr",
        issuer_tenant_source=EnterpriseSubjectIssuerTenantSource.FIXED,
        proof_source=EnterpriseSubjectProofSource.RESOLVER,
        fixed_issuer_tenant="hr-tenant",
    )
    subject = EnterpriseSubject(
        subject_type="workcode",
        subject="HR-0008",
        issuer="configured_hr",
        issuer_tenant="hr-tenant",
        verified_at=resolver_proof,
    )
    resolution = EnterpriseSubjectResolution(
        status=EnterpriseSubjectResolutionStatus.RESOLVED,
        subject=subject,
        verified_at=resolver_proof,
    )
    clock_values = iter(
        (
            _NOW,
            _NOW + timedelta(seconds=2),
            _NOW + timedelta(seconds=3),
        )
    )
    repository = _Repository(resolution)
    service = EnterpriseSubjectService(
        _Resolver(resolution, authority=authority),
        repository,
        now=lambda: next(clock_values),
    )

    result = await service.resolve(
        platform_user_id="platform-user-secret",
        tenant_id="tenant-1",
        provider_identity=_provider_identity(),
    )

    assert result.status is EnterpriseSubjectResolutionStatus.RESOLVED
    assert result.evidence is not None


def test_resolution_shapes_and_authority_coordinates_are_strict() -> None:
    with pytest.raises(ValueError, match="resolution is invalid"):
        EnterpriseSubjectResolution(
            status=EnterpriseSubjectResolutionStatus.RESOLVED,
            verified_at=_NOW,
        )
    with pytest.raises(ValueError, match="resolution is invalid"):
        EnterpriseSubjectResolution(
            status=EnterpriseSubjectResolutionStatus.UNAVAILABLE,
            verified_at=_NOW,
        )
    with pytest.raises(ValueError, match="slot is invalid"):
        EnterpriseSubjectSlot(
            subject_type="employee_no",
            issuer="feishu_contact",
            issuer_tenant=" tenant-secret",
        )
    with pytest.raises(ValueError, match="authority is invalid"):
        EnterpriseSubjectAuthority(
            provider="feishu",
            subject_type="employee_no",
            issuer="feishu_contact",
            issuer_tenant_source=EnterpriseSubjectIssuerTenantSource.PROVIDER_IDENTITY,
            proof_source=EnterpriseSubjectProofSource.PROVIDER_IDENTITY,
            fixed_issuer_tenant="must-not-be-set",
        )

    malformed_authority = object.__new__(EnterpriseSubjectAuthority)
    object.__setattr__(malformed_authority, "provider", "feishu")
    object.__setattr__(malformed_authority, "subject_type", "employee_no")
    object.__setattr__(malformed_authority, "issuer", "feishu_contact")
    object.__setattr__(
        malformed_authority,
        "issuer_tenant_source",
        EnterpriseSubjectIssuerTenantSource.PROVIDER_IDENTITY,
    )
    object.__setattr__(
        malformed_authority,
        "proof_source",
        EnterpriseSubjectProofSource.PROVIDER_IDENTITY,
    )
    with pytest.raises(ValueError, match="service configuration is invalid"):
        EnterpriseSubjectService(
            _Resolver(_resolved(), authority=malformed_authority),
            _Repository(_resolved()),
        )

    malformed_result = object.__new__(EnterpriseSubjectServiceResult)
    object.__setattr__(
        malformed_result,
        "status",
        EnterpriseSubjectResolutionStatus.RESOLVED,
    )
    object.__setattr__(malformed_result, "evidence", object())
    object.__setattr__(malformed_result, "error_code", None)
    object.__setattr__(malformed_result, "fatal", False)
    with pytest.raises(ValueError, match="service result is invalid"):
        malformed_result.__post_init__()
