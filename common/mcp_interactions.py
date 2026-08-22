"""Transport-neutral EIM-U14 contracts at the RAGFlow/MultiRAG seam.

This module deliberately knows nothing about SQLAlchemy, FastAPI, Channel
providers, or identity repositories. Upstream-facing MCP code can emit one
immutable request while the MultiRAG identity layer owns persistence,
authorization, leasing, and rendering.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

_OPAQUE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")
_REPLAY_MODES = frozenset({"reusable", "single_use"})


class InteractionEffect(StrEnum):
    READ = "read"
    PREPARE = "prepare"
    SIDE_EFFECT = "side_effect"


def decode_interaction_payload_key(encoded_key: str) -> bytes:
    """Strictly decode one canonical URL-safe base64 AES-256 key."""

    candidate = encoded_key.strip()
    padding = "=" * (-len(candidate) % 4)
    try:
        raw = base64.b64decode(
            (candidate + padding).encode("ascii"),
            altchars=b"-_",
            validate=True,
        )
    except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
        raise ValueError("interaction payload key must be canonical URL-safe base64") from exc
    if len(raw) != 32:
        raise ValueError("interaction payload key must decode to 32 bytes")
    if base64.urlsafe_b64encode(raw).decode().rstrip("=") != candidate.rstrip("="):
        raise ValueError("interaction payload key must be canonical URL-safe base64")
    return raw


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("interaction request is invalid") from exc


def canonical_call_digest(
    *,
    mcp_server_id: str,
    resource_name: str,
    resource_uri: str,
    tool_name: str,
    arguments: Mapping[str, Any],
) -> str:
    """Bind the exact server/resource/tool/arguments logical call."""

    envelope = {
        "arguments": dict(arguments),
        "mcp_server_id": mcp_server_id,
        "resource_name": resource_name,
        "resource_uri": resource_uri,
        "tool_name": tool_name,
    }
    return hashlib.sha256(
        ("multirag.mcp-interaction.call.v1\x00" + _canonical_json(envelope)).encode(),
    ).hexdigest()


def _valid_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 2048


def _valid_opaque(value: object) -> bool:
    return isinstance(value, str) and _OPAQUE_ID.fullmatch(value) is not None


@dataclass(frozen=True, slots=True)
class InteractionRequest:
    """Sensitive per-round request handed to the durable Host implementation."""

    tenant_id: str = field(repr=False)
    platform_user_id: str = field(repr=False)
    external_identity_id: str | None = field(default=None, repr=False)
    identity_revision: int | None = field(default=None, repr=False)
    agent_id: str = ""
    agent_revision_id: str = ""
    mcp_server_id: str = ""
    resource_name: str = ""
    resource_uri: str = field(default="", repr=False)
    tool_name: str = ""
    original_arguments: Mapping[str, Any] = field(default_factory=dict, repr=False)
    input_requests: Mapping[str, Any] = field(default_factory=dict, repr=False)
    output_schema: Mapping[str, Any] | None = field(default=None, repr=False)
    request_state: str = field(default="", repr=False)
    effect: InteractionEffect = InteractionEffect.READ
    replay_mode: str = "reusable"
    policy_revision: str = ""
    credential_generation: int = 0
    expires_at: datetime | None = field(default=None, repr=False)
    interaction_id: str | None = field(default=None, repr=False)
    previous_revision: int | None = None
    call_digest: str = field(init=False)

    def __post_init__(self) -> None:
        required_opaque = (
            self.tenant_id,
            self.platform_user_id,
            self.agent_id,
            self.agent_revision_id,
            self.mcp_server_id,
            self.resource_name,
            self.tool_name,
            self.policy_revision,
        )
        valid = (
            all(_valid_opaque(item) for item in required_opaque)
            and _valid_text(self.resource_uri)
            and isinstance(self.original_arguments, Mapping)
            and isinstance(self.input_requests, Mapping)
            and bool(self.input_requests)
            and (self.output_schema is None or isinstance(self.output_schema, Mapping))
            and _valid_text(self.request_state)
            and type(self.effect) is InteractionEffect
            and self.effect is not InteractionEffect.SIDE_EFFECT
            and self.replay_mode in _REPLAY_MODES
            and type(self.credential_generation) is int
            and self.credential_generation >= 0
            and isinstance(self.expires_at, datetime)
            and self.expires_at.tzinfo is not None
            and self.expires_at.utcoffset() is not None
            and (self.external_identity_id is None or _valid_opaque(self.external_identity_id))
            and (self.identity_revision is None or (type(self.identity_revision) is int and self.identity_revision > 0))
            and ((self.interaction_id is None and self.previous_revision is None) or (_valid_opaque(self.interaction_id) and type(self.previous_revision) is int and self.previous_revision > 0))
        )
        if not valid:
            raise ValueError("interaction request is invalid")
        _canonical_json(dict(self.original_arguments))
        _canonical_json(dict(self.input_requests))
        if self.output_schema is not None:
            _canonical_json(dict(self.output_schema))
        object.__setattr__(
            self,
            "call_digest",
            canonical_call_digest(
                mcp_server_id=self.mcp_server_id,
                resource_name=self.resource_name,
                resource_uri=self.resource_uri,
                tool_name=self.tool_name,
                arguments=self.original_arguments,
            ),
        )


@dataclass(frozen=True, slots=True)
class InteractionReceipt:
    interaction_id: str = field(repr=False)
    revision: int

    def __post_init__(self) -> None:
        if not _valid_opaque(self.interaction_id) or type(self.revision) is not int or self.revision <= 0:
            raise ValueError("interaction receipt is invalid")


@dataclass(frozen=True, slots=True)
class InteractionResume:
    interaction_id: str = field(repr=False)
    revision: int
    request: InteractionRequest = field(repr=False)
    input_responses: Mapping[str, Any] = field(repr=False)
    call_digest: str = field(init=False)

    def __post_init__(self) -> None:
        if (
            not _valid_opaque(self.interaction_id)
            or type(self.revision) is not int
            or self.revision <= 0
            or not isinstance(self.request, InteractionRequest)
            or not isinstance(self.input_responses, Mapping)
            or not self.input_responses
        ):
            raise ValueError("interaction resume is invalid")
        _canonical_json(dict(self.input_responses))
        object.__setattr__(self, "call_digest", self.request.call_digest)


class InteractionHandler(Protocol):
    async def pause(self, request: InteractionRequest) -> InteractionReceipt: ...


class MCPInteractionPaused(BaseException):
    """Control signal that bypasses upstream ``except Exception`` tool adapters."""

    __slots__ = ("interaction_id", "revision")

    def __init__(self, *, interaction_id: str, revision: int) -> None:
        receipt = InteractionReceipt(interaction_id=interaction_id, revision=revision)
        self.interaction_id = receipt.interaction_id
        self.revision = receipt.revision
        super().__init__("MCP interaction paused")

    def __repr__(self) -> str:
        return "MCPInteractionPaused(<redacted>)"


__all__ = [
    "InteractionEffect",
    "InteractionHandler",
    "InteractionReceipt",
    "InteractionRequest",
    "InteractionResume",
    "MCPInteractionPaused",
    "canonical_call_digest",
    "decode_interaction_payload_key",
]
