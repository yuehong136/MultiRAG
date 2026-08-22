"""EIM-A2 configuration-to-runtime assembly contract."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from api.identity.mcp_issuer.contracts import McpAccessGrant, McpAccessTokenRequest
from api.identity.mcp_issuer.runtime import get_mcp_token_issuer, reset_mcp_token_issuer
from api.identity.principal import (
    AuthenticatedActor,
    AuthenticationContext,
    AuthenticationSource,
    IdentityAssurance,
    TenantMembershipEvidence,
    build_principal_from_authenticated_actor,
)
from common.app_config import McpIssuerConfig


def _write_keypair(tmp_path: Path) -> tuple[Path, Path, ec.EllipticCurvePublicKey]:
    private_key = ec.generate_private_key(ec.SECP256R1())
    private_path = tmp_path / "runtime.private.pem"
    public_path = tmp_path / "runtime.public.pem"
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
    return private_path, public_path, private_key.public_key()


def test_runtime_assembles_enabled_file_provider_and_resource_registry(tmp_path: Path, monkeypatch) -> None:
    from api.identity.mcp_issuer import runtime

    private_path, public_path, public_key = _write_keypair(tmp_path)
    config = McpIssuerConfig.model_validate(
        {
            "enabled": True,
            "issuer": "https://auth.multirag.example",
            "client_id": "multirag-first-party",
            "ttl_seconds": 60,
            "jwks_cache_ttl_seconds": 120,
            "resources": {
                "ofmcp_gateway": {
                    "audience": "https://gateway.ofmcp.example/mcp",
                    "registered_scopes": ["leave:read"],
                },
            },
            "key_provider": {
                "active_key_id": "runtime-current",
                "private_key_file": str(private_path),
                "public_key_files": {"runtime-current": str(public_path)},
            },
        },
    )
    monkeypatch.setattr(
        runtime,
        "get_app_config",
        lambda: SimpleNamespace(identity=SimpleNamespace(mcp_issuer=config)),
    )
    reset_mcp_token_issuer()
    try:
        issuer = get_mcp_token_issuer()
        assert get_mcp_token_issuer() is issuer
        assert issuer.jwks_cache_ttl_seconds == 120
        assert issuer.jwks_document()["keys"][0]["kid"] == "runtime-current"

        now = datetime.now(UTC)
        principal = build_principal_from_authenticated_actor(
            actor=AuthenticatedActor(platform_user_id="user-a"),
            membership=TenantMembershipEvidence(
                platform_user_id="user-a",
                tenant_id="tenant-a",
            ),
            authentication=AuthenticationContext(
                source=AuthenticationSource.WEB_SESSION,
                assurance=IdentityAssurance.AUTHENTICATED,
                validated_at=now,
            ),
        )
        result = issuer.issue(
            McpAccessTokenRequest(
                principal=principal,
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
            audience="https://gateway.ofmcp.example/mcp",
            issuer="https://auth.multirag.example",
        )
        assert claims["client_id"] == "multirag-first-party"
        assert claims["exp"] - claims["iat"] == 60
    finally:
        reset_mcp_token_issuer()
