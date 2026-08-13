"""EIM-A2 production issuance policy and ES256 token contracts."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from api.identity.contracts import (
    ExternalIdentityRecord,
    IdentityResolutionResult,
    IdentityResolutionStatus,
    UserMembershipRecord,
)
from api.identity.mcp_issuer.contracts import (
    EnterpriseSubjectRequirement,
    IssuanceErrorCode,
    IssuancePolicyFacts,
    McpAccessGrant,
    McpAccessTokenRequest,
    McpIssuerProfile,
    McpResourceProfile,
    McpTokenIssuanceError,
    evaluate_issuance_policy,
)
from api.identity.mcp_issuer.keys import FileSigningKeyProvider
from api.identity.mcp_issuer.service import McpTokenIssuer
from api.identity.principal import (
    AuthenticatedActor,
    AuthenticationContext,
    AuthenticationSource,
    EnterpriseSubject,
    IdentityAssurance,
    TenantMembershipEvidence,
    VerifiedEnterpriseSubjectEvidence,
    build_principal_from_authenticated_actor,
    build_principal_from_resolved_identity,
)

CORPUS_MANIFEST = Path(__file__).resolve().parents[1] / "fixtures" / "eim_a1" / "v1" / "manifest.json"
NOW = datetime(2026, 8, 13, 8, 0, tzinfo=UTC)
AUDIENCE = "https://gateway.ofmcp.example/mcp"
ISSUER = "https://auth.multirag.example"


def _principal(
    *,
    authenticated_at: datetime | None = None,
    validated_at: datetime | None = None,
):
    return build_principal_from_authenticated_actor(
        actor=AuthenticatedActor(platform_user_id="user-a", display_name="Alice"),
        membership=TenantMembershipEvidence(platform_user_id="user-a", tenant_id="tenant-a"),
        authentication=AuthenticationContext(
            source=AuthenticationSource.WEB_SESSION,
            assurance=IdentityAssurance.AUTHENTICATED,
            validated_at=validated_at or NOW - timedelta(seconds=5),
            authenticated_at=authenticated_at,
        ),
    )


def _enterprise_principal():
    proof_time = NOW - timedelta(minutes=5)
    return build_principal_from_resolved_identity(
        result=IdentityResolutionResult(
            status=IdentityResolutionStatus.RESOLVED,
            identity=ExternalIdentityRecord(
                id="identity-a",
                tenant_id="tenant-a",
                user_id="user-a",
                provider="feishu",
                provider_tenant_key="provider-tenant-secret",
                subject_type="user_id",
                subject_value="provider-user-secret",
                state="active",
                verified_at=proof_time,
                last_seen_at=proof_time,
                identity_revision=1,
            ),
            membership=UserMembershipRecord(user_id="user-a", tenant_id="tenant-a", role="normal"),
        ),
        authentication=AuthenticationContext(
            source=AuthenticationSource.ENTERPRISE_IDENTITY,
            assurance=IdentityAssurance.ENTERPRISE_VERIFIED,
            validated_at=NOW - timedelta(seconds=5),
            assurance_verified_at=proof_time,
            provider="feishu",
            external_identity_id="identity-a",
        ),
        enterprise_subject_evidence=VerifiedEnterpriseSubjectEvidence(
            platform_user_id="user-a",
            tenant_id="tenant-a",
            enterprise_subject=EnterpriseSubject(
                subject_type="workcode",
                subject="opaque-workcode-a",
                issuer="https://hr.example",
                issuer_tenant="issuer-tenant-a",
                verified_at=proof_time,
            ),
        ),
    )


def _provider(tmp_path: Path) -> tuple[FileSigningKeyProvider, ec.EllipticCurvePublicKey]:
    private_key = ec.generate_private_key(ec.SECP256R1())
    private_path = tmp_path / "issuer.private.pem"
    public_path = tmp_path / "issuer.public.pem"
    private_path.write_bytes(
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )
    private_path.chmod(0o600)
    public_path.write_bytes(
        private_key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ),
    )
    provider = FileSigningKeyProvider.from_files(
        active_key_id="issuer-current",
        private_key_file=private_path,
        public_key_files={"issuer-current": public_path},
    )
    return provider, private_key.public_key()


def _issuer(
    tmp_path: Path,
    *,
    audience: str = AUDIENCE,
    enterprise_subject_requirement: EnterpriseSubjectRequirement | None = None,
    jti: str = "test-jti-000000000000000000000001",
    now: datetime = NOW,
) -> tuple[McpTokenIssuer, ec.EllipticCurvePublicKey]:
    provider, public_key = _provider(tmp_path)
    profile = McpIssuerProfile(
        issuer=ISSUER,
        client_id="multirag-first-party",
        ttl_seconds=300,
        jwks_cache_ttl_seconds=300,
        resources=(
            McpResourceProfile(
                name="ofmcp_gateway",
                audience=audience,
                registered_scopes=frozenset({"leave:read", "leave:submit", "medic:submit"}),
                enterprise_subject_requirement=enterprise_subject_requirement,
            ),
        ),
    )
    return (
        McpTokenIssuer(
            profile=profile,
            signing_keys=provider,
            clock=lambda: now,
            jti_factory=lambda: jti,
        ),
        public_key,
    )


@pytest.mark.parametrize(
    "case",
    json.loads(CORPUS_MANIFEST.read_text(encoding="utf-8"))["issuance_policy_cases"],
    ids=lambda case: case["id"],
)
def test_production_policy_executes_every_a1_issuance_case(case: dict[str, Any]) -> None:
    request = case["request"]
    decision = evaluate_issuance_policy(
        IssuancePolicyFacts(
            subject_is_platform_principal=request["subject_source"] == "platform_principal",
            tenant_is_server_bound=request["tenant_source"] == "server_context",
            registered_scopes=frozenset(request["registered_scopes"]),
            allowed_scopes=frozenset(request["allowed_scopes"]),
            requested_scopes=frozenset(request["requested_scopes"]),
            requested_claims=frozenset(request["requested_claims"]),
            requires_assurance=request["requires_assurance"],
            assurance_verified=request["assurance_verified"],
        ),
    )

    assert decision.allowed is (case["expected"]["issuance"] == "allow")
    assert decision.failure_reason == case["expected"]["failure_reason"]


def test_issuer_signs_exact_short_lived_audience_bound_access_token(tmp_path: Path) -> None:
    issuer, public_key = _issuer(tmp_path)
    result = issuer.issue(
        McpAccessTokenRequest(
            principal=_principal(authenticated_at=NOW - timedelta(minutes=2)),
            agent_id="agent-release-a",
            resource_name="ofmcp_gateway",
            requested_scopes=frozenset({"leave:read"}),
        ),
        McpAccessGrant(allowed_scopes=frozenset({"leave:read", "leave:submit"})),
    )

    header = jwt.get_unverified_header(result.compact)
    claims = jwt.decode(
        result.compact,
        public_key,
        algorithms=["ES256"],
        audience=AUDIENCE,
        issuer=ISSUER,
        options={"verify_exp": False, "verify_iat": False, "verify_nbf": False},
    )
    assert header == {"alg": "ES256", "kid": "issuer-current", "typ": "at+jwt"}
    assert claims == {
        "agent_id": "agent-release-a",
        "aud": AUDIENCE,
        "auth_time": int((NOW - timedelta(minutes=2)).timestamp()),
        "client_id": "multirag-first-party",
        "exp": int((NOW + timedelta(seconds=300)).timestamp()),
        "iat": int(NOW.timestamp()),
        "iss": ISSUER,
        "jti": "test-jti-000000000000000000000001",
        "nbf": int(NOW.timestamp()),
        "scope": "leave:read",
        "sub": "user-a",
        "tenant_id": "tenant-a",
        "token_use": "mcp_access",
    }
    assert "amr" not in claims
    assert len(result.compact.encode("ascii")) <= 4096
    assert result.expires_at == NOW + timedelta(seconds=300)
    assert result.compact not in repr(result)
    assert "user-a" not in repr(result)


def test_auth_time_in_the_issuance_second_is_encoded_as_numeric_date(tmp_path: Path) -> None:
    issuer, public_key = _issuer(tmp_path, now=NOW + timedelta(microseconds=700_000))
    authenticated_at = NOW + timedelta(microseconds=500_000)

    result = issuer.issue(
        McpAccessTokenRequest(
            principal=_principal(
                authenticated_at=authenticated_at,
                validated_at=NOW + timedelta(microseconds=600_000),
            ),
            agent_id="agent-release-a",
            resource_name="ofmcp_gateway",
            requested_scopes=frozenset({"leave:read"}),
        ),
        McpAccessGrant(allowed_scopes=frozenset({"leave:read"})),
    )

    claims = jwt.decode(
        result.compact,
        public_key,
        algorithms=["ES256"],
        audience=AUDIENCE,
        issuer=ISSUER,
        options={"verify_exp": False, "verify_iat": False, "verify_nbf": False},
    )
    assert claims["auth_time"] == claims["iat"] == int(NOW.timestamp())


@pytest.mark.parametrize(
    ("requested", "allowed", "expected"),
    [
        ({"unknown:scope"}, {"unknown:scope"}, IssuanceErrorCode.REQUESTED_SCOPE_NOT_REGISTERED),
        ({"leave:submit"}, {"leave:read"}, IssuanceErrorCode.REQUESTED_SCOPE_NOT_GRANTED),
    ],
)
def test_issuer_rejects_scope_elevation_before_signing(
    tmp_path: Path,
    requested: set[str],
    allowed: set[str],
    expected: IssuanceErrorCode,
) -> None:
    issuer, _ = _issuer(tmp_path)

    with pytest.raises(McpTokenIssuanceError) as raised:
        issuer.issue(
            McpAccessTokenRequest(
                principal=_principal(),
                agent_id="agent-release-a",
                resource_name="ofmcp_gateway",
                requested_scopes=frozenset(requested),
            ),
            McpAccessGrant(allowed_scopes=frozenset(allowed)),
        )

    assert raised.value.code is expected
    assert "unknown:scope" not in str(raised.value)


def test_issuer_rejects_unregistered_resource_and_forbidden_claim_without_signing(tmp_path: Path) -> None:
    issuer, _ = _issuer(tmp_path)
    for resource_name, requested_claims, expected in (
        ("attacker-resource", frozenset(), IssuanceErrorCode.RESOURCE_NOT_REGISTERED),
        ("ofmcp_gateway", frozenset({"roles"}), IssuanceErrorCode.REQUESTED_CLAIM_NOT_ALLOWED),
    ):
        with pytest.raises(McpTokenIssuanceError) as raised:
            issuer.issue(
                McpAccessTokenRequest(
                    principal=_principal(),
                    agent_id="agent-release-a",
                    resource_name=resource_name,
                    requested_scopes=frozenset({"leave:read"}),
                    requested_claims=requested_claims,
                ),
                McpAccessGrant(allowed_scopes=frozenset({"leave:read"})),
            )
        assert raised.value.code is expected


def test_issuer_public_jwks_contains_no_principal_or_private_material(tmp_path: Path) -> None:
    issuer, _ = _issuer(tmp_path)

    rendered = json.dumps(issuer.jwks_document(), sort_keys=True)

    assert "user-a" not in rendered
    assert "tenant-a" not in rendered
    assert "PRIVATE" not in rendered
    assert set(issuer.jwks_document()) == {"keys"}


def test_enterprise_subject_and_acr_require_explicit_resource_permission(tmp_path: Path) -> None:
    issuer, public_key = _issuer(
        tmp_path,
        enterprise_subject_requirement=EnterpriseSubjectRequirement(
            subject_type="workcode",
            issuer="https://hr.example",
            issuer_tenant="issuer-tenant-a",
        ),
    )
    result = issuer.issue(
        McpAccessTokenRequest(
            principal=_enterprise_principal(),
            agent_id="agent-release-a",
            resource_name="ofmcp_gateway",
            requested_scopes=frozenset({"leave:read"}),
            requested_claims=frozenset({"enterprise_subject"}),
        ),
        McpAccessGrant(allowed_scopes=frozenset({"leave:read"})),
    )

    claims = jwt.decode(
        result.compact,
        public_key,
        algorithms=["ES256"],
        audience=AUDIENCE,
        issuer=ISSUER,
        options={"verify_exp": False, "verify_iat": False, "verify_nbf": False},
    )
    assert claims["acr"] == "urn:multirag:assurance:enterprise-verified"
    assert claims["enterprise_subject"] == {
        "issuer": "https://hr.example",
        "subject": "opaque-workcode-a",
        "tenant": "issuer-tenant-a",
        "type": "workcode",
    }
    assert "amr" not in claims

    mismatched_issuer, _ = _issuer(
        tmp_path,
        enterprise_subject_requirement=EnterpriseSubjectRequirement(
            subject_type="talent_id",
            issuer="https://hr.example",
            issuer_tenant="issuer-tenant-a",
        ),
    )
    with pytest.raises(McpTokenIssuanceError) as mismatched:
        mismatched_issuer.issue(
            McpAccessTokenRequest(
                principal=_enterprise_principal(),
                agent_id="agent-release-a",
                resource_name="ofmcp_gateway",
                requested_scopes=frozenset({"leave:read"}),
                requested_claims=frozenset({"enterprise_subject"}),
            ),
            McpAccessGrant(allowed_scopes=frozenset({"leave:read"})),
        )
    assert mismatched.value.code is IssuanceErrorCode.ASSURANCE_NOT_VERIFIED


def test_token_size_and_jti_are_checked_before_returning_compact_bearer(tmp_path: Path) -> None:
    oversized_issuer, _ = _issuer(
        tmp_path,
        audience="https://gateway.ofmcp.example/" + "a" * 3_500,
    )
    with pytest.raises(McpTokenIssuanceError) as oversized:
        oversized_issuer.issue(
            McpAccessTokenRequest(
                principal=_principal(),
                agent_id="agent-release-a",
                resource_name="ofmcp_gateway",
                requested_scopes=frozenset({"leave:read"}),
            ),
            McpAccessGrant(allowed_scopes=frozenset({"leave:read"})),
        )
    assert oversized.value.code is IssuanceErrorCode.TOKEN_TOO_LARGE

    invalid_jti_issuer, _ = _issuer(tmp_path, jti="short")
    with pytest.raises(McpTokenIssuanceError) as invalid_jti:
        invalid_jti_issuer.issue(
            McpAccessTokenRequest(
                principal=_principal(),
                agent_id="agent-release-a",
                resource_name="ofmcp_gateway",
                requested_scopes=frozenset({"leave:read"}),
            ),
            McpAccessGrant(allowed_scopes=frozenset({"leave:read"})),
        )
    assert invalid_jti.value.code is IssuanceErrorCode.JTI_INVALID
