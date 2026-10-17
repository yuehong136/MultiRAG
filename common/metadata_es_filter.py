"""Compile a lossless ES candidate query, followed by exact source matching.

Value DSL is deliberately avoided: CSV membership, typed scalar collisions,
Unicode lowercasing, missing/empty and expanded-array negatives differ from ES.
Only indexed scalar field presence can safely prune candidates. Unknown mapping
shapes return the explicit unsupported signal, never an empty match set.
"""

from typing import Any


class UnsupportedMetaFilter(ValueError):
    """The metadata mapping cannot prove lossless candidate selection."""


def build_meta_filter_query(filters: list[dict[str, Any]], logic: str, kb_ids: list[str], mapping: dict[str, Any]) -> dict[str, Any]:
    metadata = mapping.get("properties", {}).get("meta_fields", {})
    if metadata.get("enabled") is False or logic not in ("and", "or"):
        raise UnsupportedMetaFilter("Unindexed metadata or unsupported logic")
    clauses = []
    for condition in filters:
        key = condition.get("key")
        if not isinstance(key, str) or not key or any(char in key for char in ".*?\\"):
            raise UnsupportedMetaFilter("Nonliteral ES field path")
        field = metadata.get("properties", {}).get(key, {})
        if field.get("type") not in {"text", "keyword", "boolean", "long", "integer", "short", "byte", "double", "float", "half_float", "scaled_float", "date"}:
            raise UnsupportedMetaFilter("Unknown or structured field mapping")
        if field.get("index") is False or field.get("ignore_malformed") or "ignore_above" in field:
            raise UnsupportedMetaFilter("Source values may be absent from the index")
        clauses.append({"exists": {"field": "meta_fields." + key}})
    scope: list[dict[str, Any]] = [{"terms": {"kb_id": kb_ids}}]
    if clauses:
        scope.append({"bool": {"must": clauses}} if logic == "and" else {"bool": {"should": clauses, "minimum_should_match": 1}})
    return {"bool": {"filter": scope}}
