"""EIM-A2 short-lived ES256 access-token issuance service."""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from api.identity.mcp_issuer.contracts import (
    ALGORITHM,
    ENTERPRISE_ACR,
    MAX_TOKEN_BYTES,
    TOKEN_TYPE,
    TOKEN_USE,
    IssuanceErrorCode,
    IssuancePolicyFacts,
    IssuedMcpAccessToken,
    McpAccessGrant,
    McpAccessTokenRequest,
    McpIssuerProfile,
    McpTokenIssuanceError,
    evaluate_issuance_policy,
)
from api.identity.mcp_issuer.keys import SigningKeyError, SigningKeyProvider
from api.identity.principal import IdentityAssurance


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _canonical_json(value: dict[str, object]) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _valid_jti(value: object) -> bool:
    return type(value) is str and value == value.strip() and value.isascii() and 16 <= len(value) <= 128 and not any(char.isspace() for char in value)


class McpTokenIssuer:
    """Issue one resource-bound access token from server-owned authority."""

    def __init__(
        self,
        *,
        profile: McpIssuerProfile,
        signing_keys: SigningKeyProvider,
        clock: Callable[[], datetime],
        jti_factory: Callable[[], str],
    ) -> None:
        if not isinstance(profile, McpIssuerProfile) or not isinstance(signing_keys, SigningKeyProvider):
            raise TypeError("invalid MCP token issuer dependency")
        self._profile = profile
        self._signing_keys = signing_keys
        self._clock = clock
        self._jti_factory = jti_factory

    @property
    def jwks_cache_ttl_seconds(self) -> int:
        return self._profile.jwks_cache_ttl_seconds

    def jwks_document(self) -> dict[str, list[dict[str, str]]]:
        return self._signing_keys.snapshot.to_jwks_document()

    def resource_audience(self, resource_name: str) -> str | None:
        """Return the server-owned exact audience without exposing the profile."""

        resource = self._profile.resource(resource_name)
        return resource.audience if resource is not None else None

    def issue(
        self,
        request: McpAccessTokenRequest,
        grant: McpAccessGrant,
    ) -> IssuedMcpAccessToken:
        resource = self._profile.resource(request.resource_name)
        if resource is None:
            raise McpTokenIssuanceError(IssuanceErrorCode.RESOURCE_NOT_REGISTERED)

        principal = request.principal
        assurance_verified = principal.authentication.assurance is IdentityAssurance.ENTERPRISE_VERIFIED and principal.enterprise_subject is not None
        decision = evaluate_issuance_policy(
            IssuancePolicyFacts(
                subject_is_platform_principal=True,
                tenant_is_server_bound=True,
                registered_scopes=resource.registered_scopes,
                allowed_scopes=grant.allowed_scopes,
                requested_scopes=request.requested_scopes,
                requested_claims=request.requested_claims,
                requires_assurance="enterprise_subject" in request.requested_claims,
                assurance_verified=assurance_verified,
                allowed_requested_claims=resource.allowed_requested_claims,
            ),
        )
        if not decision.allowed:
            assert decision.failure_reason is not None
            raise McpTokenIssuanceError(IssuanceErrorCode(decision.failure_reason))
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise McpTokenIssuanceError(IssuanceErrorCode.REQUEST_INVALID)
        issued_at = datetime.fromtimestamp(int(now.timestamp()), tz=UTC)
        expires_at = issued_at + timedelta(seconds=self._profile.ttl_seconds)
        jti = self._jti_factory()
        if not _valid_jti(jti):
            raise McpTokenIssuanceError(IssuanceErrorCode.JTI_INVALID)

        claims: dict[str, object] = {
            "agent_id": request.agent_id,
            "aud": resource.audience,
            "client_id": self._profile.client_id,
            "exp": int(expires_at.timestamp()),
            "iat": int(issued_at.timestamp()),
            "iss": self._profile.issuer,
            "jti": jti,
            "nbf": int(issued_at.timestamp()),
            "scope": " ".join(sorted(request.requested_scopes)),
            "sub": principal.platform_user_id,
            "tenant_id": principal.tenant_id,
            "token_use": TOKEN_USE,
        }
        authenticated_at = principal.authentication.authenticated_at
        if authenticated_at is not None:
            auth_time = int(authenticated_at.timestamp())
            if auth_time > int(issued_at.timestamp()):
                raise McpTokenIssuanceError(IssuanceErrorCode.AUTH_TIME_INVALID)
            claims["auth_time"] = auth_time
        if principal.authentication.assurance is IdentityAssurance.ENTERPRISE_VERIFIED:
            claims["acr"] = ENTERPRISE_ACR
        if "enterprise_subject" in request.requested_claims:
            subject = principal.enterprise_subject
            requirement = resource.enterprise_subject_requirement
            if subject is None or requirement is None or subject.subject_type != requirement.subject_type or subject.issuer != requirement.issuer or subject.issuer_tenant != requirement.issuer_tenant:
                raise McpTokenIssuanceError(IssuanceErrorCode.ASSURANCE_NOT_VERIFIED)
            claims["enterprise_subject"] = {
                "issuer": subject.issuer,
                "subject": subject.subject,
                "tenant": subject.issuer_tenant,
                "type": subject.subject_type,
            }

        snapshot = self._signing_keys.snapshot
        header: dict[str, object] = {
            "alg": ALGORITHM,
            "kid": snapshot.active_kid,
            "typ": TOKEN_TYPE,
        }
        encoded_header = _b64url(_canonical_json(header))
        encoded_claims = _b64url(_canonical_json(claims))
        signing_input = f"{encoded_header}.{encoded_claims}".encode("ascii")
        try:
            signature = self._signing_keys.sign_es256(signing_input)
        except SigningKeyError as exc:
            raise McpTokenIssuanceError(IssuanceErrorCode.SIGNING_FAILED) from exc
        if len(signature) != 64:
            raise McpTokenIssuanceError(IssuanceErrorCode.SIGNING_FAILED)
        compact = f"{encoded_header}.{encoded_claims}.{_b64url(signature)}"
        if len(compact.encode("ascii")) > MAX_TOKEN_BYTES:
            raise McpTokenIssuanceError(IssuanceErrorCode.TOKEN_TOO_LARGE)
        return IssuedMcpAccessToken(
            compact=compact,
            expires_at=expires_at,
            scopes=request.requested_scopes,
            kid=snapshot.active_kid,
        )


__all__ = ["McpTokenIssuer"]
