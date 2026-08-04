"""Strongly typed messages for the trusted Channel execution boundary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

TargetType = Literal["multirag.canvas_agent", "multirag.dialog"]
ExecutionEventType = Literal[
    "message_delta",
    "message_completed",
    "execution_failed",
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


class ChannelActor(BaseModel):
    """Untrusted external identity assertions supplied by a Channel adapter."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str = Field(min_length=1, max_length=64)
    subject: str = Field(min_length=1, max_length=255)
    conversation: str = Field(min_length=1, max_length=255)


class ChannelExecutionCommand(BaseModel):
    """The complete untrusted body accepted from a Channel runtime.

    Tenant, target, revision, session and permission fields are deliberately not
    present. ``extra='forbid'`` prevents a caller from smuggling them into the
    trusted execution context.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str = Field(min_length=1, max_length=255)
    conversation_key: str = Field(min_length=1, max_length=512)
    message: ChannelMessage
    actor: ChannelActor


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
    principal_id: str | None = None
    session_id: str | None = None


class ExecutionEvent(BaseModel):
    """Sanitized event contract exposed to Channel runtimes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event: ExecutionEventType
    content: str | None = None
    session_id: str | None = None
    error_code: str | None = None
