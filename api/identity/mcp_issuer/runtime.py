"""Lazy runtime assembly for the disabled-by-default EIM-A2 issuer."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

from api.identity.mcp_issuer.contracts import EnterpriseSubjectRequirement, McpIssuerProfile, McpResourceProfile
from api.identity.mcp_issuer.keys import FileSigningKeyProvider
from api.identity.mcp_issuer.service import McpTokenIssuer
from common.app_config import AppConfigError, get_app_config


@lru_cache(maxsize=1)
def get_mcp_token_issuer() -> McpTokenIssuer:
    config = get_app_config().identity.mcp_issuer.require_enabled()
    key_provider = config.key_provider
    if key_provider is None:
        raise AppConfigError("identity.mcp_issuer key provider is unavailable")
    signing_keys = FileSigningKeyProvider.from_files(
        active_key_id=key_provider.active_key_id,
        private_key_file=Path(key_provider.private_key_file.get_secret_value()),
        public_key_files={kid: Path(path) for kid, path in key_provider.public_key_files.items()},
    )
    profile = McpIssuerProfile(
        issuer=config.issuer,
        client_id=config.client_id,
        ttl_seconds=config.ttl_seconds,
        jwks_cache_ttl_seconds=config.jwks_cache_ttl_seconds,
        resources=tuple(
            McpResourceProfile(
                name=name,
                audience=resource.audience,
                registered_scopes=frozenset(resource.registered_scopes),
                allow_provider_identity=resource.allow_provider_identity,
                enterprise_subject_requirement=(
                    EnterpriseSubjectRequirement(
                        subject_type=resource.enterprise_subject.subject_type,
                        issuer=resource.enterprise_subject.issuer,
                        issuer_tenant=resource.enterprise_subject.issuer_tenant,
                    )
                    if resource.enterprise_subject is not None
                    else None
                ),
            )
            for name, resource in sorted(config.resources.items())
        ),
    )
    return McpTokenIssuer(
        profile=profile,
        signing_keys=signing_keys,
        clock=lambda: datetime.now(UTC),
        jti_factory=lambda: secrets.token_urlsafe(32),
    )


def reset_mcp_token_issuer() -> None:
    """Clear assembly cache for controlled config reloads and tests."""

    get_mcp_token_issuer.cache_clear()


__all__ = ["get_mcp_token_issuer", "reset_mcp_token_issuer"]
