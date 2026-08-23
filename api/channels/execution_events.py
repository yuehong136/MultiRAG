"""Typed, transport-neutral execution events consumed by Channel workers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, TypeAlias


@dataclass(frozen=True, slots=True)
class MessageDeltaEvent:
    """One ordered fragment of user-visible answer content."""

    content: str
    session_id: str | None = None
    event: Literal["message_delta"] = field(default="message_delta", init=False)


@dataclass(frozen=True, slots=True)
class MessageCompletedEvent:
    """Successful terminal event with an optional authoritative answer."""

    session_id: str
    content: str | None = None
    event: Literal["message_completed"] = field(default="message_completed", init=False)


@dataclass(frozen=True, slots=True)
class ExecutionFailedEvent:
    """Terminal failure containing only a stable, sanitized error code."""

    error_code: str
    session_id: str | None = None
    event: Literal["execution_failed"] = field(default="execution_failed", init=False)


@dataclass(frozen=True, slots=True)
class InteractionRequiredEvent:
    """Terminal handoff to one API-owned, revision-bound form delivery."""

    action_id: str = field(repr=False)
    revision: int
    expires_at: datetime
    event: Literal["interaction_required"] = field(
        default="interaction_required",
        init=False,
    )


BindingExecutionEvent: TypeAlias = MessageDeltaEvent | MessageCompletedEvent | ExecutionFailedEvent | InteractionRequiredEvent
