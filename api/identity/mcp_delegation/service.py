"""Server-authoritative EIM-P3 grant evaluation and token issuance."""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Protocol

from api.identity.mcp_delegation.contracts import (
    DelegatedServerBinding,
    DelegatedToolPolicy,
    DelegationErrorCode,
    DelegationGrant,
    GrantPolicySnapshot,
    McpDelegationError,
    ToolPolicySnapshot,
)
from api.identity.mcp_issuer.contracts import (
    ENTERPRISE_ACR,
    IssuedMcpAccessToken,
    McpAccessGrant,
    McpAccessTokenRequest,
    McpTokenIssuanceError,
)
from api.identity.principal import IdentityAssurance, Principal
from api.identity.run_context import RunContext
from common.constants import MCPServerType
from common.mcp_tool_call_conn import MCPRequestCredential


class _TokenIssuer(Protocol):
    def resource_audience(self, resource_name: str) -> str | None: ...

    def issue(
        self,
        request: McpAccessTokenRequest,
        grant: McpAccessGrant,
    ) -> IssuedMcpAccessToken: ...


@dataclass(frozen=True, slots=True)
class _AuthorizationDecision:
    policy: DelegatedToolPolicy
    grant: DelegationGrant = field(repr=False)


@dataclass(frozen=True, slots=True)
class McpInteractionAuthorization:
    """Current non-secret policy facts needed before a durable resume."""

    effect: str
    replay_mode: str
    policy_revision: str
    credential_generation: int


@dataclass(frozen=True, slots=True)
class BoundMcpCredentialProvider:
    """One immutable server/run binding; tokens are never retained."""

    _service: McpDelegationService = field(repr=False)
    _binding: DelegatedServerBinding
    _principal: Principal = field(repr=False)
    _agent_id: str
    _agent_revision_id: str

    @property
    def resource_name(self) -> str:
        return self._binding.resource_name

    def is_authorized(self, canonical_tool_name: str) -> bool:
        """Evaluate model visibility without issuing or retaining a bearer."""

        try:
            self._service.authorize(
                principal=self._principal,
                agent_id=self._agent_id,
                agent_revision_id=self._agent_revision_id,
                binding=self._binding,
                canonical_tool_name=canonical_tool_name,
            )
        except McpDelegationError:
            return False
        return True

    def credential_for(self, canonical_tool_name: str) -> MCPRequestCredential:
        decision = self._service.authorize(
            principal=self._principal,
            agent_id=self._agent_id,
            agent_revision_id=self._agent_revision_id,
            binding=self._binding,
            canonical_tool_name=canonical_tool_name,
        )
        requested_claims = frozenset({"enterprise_subject"}) if decision.policy.enterprise_subject is not None else frozenset()
        try:
            issued = self._service.issuer.issue(
                McpAccessTokenRequest(
                    principal=self._principal,
                    agent_id=self._agent_id,
                    resource_name=self._binding.resource_name,
                    requested_scopes=decision.policy.required_scopes,
                    requested_claims=requested_claims,
                ),
                McpAccessGrant(allowed_scopes=decision.grant.allowed_scopes),
            )
        except McpTokenIssuanceError as exc:
            raise McpDelegationError(DelegationErrorCode.TOKEN_ISSUANCE_FAILED) from exc
        return MCPRequestCredential(
            bearer=issued.compact,
            resource_name=self._binding.resource_name,
            canonical_tool_name=canonical_tool_name,
            policy_revision=self._service.tool_policy.policy_revision,
            credential_generation=self._service.grant_policy.credential_generation,
            effect=decision.policy.effect,
            replay_mode=decision.policy.replay_mode,
        )

    def interaction_authorization(
        self,
        canonical_tool_name: str,
    ) -> McpInteractionAuthorization:
        decision = self._service.authorize(
            principal=self._principal,
            agent_id=self._agent_id,
            agent_revision_id=self._agent_revision_id,
            binding=self._binding,
            canonical_tool_name=canonical_tool_name,
        )
        return McpInteractionAuthorization(
            effect=decision.policy.effect,
            replay_mode=decision.policy.replay_mode,
            policy_revision=self._service.tool_policy.policy_revision,
            credential_generation=self._service.grant_policy.credential_generation,
        )


