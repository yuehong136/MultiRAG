"""Pure capability contracts shared by Channel control and execution planes."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

CommitMode = Literal["detached_cas", "candidate_cas"]
EffectClass = Literal["generation_only", "external_effects", "unknown"]
RegenerationMode = Literal["always", "conditional", "never"]


class ProviderCapabilities(BaseModel):
    """Transport and interaction features implemented by one Provider."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    private_chat: bool
    group_chat: bool
    text: bool
    files: bool
    images: bool
    streaming_cards: bool
    progressive_reply: bool = False
    interactive_actions: bool = False
    cancel_control: bool = False
    feedback_control: bool = False
    threaded_reply: bool = False


class TargetCapabilities(BaseModel):
    """Execution and persistence facts declared by a MultiRAG target driver."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    streaming: bool
    cancellable: bool
    regeneration: RegenerationMode
    retryable: bool
    feedback: bool
    commit_mode: CommitMode
    effect_class: EffectClass


class RunCapabilityPolicy(BaseModel):
    """Binding policy gates applied after Provider and target capabilities."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    progressive_reply: bool = True
    cancel: bool = True
    regenerate: bool = True
    retry: bool = True
    feedback: bool = True

    @classmethod
    def from_binding_policy(cls, policy: Mapping[str, Any]) -> RunCapabilityPolicy:
        """Read the optional capability namespace without widening on bad data."""

        raw = policy.get("reply_capabilities")
        if raw is None:
            return cls()
        if not isinstance(raw, Mapping):
            return cls(
                progressive_reply=False,
                cancel=False,
                regenerate=False,
                retry=False,
                feedback=False,
            )
        values: dict[str, bool] = {}
        for field in ("progressive_reply", "cancel", "regenerate", "retry", "feedback"):
            value = raw.get(field, True)
            values[field] = value if isinstance(value, bool) else False
        return cls(**values)


class EffectiveReplyCapabilities(BaseModel):
    """Minimal sanitized capability envelope consumed by a Provider worker."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    progressive_reply: bool = False
    cancel_queued: bool = False
    cancel_running: bool = False
    regenerate: bool = False
    retry: bool = False
    feedback: bool = False


class ChannelRuntimeCapabilities(EffectiveReplyCapabilities):
    """Additive worker preflight envelope with fail-closed server features."""

    identity_event_receipt: bool = False
    interaction_delivery: bool = False

    def to_reply_capabilities(self) -> EffectiveReplyCapabilities:
        """Keep non-reply server features out of reply rendering contracts."""

        return EffectiveReplyCapabilities(
            progressive_reply=self.progressive_reply,
            cancel_queued=self.cancel_queued,
            cancel_running=self.cancel_running,
            regenerate=self.regenerate,
            retry=self.retry,
            feedback=self.feedback,
        )


def parse_effective_reply_capabilities(payload: object) -> EffectiveReplyCapabilities:
    """Validate known fields while tolerating future additive wire features."""

    if not isinstance(payload, Mapping):
        raise TypeError("Capability payload must be an object")
    known = {field: payload[field] for field in EffectiveReplyCapabilities.model_fields if field in payload}
    return EffectiveReplyCapabilities.model_validate(known)


def parse_channel_runtime_capabilities(payload: object) -> ChannelRuntimeCapabilities:
    """Accept old additive responses while defaulting server features off."""

    if not isinstance(payload, Mapping):
        raise TypeError("Capability payload must be an object")
    known = {field: payload[field] for field in ChannelRuntimeCapabilities.model_fields if field in payload}
    return ChannelRuntimeCapabilities.model_validate(known)


def resolve_effective_reply_capabilities(
    provider: ProviderCapabilities,
    target: TargetCapabilities,
    policy: RunCapabilityPolicy,
) -> EffectiveReplyCapabilities:
    """Intersect independent declarations without Provider-target branches."""

    actions = provider.interactive_actions
    cancel = actions and provider.cancel_control and policy.cancel
    return EffectiveReplyCapabilities(
        progressive_reply=provider.progressive_reply and target.streaming and policy.progressive_reply,
        cancel_queued=cancel,
        cancel_running=cancel and target.cancellable,
        # ``conditional`` is a declaration that the target still owes us a
        # run-specific answer.  Only a resolved ``always`` may create a replay
        # control; otherwise an unknown graph would fail open.
        regenerate=actions and target.regeneration == "always" and policy.regenerate,
        retry=actions and target.retryable and policy.retry,
        feedback=actions and provider.feedback_control and target.feedback and policy.feedback,
    )
