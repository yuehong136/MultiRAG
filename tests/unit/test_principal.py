"""EIM-P1 canonical Principal and authentication-evidence contracts."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, fields, replace
from datetime import UTC, datetime, timedelta

import pytest

from api.identity.contracts import (
    ExternalIdentityRecord,
    IdentityErrorCode,
    IdentityResolutionResult,
    IdentityResolutionStatus,
    ProvisioningAction,
    UserMembershipRecord,
)
from api.identity.principal import (
    AuthenticatedActor,
    AuthenticationContext,
    AuthenticationSource,
    EnterpriseSubject,
    IdentityAssurance,
    Principal,
    PrincipalBuildError,
    PrincipalErrorCode,
    TenantMembershipEvidence,
    VerifiedEnterpriseSubjectEvidence,
    VerifiedProviderIdentity,
    build_principal_from_authenticated_actor,
    build_principal_from_resolved_identity,
)
from api.utils.api_utils import Principal as CompatibilityPrincipal

_NOW = datetime(2026, 8, 12, 15, 30, tzinfo=UTC)
_PROOF_TIME = _NOW - timedelta(minutes=5)


def _web_auth() -> AuthenticationContext:
    return AuthenticationContext(
        source=AuthenticationSource.WEB_SESSION,
        assurance=IdentityAssurance.AUTHENTICATED,
        validated_at=_NOW,
    )


def _enterprise_auth(assurance: IdentityAssurance = IdentityAssurance.DIRECTORY_VERIFIED) -> AuthenticationContext:
    return AuthenticationContext(
        source=AuthenticationSource.ENTERPRISE_IDENTITY,
        assurance=assurance,
        validated_at=_NOW,
        assurance_verified_at=_PROOF_TIME,
        provider="feishu",
        external_identity_id="identity-1",
    )


def _resolved() -> IdentityResolutionResult:
    return IdentityResolutionResult(
        status=IdentityResolutionStatus.RESOLVED,
        identity=ExternalIdentityRecord(
            id="identity-1",
            tenant_id="tenant-1",
            user_id="user-1",
            provider="feishu",
            provider_tenant_key="tenant-key-secret",
            subject_type="user_id",
            subject_value="provider-subject-secret",
            state="active",
            verified_at=_PROOF_TIME,
            last_seen_at=_PROOF_TIME,
            identity_revision=3,
            attributes=(("display_name", "Alice"),),
        ),
        membership=UserMembershipRecord(
            user_id="user-1",
            tenant_id="tenant-1",
            role="admin",
        ),
    )


def _subject() -> EnterpriseSubject:
    return EnterpriseSubject(
        subject_type="workcode",
        subject="employee-secret",
        issuer="hr.example",
        issuer_tenant="enterprise-secret",
        verified_at=_PROOF_TIME,
    )


def _provider_identity(**changes: object) -> VerifiedProviderIdentity:
    return replace(
        VerifiedProviderIdentity(
            platform_user_id="user-1",
            tenant_id="tenant-1",
            provider="feishu",
            provider_tenant="tenant-key-secret",
            provider_account_id="provider-account-secret",
            subject_type="user_id",
            subject="provider-subject-secret",
            verified_at=_PROOF_TIME,
        ),
        **changes,
    )


def test_api_utils_reexports_the_single_canonical_principal() -> None:
    assert CompatibilityPrincipal is Principal


def test_principal_rejects_direct_construction_without_evidence_builder() -> None:
    with pytest.raises(PrincipalBuildError) as exc_info:
        Principal(platform_user_id="user-1", tenant_id="tenant-1")

    assert exc_info.value.code is PrincipalErrorCode.CONTEXT_MISSING


def test_principal_is_deeply_immutable_and_excludes_authorization_state() -> None:
    principal = build_principal_from_authenticated_actor(
        actor=AuthenticatedActor(
            platform_user_id="user-1",
            display_name="Alice",
        ),
        membership=TenantMembershipEvidence(
            platform_user_id="user-1",
            tenant_id="tenant-1",
        ),
        authentication=_web_auth(),
    )

    assert principal.id == "user-1"
    assert principal.nickname == "Alice"
    assert principal.tenant_id == "tenant-1"
    assert principal.identity_revision is None
    assert {item.name for item in fields(Principal)} == {
        "platform_user_id",
        "tenant_id",
        "authentication",
        "identity_revision",
        "provider_identity",
        "enterprise_subject",
        "display_name",
    }
    with pytest.raises((FrozenInstanceError, AttributeError)):
        principal.tenant_id = "attacker-tenant"  # type: ignore[misc]
    assert not hasattr(principal, "__dict__")
    assert not hasattr(principal, "role")
    assert not hasattr(principal, "groups")
    assert not hasattr(principal, "scopes")
    assert not hasattr(principal, "access_token")


def test_default_repr_hides_platform_and_provider_identifiers() -> None:
    secret_values = {
        "user-1",
        "alice@example.com",
        "Alice",
        "identity-1",
        "employee-secret",
        "enterprise-secret",
        "tenant-key-secret",
        "provider-account-secret",
        "provider-subject-secret",
    }
    subject = _subject()
    principal = build_principal_from_resolved_identity(
        result=_resolved(),
        authentication=_enterprise_auth(IdentityAssurance.ENTERPRISE_VERIFIED),
        enterprise_subject_evidence=VerifiedEnterpriseSubjectEvidence(
            platform_user_id="user-1",
            tenant_id="tenant-1",
            enterprise_subject=subject,
        ),
        provider_identity=_provider_identity(),
    )

    rendered = repr(principal)
    rendered_subject = repr(subject)
    for secret in secret_values:
        assert secret not in rendered
        assert secret not in rendered_subject


@pytest.mark.parametrize(
    "bad_time",
    [datetime(2026, 8, 12, 15, 30), _NOW + timedelta(seconds=1)],
)
def test_authentication_times_must_be_aware_and_not_after_validation(bad_time: datetime) -> None:
    with pytest.raises(PrincipalBuildError) as exc_info:
        AuthenticationContext(
            source=AuthenticationSource.WEB_SESSION,
            assurance=IdentityAssurance.AUTHENTICATED,
            validated_at=_NOW,
            authenticated_at=bad_time,
        )

    assert exc_info.value.code is PrincipalErrorCode.AUTHENTICATION_INVALID


def test_current_web_and_api_credentials_cannot_claim_enterprise_assurance() -> None:
    with pytest.raises(PrincipalBuildError) as exc_info:
        AuthenticationContext(
            source=AuthenticationSource.SDK_API_TOKEN,
            assurance=IdentityAssurance.DIRECTORY_VERIFIED,
            validated_at=_NOW,
            provider="feishu",
            external_identity_id="identity-1",
            assurance_verified_at=_PROOF_TIME,
        )

    assert exc_info.value.code is PrincipalErrorCode.ASSURANCE_INVALID


@pytest.mark.parametrize(
    ("provider", "identity_id", "proof_time"),
    [
        (None, "identity-1", _PROOF_TIME),
        ("feishu", None, _PROOF_TIME),
        ("feishu", "identity-1", None),
    ],
)
def test_enterprise_authentication_requires_complete_internal_evidence(
    provider: str | None,
    identity_id: str | None,
    proof_time: datetime | None,
) -> None:
    with pytest.raises(PrincipalBuildError) as exc_info:
        AuthenticationContext(
            source=AuthenticationSource.ENTERPRISE_IDENTITY,
            assurance=IdentityAssurance.DIRECTORY_VERIFIED,
            validated_at=_NOW,
            provider=provider,
            external_identity_id=identity_id,
            assurance_verified_at=proof_time,
        )

    assert exc_info.value.code is PrincipalErrorCode.ASSURANCE_INVALID


def test_builder_rejects_actor_membership_coordinate_confusion() -> None:
    with pytest.raises(PrincipalBuildError) as exc_info:
        build_principal_from_authenticated_actor(
            actor=AuthenticatedActor(platform_user_id="user-a"),
            membership=TenantMembershipEvidence(
                platform_user_id="user-b",
                tenant_id="tenant-1",
            ),
            authentication=_web_auth(),
        )

    assert exc_info.value.code is PrincipalErrorCode.CONTEXT_CONFLICT


def test_authenticated_actor_builder_cannot_bypass_resolved_identity_path() -> None:
    with pytest.raises(PrincipalBuildError) as exc_info:
        build_principal_from_authenticated_actor(
            actor=AuthenticatedActor(platform_user_id="user-1"),
            membership=TenantMembershipEvidence(
                platform_user_id="user-1",
                tenant_id="tenant-1",
            ),
            authentication=_enterprise_auth(),
        )

    assert exc_info.value.code is PrincipalErrorCode.AUTHENTICATION_INVALID


def test_enterprise_subject_evidence_cannot_be_grafted_across_users_or_tenants() -> None:
    for user_id, tenant_id in (("user-other", "tenant-1"), ("user-1", "tenant-other")):
        with pytest.raises(PrincipalBuildError) as exc_info:
            build_principal_from_resolved_identity(
                result=_resolved(),
                authentication=_enterprise_auth(IdentityAssurance.ENTERPRISE_VERIFIED),
                enterprise_subject_evidence=VerifiedEnterpriseSubjectEvidence(
                    platform_user_id=user_id,
                    tenant_id=tenant_id,
                    enterprise_subject=_subject(),
                ),
            )

        assert exc_info.value.code is PrincipalErrorCode.CONTEXT_CONFLICT


def test_enterprise_assurance_requires_subject_with_the_same_proof_time() -> None:
    later_subject = EnterpriseSubject(
        subject_type="workcode",
        subject="employee-secret",
        issuer="hr.example",
        issuer_tenant="enterprise-secret",
        verified_at=_PROOF_TIME + timedelta(seconds=1),
    )
    with pytest.raises(PrincipalBuildError) as exc_info:
        build_principal_from_resolved_identity(
            result=_resolved(),
            authentication=_enterprise_auth(IdentityAssurance.ENTERPRISE_VERIFIED),
            enterprise_subject_evidence=VerifiedEnterpriseSubjectEvidence(
                platform_user_id="user-1",
                tenant_id="tenant-1",
                enterprise_subject=later_subject,
            ),
        )

    assert exc_info.value.code is PrincipalErrorCode.ASSURANCE_INVALID


def test_resolved_identity_builder_binds_provider_identity_user_and_tenant() -> None:
    principal = build_principal_from_resolved_identity(
        result=_resolved(),
        authentication=_enterprise_auth(),
        provider_identity=_provider_identity(),
    )

    assert principal.id == "user-1"
    assert principal.tenant_id == "tenant-1"
    assert principal.display_name == "Alice"
    assert principal.identity_revision == 3
    assert principal.provider_identity == _provider_identity()
    assert principal.authentication.assurance is IdentityAssurance.DIRECTORY_VERIFIED
    assert "tenant-key-secret" not in repr(principal)
    assert "provider-subject-secret" not in repr(principal)


@pytest.mark.parametrize("subject", [" leading", "trailing ", "line\nbreak", "x" * 256])
def test_provider_identity_rejects_noncanonical_subject(subject: str) -> None:
    with pytest.raises(PrincipalBuildError) as exc_info:
        _provider_identity(subject=subject)

    assert exc_info.value.code is PrincipalErrorCode.INPUT_INVALID


@pytest.mark.parametrize(
    "changes",
    [
        {"platform_user_id": "user-other"},
        {"tenant_id": "tenant-other"},
        {"provider": "dingtalk"},
        {"provider_tenant": "tenant-other"},
        {"subject_type": "staff_id"},
        {"subject": "provider-subject-other"},
        {"verified_at": _PROOF_TIME - timedelta(seconds=1)},
    ],
)
def test_provider_identity_cannot_be_grafted_or_reinterpreted(
    changes: dict[str, object],
) -> None:
    with pytest.raises(PrincipalBuildError) as exc_info:
        build_principal_from_resolved_identity(
            result=_resolved(),
            authentication=_enterprise_auth(),
            provider_identity=_provider_identity(**changes),
        )

    assert exc_info.value.code is PrincipalErrorCode.CONTEXT_CONFLICT


@pytest.mark.parametrize(
    "inconsistent_result",
    [
        replace(_resolved(), error_code=IdentityErrorCode.INACTIVE),
        replace(_resolved(), provisioning_action=ProvisioningAction.REQUIRE_LINK),
        replace(_resolved(), provisioning_policy_revision=1),
        replace(_resolved(), provider_verification_required=True),
    ],
)
def test_resolved_identity_builder_rejects_inconsistent_adapter_results(
    inconsistent_result: IdentityResolutionResult,
) -> None:
    with pytest.raises(PrincipalBuildError) as exc_info:
        build_principal_from_resolved_identity(
            result=inconsistent_result,
            authentication=_enterprise_auth(),
        )

    assert exc_info.value.code is PrincipalErrorCode.CONTEXT_CONFLICT


@pytest.mark.parametrize(
    "identity_proof_time",
    [None, _PROOF_TIME - timedelta(seconds=1), _NOW + timedelta(seconds=1)],
)
def test_directory_assurance_is_bound_to_the_identity_proof_time(
    identity_proof_time: datetime | None,
) -> None:
    result = _resolved()
    assert result.identity is not None
    result = replace(
        result,
        identity=replace(result.identity, verified_at=identity_proof_time),
    )

    with pytest.raises(PrincipalBuildError) as exc_info:
        build_principal_from_resolved_identity(
            result=result,
            authentication=_enterprise_auth(),
        )

    assert exc_info.value.code is PrincipalErrorCode.ASSURANCE_INVALID


def test_principal_accepts_i3_attribute_and_enterprise_issuer_column_limits() -> None:
    result = _resolved()
    assert result.identity is not None
    result = replace(
        result,
        identity=replace(result.identity, attributes=(("display_name", "A" * 512),)),
    )

    principal = build_principal_from_resolved_identity(
        result=result,
        authentication=_enterprise_auth(),
    )
    subject = EnterpriseSubject(
        subject_type="workcode",
        subject="employee-secret",
        issuer="i" * 128,
        issuer_tenant="enterprise-secret",
        verified_at=_PROOF_TIME,
    )

    assert principal.display_name == "A" * 512
    assert subject.issuer == "i" * 128


@pytest.mark.parametrize(
    "result",
    [
        IdentityResolutionResult(status=IdentityResolutionStatus.MISSING),
        IdentityResolutionResult(status=IdentityResolutionStatus.INACTIVE),
        IdentityResolutionResult(status=IdentityResolutionStatus.CONFLICT),
    ],
)
def test_unresolved_identity_is_never_promoted(result: IdentityResolutionResult) -> None:
    with pytest.raises(PrincipalBuildError) as exc_info:
        build_principal_from_resolved_identity(
            result=result,
            authentication=_enterprise_auth(),
        )

    assert exc_info.value.code is PrincipalErrorCode.IDENTITY_INACTIVE


def test_resolved_identity_rejects_wrong_provider_or_internal_identity_id() -> None:
    for provider, identity_id in (("dingtalk", "identity-1"), ("feishu", "identity-other")):
        auth = AuthenticationContext(
            source=AuthenticationSource.ENTERPRISE_IDENTITY,
            assurance=IdentityAssurance.DIRECTORY_VERIFIED,
            validated_at=_NOW,
            assurance_verified_at=_PROOF_TIME,
            provider=provider,
            external_identity_id=identity_id,
        )
        with pytest.raises(PrincipalBuildError) as exc_info:
            build_principal_from_resolved_identity(
                result=_resolved(),
                authentication=auth,
            )

        assert exc_info.value.code is PrincipalErrorCode.AUTHENTICATION_INVALID


def test_resolved_identity_rejects_non_active_membership_role() -> None:
    result = _resolved()
    assert result.membership is not None
    result = replace(
        result,
        membership=replace(result.membership, role="invite"),
    )

    with pytest.raises(PrincipalBuildError) as exc_info:
        build_principal_from_resolved_identity(
            result=result,
            authentication=_enterprise_auth(),
        )

    assert exc_info.value.code is PrincipalErrorCode.MEMBERSHIP_INACTIVE


def test_principal_error_text_never_contains_input_identifiers() -> None:
    secret = "secret-user-id"
    with pytest.raises(PrincipalBuildError) as exc_info:
        AuthenticatedActor(platform_user_id=secret * 10)

    assert exc_info.value.code is PrincipalErrorCode.INPUT_INVALID
    assert secret not in str(exc_info.value)
    assert secret not in repr(exc_info.value)