class McpDelegationService:
    """Evaluate immutable P3 authority and issue one bearer per operation."""

    def __init__(
        self,
        *,
        tool_policy: ToolPolicySnapshot,
        grant_policy: GrantPolicySnapshot,
        issuer: _TokenIssuer,
    ) -> None:
        if grant_policy.policy_revision != tool_policy.policy_revision:
            raise McpDelegationError(DelegationErrorCode.SNAPSHOT_INVALID)
        self.tool_policy = tool_policy
        self.grant_policy = grant_policy
        self.issuer = issuer
        self._decision_cache: dict[tuple[object, ...], _AuthorizationDecision] = {}
        self._decision_cache_lock = Lock()

    def bind(
        self,
        *,
        mcp_server: Any,
        run_context: RunContext | None,
    ) -> BoundMcpCredentialProvider | None:
        server_id = str(getattr(mcp_server, "id", ""))
        binding = self.grant_policy.bindings.get(server_id)
        if binding is None:
            return None
        if run_context is None or run_context.principal is None or run_context.agent_id is None or run_context.agent_revision_id is None:
            raise McpDelegationError(DelegationErrorCode.CONTEXT_REQUIRED)
        if getattr(mcp_server, "tenant_id", None) != run_context.tenant_id:
            raise McpDelegationError(DelegationErrorCode.SERVER_TENANT_MISMATCH)
        if getattr(mcp_server, "server_type", None) != MCPServerType.STREAMABLE_HTTP:
            raise McpDelegationError(DelegationErrorCode.TRANSPORT_NOT_SUPPORTED)
        headers = getattr(mcp_server, "headers", None) or {}
        if type(headers) is not dict or any(str(name).casefold() == "authorization" for name in headers):
            raise McpDelegationError(DelegationErrorCode.STATIC_AUTH_CONFLICT)
        issuer_audience = self.issuer.resource_audience(binding.resource_name)
        if issuer_audience != binding.audience or str(getattr(mcp_server, "url", "")).strip() != binding.audience:
            raise McpDelegationError(DelegationErrorCode.SERVER_AUDIENCE_MISMATCH)
        grant_key = (
            run_context.tenant_id,
            run_context.principal.platform_user_id,
            run_context.agent_id,
            run_context.agent_revision_id,
            binding.resource_name,
        )
        if grant_key not in self.grant_policy.grants:
            raise McpDelegationError(DelegationErrorCode.GRANT_NOT_FOUND)
        return BoundMcpCredentialProvider(
            _service=self,
            _binding=binding,
            _principal=run_context.principal,
            _agent_id=run_context.agent_id,
            _agent_revision_id=run_context.agent_revision_id,
        )

    def authorize(
        self,
        *,
        principal: Principal,
        agent_id: str,
        agent_revision_id: str,
        binding: DelegatedServerBinding,
        canonical_tool_name: str,
    ) -> _AuthorizationDecision:
        policy = self.tool_policy.tools.get(canonical_tool_name)
        if policy is None:
            raise McpDelegationError(DelegationErrorCode.TOOL_POLICY_NOT_FOUND)
        cache_key: tuple[object, ...] = (
            principal.tenant_id,
            principal.platform_user_id,
            agent_id,
            agent_revision_id,
            binding.mcp_server_id,
            binding.resource_name,
            canonical_tool_name,
            tuple(sorted(policy.required_scopes)),
            self.tool_policy.policy_revision,
            self.grant_policy.grant_revision,
            self.grant_policy.credential_generation,
        )
        with self._decision_cache_lock:
            decision = self._decision_cache.get(cache_key)
            if decision is None:
                decision = self._authorize_uncached(
                    tenant_id=principal.tenant_id,
                    platform_user_id=principal.platform_user_id,
                    agent_id=agent_id,
                    agent_revision_id=agent_revision_id,
                    mcp_server_id=binding.mcp_server_id,
                    resource_name=binding.resource_name,
                    canonical_tool_name=canonical_tool_name,
                )
                if len(self._decision_cache) >= 4096:
                    self._decision_cache.pop(next(iter(self._decision_cache)))
                self._decision_cache[cache_key] = decision
        # Authentication assurance is request evidence, not immutable grant
        # authority. Re-evaluate it even when the grant/scope decision is cached.
        self._verify_assurance(principal=principal, policy=decision.policy)
        return decision

    def _authorize_uncached(
        self,
        *,
        tenant_id: str,
        platform_user_id: str,
        agent_id: str,
        agent_revision_id: str,
        mcp_server_id: str,
        resource_name: str,
        canonical_tool_name: str,
    ) -> _AuthorizationDecision:
        binding = self.grant_policy.bindings.get(mcp_server_id)
        if binding is None or binding.resource_name != resource_name:
            raise McpDelegationError(DelegationErrorCode.SERVER_NOT_BOUND)
        policy = self.tool_policy.tools.get(canonical_tool_name)
        if policy is None:
            raise McpDelegationError(DelegationErrorCode.TOOL_POLICY_NOT_FOUND)
        grant = self.grant_policy.grants.get(
            (tenant_id, platform_user_id, agent_id, agent_revision_id, resource_name),
        )
        if grant is None:
            raise McpDelegationError(DelegationErrorCode.GRANT_NOT_FOUND)
        if not policy.required_scopes.issubset(grant.allowed_scopes):
            raise McpDelegationError(DelegationErrorCode.SCOPE_DENIED)
        return _AuthorizationDecision(policy=policy, grant=grant)

    @staticmethod
    def _verify_assurance(*, principal: Principal, policy: DelegatedToolPolicy) -> None:
        if policy.required_amr:
            # A2 deliberately does not invent AMR. Until a verified AMR source
            # exists, a tool requiring one cannot receive a delegated token.
            raise McpDelegationError(DelegationErrorCode.ASSURANCE_DENIED)
        if policy.accepted_acr_values and (principal.authentication.assurance is not IdentityAssurance.ENTERPRISE_VERIFIED or ENTERPRISE_ACR not in policy.accepted_acr_values):
            raise McpDelegationError(DelegationErrorCode.ASSURANCE_DENIED)
        requirement = policy.enterprise_subject
        if requirement is None:
            return
        subject = principal.enterprise_subject
        if subject is None or subject.subject_type != requirement.subject_type or subject.issuer != requirement.issuer or subject.issuer_tenant != requirement.issuer_tenant:
            raise McpDelegationError(DelegationErrorCode.ASSURANCE_DENIED)


__all__ = [
    "BoundMcpCredentialProvider",
    "McpDelegationService",
    "McpInteractionAuthorization",
]
