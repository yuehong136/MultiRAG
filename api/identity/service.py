"""Framework-neutral local identity resolution orchestration."""

from __future__ import annotations

from api.identity.contracts import (
    IdentityErrorCode,
    IdentityLookupRepository,
    IdentityResolutionRequest,
    IdentityResolutionResult,
    IdentityResolutionStatus,
    ProvisioningAction,
    ProvisioningPolicyResolver,
)
from api.identity.policy import decide_provisioning
from api.identity.validation import (
    valid_alias_key,
    valid_policy_snapshot,
    valid_provider_context,
)


class IdentityService:
    def __init__(
        self,
        repository: IdentityLookupRepository,
        policy_resolver: ProvisioningPolicyResolver,
    ) -> None:
        self._repository = repository
        self._policy_resolver = policy_resolver

    async def resolve_external_identity(
        self,
        request: IdentityResolutionRequest,
    ) -> IdentityResolutionResult:
        context = request.context
        if not valid_provider_context(context) or not valid_alias_key(request.alias):
            return IdentityResolutionResult(
                status=IdentityResolutionStatus.CONFLICT,
                error_code=IdentityErrorCode.ASSERTION_INVALID,
            )

        snapshot = await self._repository.resolve_identity(request)
        if snapshot is None:
            return IdentityResolutionResult(
                status=IdentityResolutionStatus.CONFLICT,
                error_code=IdentityErrorCode.TENANT_MISMATCH,
            )
        if snapshot.account.context() != context:
            return IdentityResolutionResult(
                status=IdentityResolutionStatus.CONFLICT,
                error_code=IdentityErrorCode.PROVIDER_MISMATCH,
            )
        if snapshot.account.identity_health_state != "healthy":
            return IdentityResolutionResult(
                status=IdentityResolutionStatus.INACTIVE,
                error_code=IdentityErrorCode.INACTIVE,
            )
        if snapshot.identity is None:
            try:
                policy = await self._policy_resolver.get_policy(context.tenant_id)
                if not valid_policy_snapshot(policy, tenant_id=context.tenant_id):
                    raise ValueError("provisioning policy is unavailable")
                decision = decide_provisioning(policy.mode)
            except Exception:
                return IdentityResolutionResult(
                    status=IdentityResolutionStatus.CONFLICT,
                    error_code=IdentityErrorCode.POLICY_UNAVAILABLE,
                )
            return IdentityResolutionResult(
                status=IdentityResolutionStatus.MISSING,
                error_code=(IdentityErrorCode.LINK_REQUIRED if decision.action is ProvisioningAction.REQUIRE_LINK else None),
                provisioning_action=decision.action,
                provisioning_policy_revision=policy.revision,
                provider_verification_required=True,
            )

        identity = snapshot.identity
        if identity.tenant_id != context.tenant_id or identity.provider != context.provider or identity.provider_tenant_key != context.provider_tenant_key:
            return IdentityResolutionResult(
                status=IdentityResolutionStatus.CONFLICT,
                error_code=IdentityErrorCode.TENANT_MISMATCH,
            )
        if identity.state == "conflict":
            return IdentityResolutionResult(
                status=IdentityResolutionStatus.CONFLICT,
                error_code=IdentityErrorCode.LINK_CONFLICT,
            )
        if identity.state == "revoked":
            return IdentityResolutionResult(
                status=IdentityResolutionStatus.INACTIVE,
                error_code=IdentityErrorCode.INACTIVE,
            )
        if snapshot.account.last_scope_change_at is not None and (snapshot.alias_verified_at is None or snapshot.alias_verified_at < snapshot.account.last_scope_change_at):
            return IdentityResolutionResult(
                status=IdentityResolutionStatus.INACTIVE,
                error_code=IdentityErrorCode.INACTIVE,
                provider_verification_required=True,
            )
        if identity.state != "active" or snapshot.membership is None:
            return IdentityResolutionResult(
                status=IdentityResolutionStatus.INACTIVE,
                error_code=IdentityErrorCode.INACTIVE,
            )
        if snapshot.membership.tenant_id != context.tenant_id or snapshot.membership.user_id != identity.user_id or snapshot.membership.role not in {"owner", "admin", "normal"}:
            return IdentityResolutionResult(
                status=IdentityResolutionStatus.INACTIVE,
                error_code=IdentityErrorCode.INACTIVE,
            )
        return IdentityResolutionResult(
            status=IdentityResolutionStatus.RESOLVED,
            identity=identity,
            membership=snapshot.membership,
        )
