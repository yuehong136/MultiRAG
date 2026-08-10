"""HTTP contracts for generation-scoped Channel execution capabilities."""

from __future__ import annotations

from api.channel_capabilities import TargetCapabilities
from api.channel_execution.dependencies import (
    DenyAllWorkloadAuthenticator,
    get_binding_capability_resolver,
    get_published_target_execution_service,
    require_channel_workload,
)
from api.channel_execution.models import ExecutionTargetRef, TrustedChannelContext, WorkloadIdentity


class _CapabilityResolver:
    def __init__(self, context: TrustedChannelContext | None) -> None:
        self.context = context
        self.calls: list[tuple[str, WorkloadIdentity]] = []

    async def resolve_capabilities(
        self,
        *,
        binding_id: str,
        workload: WorkloadIdentity,
    ) -> TrustedChannelContext | None:
        self.calls.append((binding_id, workload))
        return self.context


class _TargetService:
    async def capabilities(
        self,
        *,
        context: TrustedChannelContext,
    ) -> TargetCapabilities:
        assert context.target.target_id == "canvas-private-id"
        return TargetCapabilities(
            streaming=True,
            cancellable=True,
            regeneration="always",
            retryable=True,
            feedback=True,
            commit_mode="candidate_cas",
            effect_class="unknown",
        )


def _authenticate() -> WorkloadIdentity:
    return WorkloadIdentity(
        subject="runner-unit",
        binding_id="binding-1",
        binding_generation=7,
    )


def _context() -> TrustedChannelContext:
    return TrustedChannelContext(
        binding_id="binding-1",
        tenant_id="tenant-private",
        target=ExecutionTargetRef(
            target_type="multirag.canvas_agent",
            target_id="canvas-private-id",
            revision_id="revision-private-id",
        ),
        enabled=True,
        binding_generation=7,
        provider="feishu",
        run_policy={"reply_capabilities": {"feedback": False}},
    )


def test_capability_preflight_requires_binding_scoped_workload(client) -> None:
    client.app.dependency_overrides[require_channel_workload] = DenyAllWorkloadAuthenticator().authenticate

    response = client.get("/api/v1/internal/channel-bindings/binding-1/execution-capabilities")

    assert response.status_code == 401


def test_capability_preflight_returns_only_sanitized_effective_features(client) -> None:
    resolver = _CapabilityResolver(_context())
    client.app.dependency_overrides[require_channel_workload] = _authenticate
    client.app.dependency_overrides[get_binding_capability_resolver] = lambda: resolver
    client.app.dependency_overrides[get_published_target_execution_service] = _TargetService

    response = client.get("/api/v1/internal/channel-bindings/binding-1/execution-capabilities")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert response.json() == {
        "progressive_reply": True,
        "cancel_queued": True,
        "cancel_running": True,
        "regenerate": True,
        "retry": True,
        "feedback": False,
    }
    serialized = response.text
    for forbidden in (
        "tenant-private",
        "canvas-private-id",
        "revision-private-id",
        "target_type",
        "commit_mode",
        "effect_class",
    ):
        assert forbidden not in serialized
    assert resolver.calls == [("binding-1", _authenticate())]
