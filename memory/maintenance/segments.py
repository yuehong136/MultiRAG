"""Lossless, bounded Unicode spans for explicitly selected RAW evidence.

The implementation is original. Hindsight's multi-chunk coverage regressions
informed the invariant that a part is never a whole-document completion stamp.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from memory.revisions.domain import RevisionValidationError, content_hash, request_hash

SPLITTER_VERSION = "unicode-boundary-v1"
CONTEXT_CODEPOINTS = 64
MAX_TOTAL_FRAGMENTS = 256
MAX_SOURCE_BYTES = 1048576


def segmentation_request(value: Any) -> dict[str, Any] | None:
    """Absent/whole remains the original request and profile, byte for byte."""
    if value is None or value == {"mode": "whole"}:
        return None
    if not isinstance(value, dict) or value.get("mode") != "segmented" or set(value) - {"mode", "fragment_token_limit", "max_parts_per_source"}:
        raise RevisionValidationError("invalid_maintenance_segmentation")
    limit, parts = value.get("fragment_token_limit", 2048), value.get("max_parts_per_source", 32)
    if type(limit) is not int or not 256 <= limit <= 8192 or type(parts) is not int or not 1 <= parts <= 64:
        raise RevisionValidationError("invalid_maintenance_segmentation")
    return {"mode": "segmented", "fragment_token_limit": limit, "max_parts_per_source": parts, "splitter_version": SPLITTER_VERSION, "context_codepoints": CONTEXT_CODEPOINTS}


def fragment_metadata(memory_id: str, source: dict[str, Any], content: str, *, start: int, end: int, index: int, count: int) -> dict[str, Any]:
    read_start, read_end = max(0, start - CONTEXT_CODEPOINTS), min(len(content), end + CONTEXT_CODEPOINTS)
    result = {
        "part_index": index,
        "part_count": count,
        "offset_unit": "unicode_codepoint",
        "core_start": start,
        "core_end": end,
        "read_start": read_start,
        "read_end": read_end,
        "core_sha256": content_hash(content[start:end]),
        "read_sha256": content_hash(content[read_start:read_end]),
        "splitter_version": SPLITTER_VERSION,
    }
    identity = {name: source[name] for name in ("source_id", "source_revision", "source_generation", "content_sha256")}
    return {"fragment_id": request_hash({"memory_id": memory_id, **identity, **result}), **result}


def validate_fragment(value: Any, *, content: str | None = None) -> dict[str, Any]:
    fields = {"fragment_id", "part_index", "part_count", "offset_unit", "core_start", "core_end", "read_start", "read_end", "core_sha256", "read_sha256", "splitter_version"}
    if not isinstance(value, dict) or set(value) != fields:
        raise RevisionValidationError("invalid_maintenance_fragment")
    if any(type(value[name]) is not int for name in ("part_index", "part_count", "core_start", "core_end", "read_start", "read_end")):
        raise RevisionValidationError("invalid_maintenance_fragment")
    if (
        not 0 <= value["part_index"] < value["part_count"] <= 64
        or not 0 <= value["read_start"] <= value["core_start"] < value["core_end"] <= value["read_end"]
        or value["core_start"] - value["read_start"] > CONTEXT_CODEPOINTS
        or value["read_end"] - value["core_end"] > CONTEXT_CODEPOINTS
        or value["offset_unit"] != "unicode_codepoint"
        or value["splitter_version"] != SPLITTER_VERSION
        or any(not isinstance(value[name], str) or len(value[name]) != 64 or any(ch not in "0123456789abcdef" for ch in value[name]) for name in ("fragment_id", "core_sha256", "read_sha256"))
    ):
        raise RevisionValidationError("invalid_maintenance_fragment")
    if content is not None:
        relative_start, relative_end = value["core_start"] - value["read_start"], value["core_end"] - value["read_start"]
        if len(content) != value["read_end"] - value["read_start"] or content_hash(content) != value["read_sha256"] or content_hash(content[relative_start:relative_end]) != value["core_sha256"]:
            raise RevisionValidationError("maintenance_fragment_hash_mismatch")
    return dict(value)


def split_source(
    memory_id: str,
    source: dict[str, Any],
    content: str,
    configuration: dict[str, Any],
    *,
    count_tokens: Callable[[str], int],
    fits: Callable[[dict[str, Any]], bool],
) -> list[dict[str, Any]]:
    """Tile every original code point once; overlap is evidence, never coverage.

    Prefer paragraph/sentence/line boundaries in the latter half of a fitting
    core. Binary search only finds a candidate; every emitted read span is
    independently checked against both the text and complete request budgets.
    """
    if not isinstance(content, str) or not content.strip() or len(content.encode("utf-8")) > MAX_SOURCE_BYTES:
        raise RevisionValidationError("segment_source_size_limit")
    spans: list[tuple[int, int]] = []
    start = 0
    while start < len(content):
        if len(spans) == configuration["max_parts_per_source"]:
            raise RevisionValidationError("segment_parts_limit")

        def acceptable(end: int) -> bool:
            fragment = fragment_metadata(memory_id, source, content, start=start, end=end, index=len(spans), count=64)
            body = content[fragment["read_start"] : fragment["read_end"]]
            return len(body.encode("utf-8")) <= 65535 and count_tokens(body) <= configuration["fragment_token_limit"] and fits(fragment)

        low, high, chosen = start + 1, len(content), None
        while low <= high:
            middle = (low + high) // 2
            if acceptable(middle):
                chosen, low = middle, middle + 1
            else:
                high = middle - 1
        if chosen is None:
            raise RevisionValidationError("segment_no_fragment_fits")
        if chosen < len(content):
            floor = start + max(1, (chosen - start) // 2)
            # Prefer stronger boundaries before falling back to a literal cut.
            for delimiters in (("\n\n",), ("。", "！", "？", ". ", "! ", "? "), ("\n", ";", "；", "，", ", ", " ")):
                boundaries = [content.rfind(delimiter, floor, chosen) + len(delimiter) for delimiter in delimiters if content.rfind(delimiter, floor, chosen) >= 0]
                if boundaries and acceptable(max(boundaries)):
                    chosen = max(boundaries)
                    break
        spans.append((start, chosen))
        start = chosen
    result = [fragment_metadata(memory_id, source, content, start=start, end=end, index=index, count=len(spans)) for index, (start, end) in enumerate(spans)]
    if any(not fits(fragment) or count_tokens(content[fragment["read_start"] : fragment["read_end"]]) > configuration["fragment_token_limit"] for fragment in result):
        raise RevisionValidationError("segment_no_fragment_fits")
    return result
