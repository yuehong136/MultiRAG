"""Lazy runtime assembly for disabled-by-default EIM-P3 delegation."""

from __future__ import annotations

import os
import ssl
import stat
from functools import lru_cache
from pathlib import Path
from typing import Any

from api.identity.mcp_delegation.policy import load_grant_policy_snapshot, load_tool_policy_snapshot
from api.identity.mcp_delegation.service import BoundMcpCredentialProvider, McpDelegationService
from api.identity.mcp_issuer.runtime import get_mcp_token_issuer
from api.identity.run_context import RunContext
from common.app_config import AppConfigError, get_app_config

_active_service: McpDelegationService | None = None
_MAX_CA_BUNDLE_BYTES = 1_048_576


def _effective_user_id() -> int:
    getter = getattr(os, "geteuid", None)
    if getter is None:
        raise AppConfigError("identity.mcp_delegation TLS CA bundle ownership cannot be verified")
    return int(getter())


def _trusted_ca_bundle_owner(file_owner_id: int) -> bool:
    return file_owner_id in {0, _effective_user_id()}


def _build_tls_ssl_context(ca_bundle_file: str) -> ssl.SSLContext | None:
    """Build an isolated trust store for delegated MCP without global env."""

    if not ca_bundle_file:
        return None
    path = Path(ca_bundle_file)
    if not path.is_absolute() or path.is_symlink():
        raise AppConfigError("identity.mcp_delegation TLS CA bundle is invalid")
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError:
        raise AppConfigError("identity.mcp_delegation TLS CA bundle is unavailable") from None
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size <= 0 or metadata.st_size > _MAX_CA_BUNDLE_BYTES:
            raise AppConfigError("identity.mcp_delegation TLS CA bundle is invalid")
        if os.name != "nt":
            mode = stat.S_IMODE(metadata.st_mode)
            if mode & 0o022 or not _trusted_ca_bundle_owner(metadata.st_uid):
                raise AppConfigError("identity.mcp_delegation TLS CA bundle permissions or ownership permit an untrusted principal")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            raw = stream.read(_MAX_CA_BUNDLE_BYTES + 1)
        if len(raw) > _MAX_CA_BUNDLE_BYTES:
            raise AppConfigError("identity.mcp_delegation TLS CA bundle is invalid")
    except OSError:
        raise AppConfigError("identity.mcp_delegation TLS CA bundle is unavailable") from None
    finally:
        os.close(descriptor)
    try:
        pem = raw.decode("ascii")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.verify_mode = ssl.CERT_REQUIRED
        context.check_hostname = True
        context.load_verify_locations(cadata=pem)
    except (UnicodeError, ValueError, ssl.SSLError):
        raise AppConfigError("identity.mcp_delegation TLS CA bundle is invalid") from None
    return context


@lru_cache(maxsize=1)
def get_mcp_delegation_service() -> McpDelegationService:
    config = get_app_config().identity.mcp_delegation.require_enabled()
    tool_policy = load_tool_policy_snapshot(Path(config.tool_policy_file))
    grant_policy = load_grant_policy_snapshot(
        Path(config.grant_policy_file),
        tool_policy=tool_policy,
    )
    return McpDelegationService(
        tool_policy=tool_policy,
        grant_policy=grant_policy,
        issuer=get_mcp_token_issuer(),
        tls_ssl_context=_build_tls_ssl_context(config.tls_ca_bundle_file),
    )


def resolve_mcp_credential_provider(
    *,
    mcp_server: Any,
    run_context: RunContext | None,
) -> BoundMcpCredentialProvider | None:
    if _active_service is None:
        return None
    return _active_service.bind(
        mcp_server=mcp_server,
        run_context=run_context,
    )


def activate_mcp_delegation() -> None:
    """Publish the immutable service during application startup."""

    global _active_service
    config = get_app_config().identity.mcp_delegation
    _active_service = get_mcp_delegation_service() if config.enabled else None


def reset_mcp_delegation_service() -> None:
    global _active_service
    _active_service = None
    get_mcp_delegation_service.cache_clear()


__all__ = [
    "activate_mcp_delegation",
    "get_mcp_delegation_service",
    "reset_mcp_delegation_service",
    "resolve_mcp_credential_provider",
]
