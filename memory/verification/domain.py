"""Validation and metadata cursors; no verification claim is model-generated."""

from __future__ import annotations

import base64
import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from memory.revisions.domain import RevisionValidationError

RESULTS = ("human_confirmed", "disputed", "needs_review")
METHODS = ("source_comparison", "external_check", "other")
MAX_SOURCES = 32
TODO_SCAN_LIMIT = 64
TODO_PHYSICAL_LIMIT = 256


def identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
        raise RevisionValidationError("invalid_verification_identifier")
    return value


def head_version(value: Any) -> int:
    if isinstance(value, str) and re.fullmatch(r"0|[1-9][0-9]{0,18}", value):
        value = int(value)
    if type(value) is not int or not 0 <= value < 2**63:
        raise RevisionValidationError("invalid_verification_head_version")
    return value


def bounded_text(value: Any, maximum: int, *, nonblank: bool = False) -> str:
    if not isinstance(value, str) or len(value) > maximum or (nonblank and not value.strip()):
        raise RevisionValidationError("invalid_verification_text")
    return value


def create_request(
    *, expected_head_version: Any, expected_binding_sha256: str, result: str, method: str, applicability: str, notes: str, evidence: list[dict[str, str]], checked_at: datetime | None
) -> dict[str, Any]:
    if result not in RESULTS or method not in METHODS or not isinstance(expected_binding_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_binding_sha256):
        raise RevisionValidationError("invalid_verification_result_or_binding")
    if not isinstance(evidence, list) or len(evidence) > 8:
        raise RevisionValidationError("invalid_verification_evidence")
    copied = []
    for item in evidence:
        if not isinstance(item, dict) or set(item) != {"description", "reference"}:
            raise RevisionValidationError("invalid_verification_evidence")
        copied.append({"description": bounded_text(item["description"], 1024, nonblank=True), "reference": bounded_text(item["reference"], 1024)})
    if checked_at is not None:
        if not isinstance(checked_at, datetime) or checked_at.tzinfo is None or checked_at.utcoffset() is None or checked_at > datetime.now(UTC) + timedelta(minutes=5):
            raise RevisionValidationError("invalid_reported_verification_time")
        checked_at = checked_at.astimezone(UTC)
    return {
        "expected_head_version": head_version(expected_head_version),
        "expected_binding_sha256": expected_binding_sha256,
        "result": result,
        "method": method,
        "applicability": bounded_text(applicability, 1024, nonblank=True),
        "notes": bounded_text(notes, 2048),
        "evidence": copied,
        "checked_at": checked_at.isoformat() if checked_at is not None else None,
    }


def cursor_encode(query_sha256: str, after: str | int) -> str:
    return base64.urlsafe_b64encode(json.dumps({"query_sha256": query_sha256, "after": after}, separators=(",", ":")).encode()).decode().rstrip("=")


def cursor_decode(value: str | None, query_sha256: str) -> str | int | None:
    if value is None:
        return None
    try:
        if len(value) > 512:
            raise ValueError
        parsed = json.loads(base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True))
        if not isinstance(parsed, dict) or set(parsed) != {"query_sha256", "after"} or parsed["query_sha256"] != query_sha256:
            raise ValueError
        return parsed["after"]
    except (TypeError, ValueError, UnicodeError) as error:
        raise RevisionValidationError("invalid_verification_cursor") from error
