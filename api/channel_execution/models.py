"""Strongly typed messages for the trusted Channel execution boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from api.identity.principal import AuthenticationSource, Principal

TargetType = Literal["multirag.canvas_agent", "multirag.dialog"]
ExecutionOperation = Literal["message", "regenerate"]
ExecutionEventType = Literal[
    "message_delta",
    "message_completed",
    "execution_failed",
    "interaction_required",
]


class ExecutionTargetRef(BaseModel):
    """A target loaded from trusted binding state, never from a Channel request."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    target_type: TargetType
    target_id: str = Field(min_length=1, max_length=255)
    revision_id: str | None = Field(default=None, min_length=1, max_length=255)


class ChannelMessage(BaseModel):
    """Normalized external message accepted by the first execution API version."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["text"] = "text"
    content: str = Field(min_length=1, max_length=4000)


class ExternalIdentityIdentifier(BaseModel):
    """One bounded, untrusted identifier supplied by a Channel adapter."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: str = Field(min_length=1, max_length=64)
    value: str = Field(min_length=1, max_length=255, repr=False)

    @model_validator(mode="after")
    def reject_blank_text(self) -> ExternalIdentityIdentifier:
        if not self.kind.strip() or self.kind != self.kind.strip() or not self.value.strip() or self.value != self.value.strip():
            raise ValueError("external identity identifier cannot be blank")
        return self


class ExternalIdentityAssertion(BaseModel):
    """Structured Provider identity material that is not yet a Principal."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str = Field(min_length=1, max_length=64)
    provider_tenant_key: str | None = Field(
        default=None,
        min_length=1,
        max_length=255,
        repr=False,
    )
    identifiers: tuple[ExternalIdentityIdentifier, ...] = Field(
        min_length=1,
        max_length=8,
        repr=False,
    )

    @model_validator(mode="after")
    def validate_identity_material(self) -> ExternalIdentityAssertion:
        if (
            not self.provider.strip()
            or self.provider != self.provider.strip()
            or (self.provider_tenant_key is not None and (not self.provider_tenant_key.strip() or self.provider_tenant_key != self.provider_tenant_key.strip()))
        ):
            raise ValueError("external identity assertion cannot be blank")
        kinds = [identifier.kind for identifier in self.identifiers]
        if len(kinds) != len(set(kinds)):
            raise ValueError("external identity identifier kinds must be unique")
        return self


class ChannelActor(BaseModel):
    """Untrusted external identity assertions supplied by a Channel adapter."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str = Field(min_length=1, max_length=64)
    subject: str = Field(min_length=1, max_length=255)
    conversation: str = Field(min_length=1, max_length=255)
    identity: ExternalIdentityAssertion | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        repr=False,
    )

    @model_validator(mode="after")
    def validate_identity_provider(self) -> ChannelActor:
        if self.identity is not None and self.identity.provider != self.provider:
            raise ValueError("structured identity provider must match legacy actor provider")
        return self


class ChannelExecutionCommand(BaseModel):
    """The complete untrusted body accepted from a Channel runtime.

    Tenant, target, revision, session and permission fields are deliberately not
    present. ``extra='forbid'`` prevents a caller from smuggling them into the
    trusted execution context.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str = Field(min_length=1, max_length=255)
    conversation_key: str = Field(min_length=1, max_length=512)
    operation: ExecutionOperation = "message"
    message: ChannelMessage
    actor: ChannelActor
    # Provider message/card locator only.  It is never treated as authority;
    # the API binds it to the trusted binding, Principal and interaction row.
    presentation_ref: str | None = Field(
        default=None,
        min_length=1,
        max_length=255,
        repr=False,
    )


@dataclass(frozen=True, slots=True)
class WorkloadIdentity:
    """Authenticated identity of a Channel runtime process."""

    subject: str
    binding_id: str | None = None
    binding_generation: int | None = None


@dataclass(frozen=True, slots=True)
class TrustedChannelContext:
    """Server-resolved binding state used to authorize one execution."""

    binding_id: str
    tenant_id: str
    target: ExecutionTargetRef
    enabled: bool
    binding_generation: int
    provider: str = ""
    run_policy: dict[str, Any] = field(default_factory=dict)
    principal_id: str | None = field(default=None, repr=False)
    principal: Principal | None = field(default=None, repr=False)
    session_id: str | None = None

    def __post_init__(self) -> None:
        if self.principal is None:
            if self.principal_id is not None:
                raise ValueError("channel principal context is inconsistent")
            return
        if (
            self.principal_id != self.principal.platform_user_id
            or self.tenant_id != self.principal.tenant_id
            or self.principal.authentication.source is not AuthenticationSource.ENTERPRISE_IDENTITY
            or self.principal.authentication.provider != self.provider
        ):
            raise ValueError("channel principal context is inconsistent")


class ExecutionEvent(BaseModel):
    """Sanitized event contract exposed to Channel runtimes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event: ExecutionEventType
    content: str | None = None
    session_id: str | None = None
    error_code: str | None = None
    action_id: str | None = Field(default=None, min_length=1, max_length=32, repr=False)
    revision: int | None = Field(default=None, gt=0)
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def validate_interaction_shape(self) -> ExecutionEvent:
        interaction_values = (self.action_id, self.revision, self.expires_at)
        if self.event == "interaction_required":
            if (
                self.action_id is None
                or self.revision is None
                or self.content is not None
                or self.session_id is not None
                or self.error_code is not None
                or (self.expires_at is not None and (self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None))
            ):
                raise ValueError("interaction_required event is invalid")
        elif any(value is not None for value in interaction_values):
            raise ValueError("interaction fields require interaction_required")
        return self
