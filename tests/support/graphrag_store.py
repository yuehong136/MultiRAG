"""Explicit unit-only GraphRAG storage fake."""

import json
from typing import Any


class Store:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.events: list[str] = []
        self.fail_read = False
        self.fail_write = False
        self.fail_delete = False

    def db_type(self) -> str:
        return "milvus"

    def graph_rows(self, _name: str, kb: str, kinds: list[str]) -> list[dict[str, Any]]:
        if self.fail_read:
            raise RuntimeError("snapshot failed")
        return [dict(row) for row in self.rows.values() if row["kb_id"] == kb and row["knowledge_graph_kwd"] in kinds]

    def graph_subgraph_rows(self, name: str, kb: str, doc: str) -> list[dict[str, Any]]:
        return [row for row in self.graph_rows(name, kb, ["subgraph"]) if row.get("source_id") in ([doc], doc)]

    def graph_delete_rows(self, _name: str, _kb: str, ids: list[str]) -> None:
        self.events.append("prune")
        if self.fail_delete:
            raise RuntimeError("prune failed")
        for cid in ids:
            self.rows.pop(cid, None)

    def insert(self, chunks: list[dict[str, Any]], _name: str, _kb: str) -> list[str]:
        self.events.extend(chunk["knowledge_graph_kwd"] for chunk in chunks)
        if self.fail_write:
            return ["write failed"]
        for chunk in chunks:
            self.rows[chunk["id"]] = {**chunk, "pk": chunk["id"]}
        return []

    def _get_connection(self) -> "Store":
        return self

    def describe_collection(self, _name: str) -> dict[str, Any]:
        return {"fields": []}

    def query(self, _name: str, *, expr: str, **kwargs: Any) -> list[dict[str, Any]]:
        identifiers = json.loads(expr.split("pk in ")[1])
        return [self.rows[cid] for cid in identifiers if cid in self.rows]
