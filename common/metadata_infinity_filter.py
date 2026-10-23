"""Lossless Infinity metadata candidates, always followed by source matching.

String coercion, Unicode case folding and expanded-array negatives belong to
the shared matcher. Presence is a safe superset for those operators; numeric
and boolean list membership can additionally prune by JSON containment. Never
translate multi-valued negatives into NOT JSON_CONTAINS.
"""

import json
import math
import re
from collections.abc import Iterator
from typing import Any

from infinity.common import InfinityException
from infinity.errors import ErrorCode

_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def _sql_string(value: str) -> str:
    # IDs come from authorized SQL bindings. Refuse ambiguous SDK escaping,
    # rather than interpolate backslashes/control characters into its parser.
    if not value or any(ord(char) < 32 or char == "\\" for char in value):
        raise ValueError("Invalid Infinity metadata scope")
    return "'" + value.replace("'", "''") + "'"


def build_infinity_filter(filters: list[dict[str, Any]], logic: str, kb_ids: list[str]) -> str:
    """Compile a scoped candidate superset; an unprovable clause never prunes."""
    if not kb_ids:
        return "1 = 0"
    if logic not in {"and", "or"}:
        raise ValueError("Unsupported metadata filter logic")
    scope = "kb_id IN (" + ", ".join(_sql_string(kb_id) for kb_id in dict.fromkeys(kb_ids)) + ")"
    clauses: list[str] = []
    for condition in filters:
        key = condition.get("key")
        if not isinstance(key, str) or not _KEY.fullmatch(key):
            if logic == "or":
                return scope
            continue
        path = "'$." + key + "'"
        clause = f"JSON_EXTRACT_STRING(meta_fields, {path}) != 'null'"
        value = condition.get("value")
        if condition.get("op") == "in" and isinstance(value, (list, tuple)) and value:
            # Large numbers or text retain presence pruning. JSON numeric
            # containment must not narrow the matcher's original typed values.
            if all(isinstance(item, (bool, int, float)) and abs(item) <= 2**53 and math.isfinite(item) for item in value):
                literals = ["'true'" if item is True else "'false'" if item is False else repr(item) for item in value]
                clause = "(" + " OR ".join(f"JSON_CONTAINS(meta_fields, {path}, {literal})" for literal in dict.fromkeys(literals)) + ")"
        clauses.append(clause)
    return scope if not clauses else scope + " AND (" + (" AND " if logic == "and" else " OR ").join(clauses) + ")"


def iter_infinity_metadata(connector: Any, index_name: str, kb_ids: list[str], filters: list[dict[str, Any]] | None = None, logic: str = "and") -> Iterator[tuple[str, dict[str, Any]]]:
    """Read one complete projected query snapshot, releasing its lease on close.

    SDK 0.7.0 materializes its response. Omitting limit avoids a 10k cap and
    avoids inconsistent offset pages during concurrent replacement. Only a
    confirmed missing table is empty; malformed/partial responses and RPC
    failures propagate. No API service is imported into this common boundary.
    """
    if not kb_ids:
        return
    predicate = build_infinity_filter(filters or [], logic, kb_ids)
    pool = connector.connPool
    connection = pool.get_conn()
    try:
        database = connection.get_database(connector.dbName)
        try:
            table = database.get_table(index_name)
        except InfinityException as exc:
            if exc.error_code != ErrorCode.TABLE_NOT_EXIST:
                raise
            return
        data, _, _ = table.output(["id", "kb_id", "meta_fields"]).filter(predicate).to_result()
        columns = [data.get(key) for key in ("id", "kb_id", "meta_fields")]
        if any(not isinstance(column, list) for column in columns) or len({len(column) for column in columns}) != 1:
            raise RuntimeError("Malformed Infinity metadata response")
        seen: set[str] = set()
        for doc_id, kb_id, metadata in zip(*columns, strict=True):
            if not isinstance(doc_id, str) or not doc_id or doc_id in seen or kb_id not in kb_ids:
                raise RuntimeError("Infinity metadata response escaped the requested identity or scope")
            fields = json.loads(metadata) if isinstance(metadata, str) else metadata
            if not isinstance(fields, dict):
                raise RuntimeError("Malformed Infinity metadata source")
            seen.add(doc_id)
            yield doc_id, fields
    finally:
        pool.release_conn(connection)
