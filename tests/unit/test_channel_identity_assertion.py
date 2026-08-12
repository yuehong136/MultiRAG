"""Security contracts for the tolerate-only Channel identity assertion step."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from api.channel_execution.adapters import SqlAlchemyBindingResolver
from api.channel_execution.models import (
    ChannelActor,
    ChannelExecutionCommand,
    ChannelMessage,
    ExternalIdentityAssertion,
    ExternalIdentityIdentifier,
    WorkloadIdentity,
)


class _Repository:
    async def get_runtime_binding(
        self,
        binding_id: str,
        *,
        for_update: bool = False,
    ) -> tuple[SimpleNamespace, SimpleNamespace, None] | None:
        del for_update
        if binding_id != "binding-trusted":
            return None
        channel = SimpleNamespace(
            id="channel-trusted",
            tenant_id="tenant-trusted",
            channel="feishu",
            status=1,
        )
        binding = SimpleNamespace(
            id="binding-trusted",
            channel_id="channel-trusted",
            target_type="multirag.canvas_agent",
            target_id="target-trusted",
            target_revision_id="revision-trusted",
            enabled=True,
            generation=7,
            policy={},
        )
        return channel, binding, None


def _command(identity: ExternalIdentityAssertion | None) -> ChannelExecutionCommand:
    return ChannelExecutionCommand(
        event_id="event-external",
        conversation_key="conversation-external",
        message=ChannelMessage(content="hello"),
        actor=ChannelActor(
            provider="feishu",
            subject="legacy-external-subject",
            conversation="legacy-external-conversation",
            identity=identity,
        ),
    )


@pytest.mark.asyncio
async def test_binding_resolver_does_not_consume_structured_identity_or_change_authority() -> None:
    resolver = SqlAlchemyBindingResolver(_Repository())  # type: ignore[arg-type]
    workload = WorkloadIdentity(
        subject="runtime-trusted",
        binding_id="binding-trusted",
        binding_generation=7,
    )
    assertion = ExternalIdentityAssertion(
        provider="feishu",
        provider_tenant_key="tenant-attacker-sentinel",
        identifiers=(
            ExternalIdentityIdentifier(
                kind="provider_future_id",
                value="principal-attacker-sentinel",
            ),
        ),
    )
    assert "tenant-attacker-sentinel" not in repr(assertion)
    assert "principal-attacker-sentinel" not in repr(assertion)

    legacy_context = await resolver.resolve(
        binding_id="binding-trusted",
        workload=workload,
        command=_command(None),
    )
    structured_context = await resolver.resolve(
        binding_id="binding-trusted",
        workload=workload,
        command=_command(assertion),
    )

    assert structured_context == legacy_context
    assert structured_context is not None
    assert structured_context.tenant_id == "tenant-trusted"
    assert structured_context.target.target_id == "target-trusted"
    assert structured_context.target.revision_id == "revision-trusted"
    assert "attacker-sentinel" not in repr(structured_context)
