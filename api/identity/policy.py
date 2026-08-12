"""Provider-neutral tenant provisioning policy."""

from __future__ import annotations

from api.identity.contracts import ProvisioningAction, ProvisioningDecision, ProvisioningMode


def decide_provisioning(mode: ProvisioningMode) -> ProvisioningDecision:
    if mode is ProvisioningMode.PREPROVISIONED:
        return ProvisioningDecision(action=ProvisioningAction.BIND_PREPROVISIONED)
    if mode is ProvisioningMode.LINK_ONLY:
        return ProvisioningDecision(action=ProvisioningAction.REQUIRE_LINK)
    if mode is ProvisioningMode.JIT:
        return ProvisioningDecision(
            action=ProvisioningAction.CREATE_NORMAL_MEMBER,
            member_role="normal",
        )
    raise ValueError("unsupported provisioning mode")
