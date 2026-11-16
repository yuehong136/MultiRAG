"""Declared source segments are data; authenticated submission is not speaker proof."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from datetime import datetime
from typing import Any

from memory.revisions.domain import RevisionConflict, RevisionValidationError, content_hash, request_hash

SOURCE_FORMAT = "structured_source"
SCHEMA_VERSION = 1
MAX_SEGMENTS = 32
MAX_SOURCE_BYTES = 65535
_ZONED_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)")


def declared_event_time(value: Any) -> str | None:
    """Preserve the declared offset, never infer a timezone or substitute now."""
    if value is None:
        return None
    if not isinstance(value, str) or not _ZONED_TIME.fullmatch(value):
        raise RevisionValidationError("invalid_source_event_time")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.utcoffset() is None:
            raise ValueError("timezone required")
    except ValueError as error:
        raise RevisionValidationError("invalid_source_event_time") from error
    return parsed.isoformat()


def source_envelope(segments: Any, event_time: Any = None) -> dict[str, Any]:
    if not isinstance(segments, list) or not 1 <= len(segments) <= MAX_SEGMENTS:
        raise RevisionValidationError("invalid_source_segments")
    result = []
    for segment in segments:
        if (
            not isinstance(segment, dict)
            or set(segment) != {"role", "content"}
            or not isinstance(segment["role"], str)
            or segment["role"] not in {"user", "assistant", "tool", "unknown"}
            or not isinstance(segment["content"], str)
            or not segment["content"].strip()
        ):
            raise RevisionValidationError("invalid_source_segment")
        result.append({"role": segment["role"], "content": segment["content"]})
    value = {"schema": SOURCE_FORMAT, "schema_version": SCHEMA_VERSION, "segments": result, "event_time": declared_event_time(event_time)}
    canonical_source(value)
    return value


def canonical_source(envelope: dict[str, Any]) -> str:
    content = json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(content.encode("utf-8")) > MAX_SOURCE_BYTES:
        raise RevisionValidationError("structured_source_bytes_exceeded")
    return content


def parse_registered_source(content: str) -> dict[str, Any]:
    """Only call after SQL has established the server-owned format discriminator."""
    try:
        value = json.loads(content)
    except (ValueError, TypeError) as error:
        raise RevisionConflict("structured_source_content_mismatch") from error
    if (
        not isinstance(value, dict)
        or set(value) != {"schema", "schema_version", "segments", "event_time"}
        or value["schema"] != SOURCE_FORMAT
        or type(value["schema_version"]) is not int
        or value["schema_version"] != SCHEMA_VERSION
    ):
        raise RevisionConflict("structured_source_content_mismatch")
    normalized = source_envelope(value["segments"], value["event_time"])
    if canonical_source(normalized) != content:
        raise RevisionConflict("structured_source_content_mismatch")
    return normalized


def submission_metadata(*, actor_id: str, operation_id: str, content_sha256: str, received_at: str, reference_time: str) -> dict[str, Any]:
    received = declared_event_time(received_at)
    if (
        not isinstance(actor_id, str)
        or not actor_id
        or not isinstance(operation_id, str)
        or not operation_id
        or received is None
        or not isinstance(reference_time, str)
        or not reference_time
        or not isinstance(content_sha256, str)
        or not re.fullmatch(r"[0-9a-f]{64}", content_sha256)
    ):
        raise RevisionConflict("structured_source_receipt_invalid")
    value = {
        "format": SOURCE_FORMAT,
        "schema_version": SCHEMA_VERSION,
        "role_status": "caller_declared",
        "received_at": received,
        "reference_time": reference_time,
        "envelope_sha256": content_sha256,
        "provenance": {
            "kind": "authenticated_submission",
            "submitted_by": actor_id,
            "operation_id": operation_id,
            "authenticated_submitter": True,
            "speaker_identity_verified": False,
            "content_verified": False,
        },
    }
    return {**value, "provenance_sha256": request_hash(value)}


def validate_submission_metadata(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"format", "schema_version", "role_status", "received_at", "reference_time", "envelope_sha256", "provenance", "provenance_sha256"}:
        raise RevisionValidationError("invalid_source_submission_metadata")
    provenance = value["provenance"]
    if not isinstance(provenance, dict) or set(provenance) != {"kind", "submitted_by", "operation_id", "authenticated_submitter", "speaker_identity_verified", "content_verified"}:
        raise RevisionValidationError("invalid_source_submission_metadata")
    if type(value["schema_version"]) is not int or any(type(provenance[name]) is not bool for name in ("authenticated_submitter", "speaker_identity_verified", "content_verified")):
        raise RevisionValidationError("invalid_source_submission_metadata")
    expected = submission_metadata(
        actor_id=provenance["submitted_by"], operation_id=provenance["operation_id"], content_sha256=value["envelope_sha256"], received_at=value["received_at"], reference_time=value["reference_time"]
    )
    if value != expected:
        raise RevisionValidationError("invalid_source_submission_metadata")
    return deepcopy(expected)


def evidence_snapshot(content: str, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    if metadata is None:
        return {"content": content, "message_type": "raw", "origin": "legacy_combined", "role_status": "role_unknown"}
    checked = validate_submission_metadata(metadata)
    if content_hash(content) != checked["envelope_sha256"]:
        raise RevisionConflict("structured_source_content_mismatch")
    parse_registered_source(content)
    return {"content": content, "message_type": "raw", "origin": "authenticated_submission", "role_status": "caller_declared", "source_metadata": checked}
