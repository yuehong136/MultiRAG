"""Durable EIM-U14 state-machine contracts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from common.mcp_interactions import InteractionResume


class InteractionErrorCode(StrEnum):
    NOT_FOUND = "INTERACTION_NOT_FOUND"
    ACTOR_MISMATCH = "INTERACTION_ACTOR_MISMATCH"
    REVISION_CONFLICT = "INTERACTION_REVISION_CONFLICT"
    STATE_CONFLICT = "INTERACTION_STATE_CONFLICT"
    EXPIRED = "INTERACTION_EXPIRED"
    RESPONSE_INVALID = "INTERACTION_RESPONSE_INVALID"
    PAYLOAD_INVALID = "INTERACTION_PAYLOAD_INVALID"
    SIDE_EFFECT_BLOCKED = "INTERACTION_SIDE_EFFECT_BLOCKED"
    REAUTHORIZATION_DENIED = "INTERACTION_REAUTHORIZATION_DENIED"
    REMOTE_RESULT_UNKNOWN = "INTERACTION_REMOTE_RESULT_UNKNOWN"
    ROUND_LIMIT = "INTERACTION_ROUND_LIMIT"


class InteractionStateError(RuntimeError):
    """Non-sensitive durable interaction rejection."""

    def __init__(self, code: InteractionErrorCode) -> None:
        self.code = code
        super().__init__("MCP interaction operation rejected")


class ResponseClaimStatus(StrEnum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"


@dataclass(frozen=True, slots=True)
class ResponseClaim:
    interaction_id: str = field(repr=False)
    revision: int
    status: ResponseClaimStatus


@dataclass(frozen=True, slots=True)
class InteractionLease:
    job_id: str = field(repr=False)
    owner: str = field(repr=False)
    resume: InteractionResume = field(repr=False)


@dataclass(frozen=True, slots=True)
class InteractionProjection:
    """Renderer-safe persistent projection; continuation and arguments omitted."""

    interaction_id: str = field(repr=False)
    revision: int
    state: str
    input_requests: Mapping[str, Any] = field(repr=False)
    expires_at: datetime


__all__ = [
    "InteractionErrorCode",
    "InteractionLease",
    "InteractionProjection",
    "InteractionStateError",
    "ResponseClaim",
    "ResponseClaimStatus",
]
