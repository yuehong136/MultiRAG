"""Immutable EIM-P3 policy contracts independent of MCP transport details."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Literal


class DelegationErrorCode(StrEnum):
    SNAPSHOT_INVALID = "snapshot_invalid"
    CONTEXT_REQUIRED = "context_required"
    SERVER_NOT_BOUND = "server_not_bound"
    SERVER_TENANT_MISMATCH = "server_tenant_mismatch"
    SERVER_AUDIENCE_MISMATCH = "server_audience_mismatch"
    TRANSPORT_NOT_SUPPORTED = "transport_not_supported"
    STATIC_AUTH_CONFLICT = "static_auth_conflict"
    GRANT_NOT_FOUND = "grant_not_found"
    TOOL_POLICY_NOT_FOUND = "tool_policy_not_found"
    SCOPE_DENIED = "scope_denied"
    ASSURANCE_DENIED = "assurance_denied"
    TOKEN_ISSUANCE_FAILED = "token_issuance_failed"


class McpDelegationError(RuntimeError):
    """Stable, non-sensitive P3 refusal."""

    def __init__(self, code: DelegationErrorCode) -> None:
        self.code = code
        super().__init__("MCP delegation rejected")


@dataclass(frozen=True, slots=True)
class DelegatedServerBinding:
    mcp_server_id: str
    resource_name: str
    audience: str


@dataclass(frozen=True, slots=True)
class DelegationGrant:
    tenant_id: str = field(repr=False)
    platform_user_id: str = field(repr=False)
    agent_id: str
    agent_revision_id: str
    resource_name: str
    allowed_scopes: frozenset[str]


@dataclass(frozen=True, slots=True)
class DelegatedEnterpriseSubjectRequirement:
    subject_type: str
    issuer: str
    issuer_tenant: str = field(repr=False)


@dataclass(frozen=True, slots=True, order=True)
class DelegatedProviderIdentityCoordinate:
    provider: str
    provider_tenant: str = field(repr=False)
    subject_type: str


@dataclass(frozen=True, slots=True)
class DelegatedProviderIdentityRequirement:
    any_of: tuple[DelegatedProviderIdentityCoordinate, ...]


@dataclass(frozen=True, slots=True)
class DelegatedToolPolicy:
    canonical_tool_name: str
    required_scopes: frozenset[str]
    effect: Literal["read", "prepare", "side_effect"]
    replay_mode: Literal["reusable", "single_use"]
    enterprise_subject: DelegatedEnterpriseSubjectRequirement | None
    accepted_acr_values: frozenset[str]
    required_amr: frozenset[str]
    provider_identity: DelegatedProviderIdentityRequirement | None = field(
        default=None,
        repr=False,
    )


@dataclass(frozen=True, slots=True)
class ToolPolicySnapshot:
    policy_revision: str
    scope_registry: frozenset[str]
    tools: Mapping[str, DelegatedToolPolicy]

    @classmethod
    def frozen(
        cls,
        *,
        policy_revision: str,
        scope_registry: frozenset[str],
        tools: dict[str, DelegatedToolPolicy],
    ) -> ToolPolicySnapshot:
        return cls(
            policy_revision=policy_revision,
            scope_registry=scope_registry,
            tools=MappingProxyType(dict(tools)),
        )


GrantKey = tuple[str, str, str, str, str]


@dataclass(frozen=True, slots=True)
class GrantPolicySnapshot:
    grant_revision: str
    policy_revision: str
    credential_generation: int
    bindings: Mapping[str, DelegatedServerBinding]
    grants: Mapping[GrantKey, DelegationGrant]

    @classmethod
    def frozen(
        cls,
        *,
        grant_revision: str,
        policy_revision: str,
        credential_generation: int,
        bindings: dict[str, DelegatedServerBinding],
        grants: dict[GrantKey, DelegationGrant],
    ) -> GrantPolicySnapshot:
        return cls(
            grant_revision=grant_revision,
            policy_revision=policy_revision,
            credential_generation=credential_generation,
            bindings=MappingProxyType(dict(bindings)),
            grants=MappingProxyType(dict(grants)),
        )


__all__ = [
    "DelegatedEnterpriseSubjectRequirement",
    "DelegatedProviderIdentityCoordinate",
    "DelegatedProviderIdentityRequirement",
    "DelegatedServerBinding",
    "DelegatedToolPolicy",
    "DelegationErrorCode",
    "DelegationGrant",
    "GrantKey",
    "GrantPolicySnapshot",
    "McpDelegationError",
    "ToolPolicySnapshot",
]
