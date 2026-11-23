"""Seed and verify owned legacy collections during an isolated Milvus upgrade.

This probe intentionally rejects the normal local Milvus port and accepts only
loopback endpoints and explicitly named rehearsal collections. It is not an
exporter or a migration command for business data.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections.abc import Sequence
from numbers import Integral, Real
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from google.protobuf.json_format import MessageToDict
from google.protobuf.message import Message
from pymilvus import AnnSearchRequest, Collection, CollectionSchema, DataType, FieldSchema, Function, FunctionType, RRFRanker, connections, utility

_ALIAS = "isolated_upgrade_probe"


def validate_target(uri: str, prefix: str) -> None:
    """Reject ordinary service endpoints and collection names outside this probe."""
    parsed = urlparse(uri)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"} or parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("Use a loopback HTTP endpoint without credentials or a path")
    if parsed.port is None or not 20000 <= parsed.port <= 65535:
        raise ValueError("Use an isolated loopback port between 20000 and 65535")
    if not re.fullmatch(r"upgrade_rehearsal_[a-z0-9_]{1,80}", prefix):
        raise ValueError("Collection prefix must begin with upgrade_rehearsal_ and contain lowercase letters, digits, or underscores")


def _normalize(value: Any) -> Any:
    if isinstance(value, Message):
        return MessageToDict(value, preserving_proto_field_name=True, use_integers_for_enums=True)
    if isinstance(value, bool):
        return value
    if isinstance(value, Integral):
        return int(value)
    if isinstance(value, Real):
        return float(value)
    if isinstance(value, dict):
        return {key: _normalize(item) for key, item in sorted(value.items())}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_normalize(item) for item in value]
    return value


def _digest(value: Any) -> str:
    content = json.dumps(_normalize(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(content.encode()).hexdigest()


def _memory_row(message_id: int) -> dict[str, Any]:
    vectors = [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0], [0.0, 0.0, 1.0, 0.0], [0.0, 0.0, 0.0, 1.0], [-1.0, 0.0, 0.0, 0.0], [0.0, -1.0, 0.0, 0.0]]
    return {
        "id": f"memory_{message_id}",
        "message_id": message_id,
        "message_type_kwd": "raw" if message_id == 1 else "semantic",
        "source_id": 0 if message_id == 1 else 1,
        "memory_id": "owned_rehearsal_memory",
        "user_id": "" if message_id == 1 else " external subject ",
        "agent_id": "agent",
        "session_id": "session" if message_id != 2 else "session-extra",
        "valid_at": "2026-11-01 10:00:00",
        "invalid_at": "",
        "forget_at": "2026-11-02 10:00:00" if message_id == 4 else "",
        "status_int": 0 if message_id == 5 else 1,
        "zone_id": 0,
        "content_ltks": f"旧数据 memory upgrade {message_id}：正文与来源应保留",
        "q_4_vec": vectors[(message_id - 1) % len(vectors)],
        "legacy_extra": {"kept": True, "counter": message_id},
    }


def seed(prefix: str) -> None:
    """Create representative pre-upgrade rows, including persisted updates and deletes."""
    names = [f"{prefix}_memory", f"{prefix}_bm25"]
    if any(utility.has_collection(name, using=_ALIAS, timeout=30) for name in names):
        raise ValueError("Refusing to replace existing rehearsal collections")
    fields = [FieldSchema("id", DataType.VARCHAR, max_length=256, is_primary=True)]
    fields += [FieldSchema(name, DataType.INT64) for name in ("message_id", "source_id", "status_int", "zone_id")]
    fields += [
        FieldSchema(name, DataType.VARCHAR, max_length=65535 if name == "content_ltks" else 256)
        for name in ("message_type_kwd", "memory_id", "user_id", "agent_id", "session_id", "valid_at", "invalid_at", "forget_at", "content_ltks")
    ]
    fields.append(FieldSchema("q_4_vec", DataType.FLOAT_VECTOR, dim=4))
    memory = Collection(names[0], CollectionSchema(fields, enable_dynamic_field=True), using=_ALIAS)
    memory.create_index("q_4_vec", {"index_type": "HNSW", "metric_type": "COSINE", "params": {"M": 16, "efConstruction": 50}}, timeout=60)
    memory.insert([_memory_row(i) for i in range(1, 7)], timeout=30)
    edited = _memory_row(3)
    edited["content_ltks"] = "修改后的旧正文 updated before backup"
    memory.upsert([edited], timeout=30)
    memory.delete('id == "memory_6"', timeout=30)
    memory.flush(timeout=30)
    schema = CollectionSchema(
        [
            FieldSchema("id", DataType.VARCHAR, max_length=64, is_primary=True),
            FieldSchema("content", DataType.VARCHAR, max_length=2048, enable_analyzer=True),
            FieldSchema("tags", DataType.JSON),
            FieldSchema("vector", DataType.FLOAT_VECTOR, dim=4),
            FieldSchema("sparse", DataType.SPARSE_FLOAT_VECTOR),
        ],
        enable_dynamic_field=True,
    )
    schema.add_function(Function("bm25", input_field_names=["content"], output_field_names=["sparse"], function_type=FunctionType.BM25))
    knowledge = Collection(names[1], schema, using=_ALIAS)
    knowledge.create_index("vector", {"index_type": "AUTOINDEX", "metric_type": "COSINE", "params": {}}, timeout=60)
    knowledge.create_index("sparse", {"index_type": "SPARSE_INVERTED_INDEX", "metric_type": "BM25", "params": {}}, timeout=60)
    knowledge.insert(
        [
            {"id": "k1", "content": "memory upgrade persistent provenance", "tags": {"values": ["中文", "tag,comma"]}, "vector": [1.0, 0.0, 0.0, 0.0]},
            {"id": "k2", "content": "tenant isolation archive retention", "tags": {"values": []}, "vector": [0.0, 1.0, 0.0, 0.0]},
        ],
        timeout=30,
    )
    knowledge.flush(timeout=30)


def _collection_snapshot(collection: Collection, fields: list[str]) -> dict[str, Any]:
    collection.load(timeout=60)
    rows = collection.query('id != ""', output_fields=fields, limit=100, consistency_level="Strong", timeout=30)
    rows.sort(key=lambda row: row["id"])
    schema = semantic_schema(collection.schema.to_dict(), [index.to_dict() for index in collection.indexes])
    return {"count": len(rows), "rows_sha256": _digest(rows), "schema_sha256": _digest(schema), "schema": schema}


def semantic_schema(schema: dict[str, Any], indexes: list[dict[str, Any]]) -> dict[str, Any]:
    """Preserve semantic metadata while removing physical IDs and SDK default omissions.

    The SDK's public dictionaries preserve typed protobuf defaults and function
    definitions. Only known physical identity keys are removed; additional
    semantic keys remain in the receipt instead of being silently whitelisted.
    """
    result = _normalize(schema)
    result.pop("collection_id", None)
    collection_defaults: tuple[tuple[str, Any], ...] = (("auto_id", False), ("enable_dynamic_field", False), ("enable_namespace", False), ("description", ""), ("functions", []))
    field_defaults: tuple[tuple[str, Any], ...] = (
        ("is_primary", False),
        ("auto_id", False),
        ("nullable", False),
        ("is_partition_key", False),
        ("is_clustering_key", False),
        ("is_function_output", False),
        ("description", ""),
        ("params", {}),
    )
    for key, default in collection_defaults:
        result.setdefault(key, default)
    for field in result["fields"]:
        for key in ("field_id", "fieldID"):
            field.pop(key, None)
        for key, default in field_defaults:
            field.setdefault(key, default)
    for function in result["functions"]:
        for key in ("id", "function_id", "input_field_ids", "output_field_ids"):
            function.pop(key, None)
    normalized_indexes = _normalize(indexes)
    for index in normalized_indexes:
        for key in ("collection", "collection_id", "index_id"):
            index.pop(key, None)
    result["indexes"] = sorted(normalized_indexes, key=lambda index: (index.get("field", ""), index.get("index_name", "")))
    return result


def snapshot(prefix: str, *, native_order: bool) -> dict[str, Any]:
    """Independently read persisted rows, vector/BM25/hybrid search, and optional sorting."""
    memory = Collection(f"{prefix}_memory", using=_ALIAS)
    knowledge = Collection(f"{prefix}_bm25", using=_ALIAS)
    result = {
        "memory": _collection_snapshot(memory, ["*"]),
        "bm25": _collection_snapshot(knowledge, ["id", "content", "tags", "vector"]),
    }
    active = 'status_int == 1 and forget_at == ""'
    dense = memory.search([[1.0, 0.0, 0.0, 0.0]], "q_4_vec", {"metric_type": "COSINE", "params": {"ef": 16}}, limit=3, expr=active, timeout=30)
    if not dense[0] or dense[0][0].id != "memory_1":
        raise AssertionError("Legacy dense search did not return the expected active RAW")
    keyword = knowledge.search(["memory upgrade"], "sparse", {"metric_type": "BM25", "params": {}}, limit=2, timeout=30)
    if not keyword[0] or keyword[0][0].id != "k1":
        raise AssertionError("Legacy BM25 function/index did not return k1")
    hybrid = knowledge.hybrid_search(
        [
            AnnSearchRequest([[1.0, 0.0, 0.0, 0.0]], "vector", {"metric_type": "COSINE", "params": {}}, limit=2),
            AnnSearchRequest(["memory upgrade"], "sparse", {"metric_type": "BM25", "params": {}}, limit=2),
        ],
        RRFRanker(),
        limit=2,
        timeout=30,
    )
    if not hybrid[0] or hybrid[0][0].id != "k1":
        raise AssertionError("Legacy hybrid search did not return k1")
    if native_order:
        rows = memory.query(active, output_fields=["message_id", "valid_at"], limit=100, order_by=["valid_at:desc", "message_id:desc"], consistency_level="Strong", timeout=30)
        ids = [row["message_id"] for row in rows]
        if ids != sorted(ids, reverse=True):
            raise AssertionError("Native query ORDER BY was not applied")
    return result


def write_after_upgrade(prefix: str) -> None:
    """Exercise new writes without changing any pre-upgrade baseline collection schema."""
    memory = Collection(f"{prefix}_memory", using=_ALIAS)
    if memory.query('id == "memory_9"', output_fields=["id"], limit=1, consistency_level="Strong", timeout=30):
        raise ValueError("Post-upgrade write probe has already run; inspect persisted state before any retry")
    row = _memory_row(9)
    memory.insert([row], timeout=30)
    edited = _memory_row(3)
    edited["content_ltks"] = "升级后修改 updated after upgrade"
    memory.upsert([edited], timeout=30)
    memory.delete('id == "memory_2"', timeout=30)
    memory.flush(timeout=30)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["seed", "verify", "write"])
    parser.add_argument("--uri", required=True)
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--server-version", required=True)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--native-order", action="store_true")
    args = parser.parse_args()
    validate_target(args.uri, args.prefix)
    connections.connect(_ALIAS, uri=args.uri, timeout=10)
    try:
        version = utility.get_server_version(using=_ALIAS, timeout=10)
        if version != args.server_version:
            raise ValueError(f"Expected server {args.server_version}, got {version}")
        if args.mode == "seed":
            seed(args.prefix)
        elif args.mode == "write":
            write_after_upgrade(args.prefix)
        actual = snapshot(args.prefix, native_order=args.native_order)
        if args.baseline is not None and actual != json.loads(args.baseline.read_text())["snapshot"]:
            raise AssertionError("Persisted schema/row/count hashes differ from the supplied baseline")
        receipt = {"server_version": version, "native_order_checked": args.native_order, "snapshot": actual}
        args.receipt.parent.mkdir(parents=True, exist_ok=True)
        args.receipt.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
        summary = {name: {key: value for key, value in collection.items() if key != "schema"} for name, collection in actual.items()}
        print(json.dumps({**receipt, "snapshot": summary}, ensure_ascii=False))
    finally:
        connections.disconnect(_ALIAS)


if __name__ == "__main__":
    main()
