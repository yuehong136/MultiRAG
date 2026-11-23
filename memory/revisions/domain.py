"""Deterministic revision validation, independent from SQL and model providers."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

MAX_CONTENT_BYTES = 65535
MAX_CONTENT_TOKENS = 8192
MAX_HISTORY_PAGE_SIZE = 100
_IDENTITY_FIELDS = ("user_id", "agent_id", "session_id")


class RevisionError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class RevisionConflict(RevisionError):
    pass


class RevisionNotFound(RevisionError):
    pass


class RevisionValidationError(RevisionError):
    pass


class RevisionCapacityError(RevisionConflict):
    pass


class RevisionBusy(RevisionConflict):
    pass


class RevisionPersistenceUnknown(RuntimeError):
    def __init__(self, operation_id: str) -> None:
        self.operation_id = operation_id
        self.code = "operation_commit_unknown"
        super().__init__(self.code)


class RevisionStorageUnknown(RuntimeError):
    """A remote mutation may have committed and requires stable-ID reconciliation."""


@dataclass(frozen=True, slots=True)
class ModelBinding:
    tenant_model_id: str | None
    model_reference: str
    dimension: int
    config_reference: str = ""

    def __post_init__(self) -> None:
        if (
            (self.tenant_model_id is not None and (not isinstance(self.tenant_model_id, str) or re.fullmatch(r"[0-9a-f]{32}", self.tenant_model_id) is None))
            or type(self.dimension) is not int
            or not 1 <= self.dimension <= 32768
            or not isinstance(self.model_reference, str)
            or not self.model_reference
            or not isinstance(self.config_reference, str)
            or len(self.config_reference) > 256
        ):
            raise RevisionValidationError("invalid_model_binding")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def positive_int(value: Any, code: str = "invalid_revision") -> int:
    if type(value) is not int or not 0 < value <= 2**63 - 1:
        raise RevisionValidationError(code)
    return value


def validate_operation_id(value: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value):
        raise RevisionValidationError("invalid_operation_id")


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def request_hash(request: dict[str, Any]) -> str:
    return content_hash(json.dumps(request, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False))


def revised_content(current: str, *, content: str | None, patch: dict[str, Any] | None, count_tokens: Callable[[str], int]) -> tuple[str, int]:
    if (content is None) == (patch is None):
        raise RevisionValidationError("content_or_patch_required")
    if patch is not None:
        if not isinstance(patch, dict) or set(patch) != {"old_text", "new_text"}:
            raise RevisionValidationError("invalid_patch")
        old, new = patch["old_text"], patch["new_text"]
        if not isinstance(old, str) or not old or not isinstance(new, str):
            raise RevisionValidationError("invalid_patch")
        start = current.find(old)
        if start < 0:
            raise RevisionConflict("patch_not_found")
        if current.find(old, start + 1) >= 0:
            raise RevisionConflict("patch_ambiguous")
        content = current[:start] + new + current[start + len(old) :]
    if not isinstance(content, str) or not content.strip():
        raise RevisionValidationError("empty_content")
    if content == current:
        raise RevisionValidationError("content_unchanged")
    if len(content.encode("utf-8")) > MAX_CONTENT_BYTES:
        raise RevisionValidationError("content_bytes_exceeded")
    tokens = count_tokens(content)
    if type(tokens) is not int or tokens < 0:
        raise RevisionValidationError("invalid_token_estimate")
    if tokens > MAX_CONTENT_TOKENS:
        raise RevisionValidationError("content_tokens_exceeded")
    return content, tokens


def message_scope(message: dict[str, Any], memory_id: str) -> dict[str, str]:
    if message.get("memory_id") != memory_id or any(not isinstance(message.get(field), str) for field in _IDENTITY_FIELDS):
        raise RevisionConflict("message_scope_mismatch")
    return {field: message[field] for field in _IDENTITY_FIELDS}


def is_active(message: dict[str, Any]) -> bool:
    return message.get("status") in (True, 1, "1") and message.get("forget_at") in (None, "", "-")


def vector_dimension(message: dict[str, Any]) -> int:
    vector: Any = message.get("content_embed")
    if hasattr(vector, "tolist"):
        vector = vector.tolist()
    if not isinstance(vector, list) or not vector or any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) for value in vector):
        raise RevisionConflict("invalid_embedding_dimension")
    return len(vector)


def validate_adoption(memory_id: str, message_id: int, message: dict[str, Any], source: dict[str, Any]) -> tuple[dict[str, str], int]:
    positive_int(message_id, "invalid_message_id")
    scope = message_scope(message, memory_id)
    if positive_int(message.get("message_id"), "invalid_message_id") != message_id:
        raise RevisionConflict("message_identity_mismatch")
    if message.get("message_type") not in {"semantic", "episodic", "procedural"}:
        raise RevisionValidationError("derived_memory_required")
    anchor = positive_int(message.get("source_id"), "raw_ancestor_required")
    if message_scope(source, memory_id) != scope or positive_int(source.get("message_id"), "invalid_source_id") != anchor:
        raise RevisionConflict("source_scope_mismatch")
    if source.get("message_type") != "raw" or source.get("source_id") not in (None, 0, "0", "") or isinstance(source.get("source_id"), bool):
        raise RevisionConflict("raw_ancestor_required")
    if not is_active(message) or not is_active(source):
        raise RevisionConflict("message_or_source_inactive")
    if not isinstance(message.get("content"), str) or not isinstance(source.get("content"), str):
        raise RevisionConflict("invalid_stored_content")
    return scope, vector_dimension(message)


def validate_projection(message: dict[str, Any], expected: dict[str, Any], binding: ModelBinding, *, active: bool) -> dict[str, Any]:
    memory_id = expected["memory_id"]
    if message_scope(message, memory_id) != message_scope(expected, memory_id):
        raise RevisionConflict("projection_scope_mismatch")
    if positive_int(message.get("message_id"), "invalid_message_id") != expected["message_id"] or message.get("message_type") != expected["message_type"]:
        raise RevisionConflict("projection_identity_mismatch")
    if message.get("id") is not None and message["id"] != f"{memory_id}_{expected['message_id']}":
        raise RevisionConflict("projection_identity_mismatch")
    if positive_int(message.get("source_id"), "invalid_source_id") != expected["source_id"]:
        raise RevisionConflict("projection_source_mismatch")
    if not isinstance(message.get("content"), str) or content_hash(message["content"]) != content_hash(expected["content"]):
        raise RevisionConflict("projection_content_mismatch")
    if message.get("forget_at") not in (None, "", "-"):
        raise RevisionConflict("projection_forgotten")
    for field in ("valid_at", "invalid_at"):
        actual_time = None if message.get(field) in (None, "", "-") else message[field]
        expected_time = None if expected.get(field) in (None, "", "-") else expected[field]
        if actual_time != expected_time:
            raise RevisionConflict("projection_time_mismatch")
    if message.get("status") not in ((True, 1, "1") if active else (False, 0, "0")):
        raise RevisionConflict("projection_status_mismatch")
    dimension = vector_dimension(message)
    if dimension != binding.dimension:
        raise RevisionConflict("projection_dimension_mismatch")
    return {
        "content_sha256": content_hash(message["content"]),
        "memory_id": memory_id,
        "message_id": expected["message_id"],
        "scope": message_scope(message, memory_id),
        "dimension": dimension,
        "active": active,
    }
