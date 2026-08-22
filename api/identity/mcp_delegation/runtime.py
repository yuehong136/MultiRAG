"""Lazy runtime assembly for disabled-by-default EIM-P3 delegation."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

from api.identity.mcp_delegation.policy import load_grant_policy_snapshot, load_tool_policy_snapshot
from api.identity.mcp_delegation.service import BoundMcpCredentialProvider, McpDelegationService
from api.identity.mcp_issuer.runtime import get_mcp_token_issuer
from api.identity.run_context import RunContext
from common.app_config import get_app_config

_active_service: McpDelegationService | None = None


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
