"""Stable Infinity sidecars and SQL ownership through sync/async consumers."""

import json
import os
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import infinity
import pytest
import sqlalchemy as sa
from infinity.common import ConflictType, NetworkAddress
from infinity.connection_pool import ConnectionPool
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from api.db.db_models import Knowledgebase
from api.db.services.doc_metadata_service import DocMetadataService
from api.db.services.metadata_store_engine import EngineMetadataStore
from common import settings
from common.metadata_infinity_filter import build_infinity_filter, iter_infinity_metadata
from common.metadata_utils import aggregate_metadata_rows, apply_meta_data_filter, meta_filter


class TrackedPool:
    def __init__(self, address: NetworkAddress) -> None:
        self.pool = ConnectionPool(address, max_size=4)
        self.active: set[int] = set()
        self.acquired = self.released = 0

    def get_conn(self) -> Any:
        conn = self.pool.get_conn()
        assert id(conn) not in self.active
        self.active.add(id(conn))
        self.acquired += 1
        return conn

    def release_conn(self, conn: Any) -> None:
        self.active.remove(id(conn))
        self.released += 1
        self.pool.release_conn(conn)


@pytest.fixture
def infinity_metadata(bootstrapped_engine: sa.Engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[dict[str, Any]]:
    host, port = os.environ["INFINITY_TEST_URI"].rsplit(":", 1)
    address = NetworkAddress(host, int(port))
    writer, reader = infinity.connect(address), infinity.connect(address)
    name = "after9d_f_" + uuid4().hex
    owners, kb_ids = [uuid4().hex for _ in range(2)], [uuid4().hex for _ in range(3)]
    rows = [
        (uuid4().hex, {"tag": ["Alice", "Bob"], "x": 0, "y": False}),
        (uuid4().hex, {"tag": "ALICE", "x": "0", "y": "False"}),
        (uuid4().hex, {"tag": ["K", "ÄBC", None], "x": False, "y": 0}),
        (uuid4().hex, {"tag": "O'Reilly", "x": [False, 0, 1.5], "blank": ""}),
        (uuid4().hex, {"tag": [], "blank": None}),
        (uuid4().hex, {"blank": []}),
        (uuid4().hex, {}),
        (uuid4().hex, {"non.literal": "Alice", "tag": "null"}),
    ]
    binding = {doc_id: kb_ids[position % 2] for position, (doc_id, _) in enumerate(rows)}
    indexes = ["multirag_doc_meta_" + owner for owner in owners]
    pool = TrackedPool(address)
    connector = SimpleNamespace(dbName=name, connPool=pool)
    store = EngineMetadataStore()
    env = {"engine": bootstrapped_engine, "rows": rows, "kb_ids": kb_ids, "owners": owners, "indexes": indexes, "connector": connector, "reader": reader, "name": name}
    record: dict[str, Any] = {"infinity_uri": f"{host}:{port}", "database": name, "tables": indexes, "sql_database": bootstrapped_engine.url.database, "kbs": kb_ids}
    evidence = Path(os.environ.get("MULTIRAG_F_EVIDENCE_DIR", str(tmp_path)))
    evidence.mkdir(parents=True, exist_ok=True)
    path = evidence / (name + ".json")
    path.write_text(json.dumps(record))
    try:
        database = writer.create_database(name, ConflictType.Error)
        for index in indexes:
            database.create_table(index, {"id": {"type": "varchar"}, "kb_id": {"type": "varchar"}, "meta_fields": {"type": "json"}})
        for position, (doc_id, fields) in enumerate(rows):
            database.get_table(indexes[position % 2]).insert({"id": doc_id, "kb_id": binding[doc_id], "meta_fields": json.dumps(fields, ensure_ascii=False)})
        database.get_table(indexes[0]).insert({"id": "foreign", "kb_id": kb_ids[2], "meta_fields": '{"tag":"Alice","x":0}'})
        with bootstrapped_engine.begin() as db:
            db.execute(
                sa.insert(Knowledgebase),
                [
                    {"id": kb, "tenant_id": owners[position % 2], "created_by": owners[position % 2], "name": kb, "embd_id": "controlled", "parser_id": "naive", "parser_config": {}}
                    for position, kb in enumerate(kb_ids)
                ],
            )
        monkeypatch.setattr(settings, "DOC_ENGINE", "infinity")
        monkeypatch.setitem(settings.__dict__, "docStoreConn", connector)
        monkeypatch.setattr(DocMetadataService, "_store", staticmethod(lambda: store))
        env["snapshot"] = lambda: {index: reader.get_database(name).get_table(index).output(["id", "kb_id", "meta_fields"]).to_result()[0] for index in indexes}
        yield env
    finally:
        try:
            assert not pool.active and pool.acquired == pool.released
            record["leases"] = {"acquired": pool.acquired, "released": pool.released, "active": len(pool.active)}
        finally:
            pool.pool.destroy()
            writer.get_database("default_db")
            reader.get_database("default_db")
            writer.drop_database(name, ConflictType.Ignore)
            record["infinity_database_absent"] = name not in reader.list_databases().db_names
            assert record["infinity_database_absent"]
            with bootstrapped_engine.begin() as db:
                db.execute(sa.delete(Knowledgebase).where(Knowledgebase.id.in_(kb_ids)))
            with bootstrapped_engine.connect() as db:
                record["sql_kbs_remaining"] = db.scalar(sa.select(sa.func.count()).select_from(Knowledgebase).where(Knowledgebase.id.in_(kb_ids)))
                assert record["sql_kbs_remaining"] == 0
            path.write_text(json.dumps(record, indent=2))
            writer.disconnect()
            reader.disconnect()


async def test_real_typed_matrix_and_base_intersection(infinity_metadata: dict[str, Any]) -> None:
    env = infinity_metadata
    baseline, before = aggregate_metadata_rows(env["rows"]), env["snapshot"]()
    engine = create_async_engine(env["engine"].url, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    cases = [
        ("tag", "in", ["ALICE"]),
        ("tag", "not in", ["Alice"]),
        ("tag", "not contains", "Alice"),
        ("tag", "contains", "null"),
        ("tag", "empty", ""),
        ("tag", "not empty", ""),
        ("tag", "in", ["k"]),
        ("tag", "contains", "ä"),
        ("tag", "start with", "o'"),
        ("tag", "end with", "REILLY"),
        ("x", "in", [0]),
        ("x", "in", [False]),
        ("x", "in", [1.5]),
        ("x", "in", [0, False]),
        ("x", "in", ["0"]),
        ("y", "in", [False]),
        ("blank", "empty", ""),
        ("missing", "empty", ""),
        ("blank", "not empty", ""),
        ("tag", "not in", []),
        ("tag", "not in", [None]),
        ("non.literal", "in", ["Alice"]),
    ]
    try:
        for key, op, value in cases:
            for logic in ("and", "or"):
                filters = [{"key": key, "op": op, "value": value}, {"key": "tag", "op": "in", "value": ["nobody"]}]
                expected = set(meta_filter(baseline, filters, logic))
                with Session(env["engine"]) as db:
                    assert set(DocMetadataService.filter_doc_ids_by_meta_pushdown(db, env["kb_ids"][:2], filters, logic) or []) == expected
                async with sessions() as db:
                    loader = AsyncMock(side_effect=AssertionError("manual streamable filters must not aggregate"))
                    selected = [env["rows"][0][0]]
                    result = await apply_meta_data_filter(
                        {"method": "manual", "manual": filters, "logic": logic},
                        base_doc_ids=selected,
                        metas_loader=loader,
                        metadata_filterer=lambda f, l: DocMetadataService.filter_doc_ids_by_meta_pushdown_async(db, env["kb_ids"][:2], f, l),
                    )
                    assert set(result or []) == (expected.intersection(selected) or {"-999"})
                    loader.assert_not_awaited()
        assert env["snapshot"]() == before
        with Session(env["engine"]) as db:
            assert DocMetadataService.filter_doc_ids_by_meta_pushdown(db, [uuid4().hex], [{"key": "x", "op": "in", "value": [0]}]) == []
            assert DocMetadataService.filter_doc_ids_by_meta_pushdown(db, env["kb_ids"][:2], []) is None
            # Comparisons preserve full aggregate coercion; both tenants and
            # the original arrays remain available to that residual path.
            actual = DocMetadataService.get_flatted_meta_by_kbs(db, env["kb_ids"][:2])
            for op in ("=", "≠", ">", "<", "≥", "≤"):
                filters = [{"key": "x", "op": op, "value": "0"}]
                assert set(meta_filter(actual, filters)) == set(meta_filter(baseline, filters))
    finally:
        await engine.dispose()


def test_complete_snapshot_over_ten_thousand_and_candidate_pruning(infinity_metadata: dict[str, Any]) -> None:
    env = infinity_metadata
    table = env["reader"].get_database(env["name"]).get_table(env["indexes"][0])
    wanted = {"large_" + str(i) for i in range(10005)}
    for start in range(0, 10005, 1000):
        table.insert([{"id": "large_" + str(i), "kb_id": env["kb_ids"][0], "meta_fields": '{"x":12}'} for i in range(start, min(start + 1000, 10005))])
    filters = [{"key": "x", "op": "in", "value": [12]}]
    with Session(env["engine"]) as db:
        assert set(DocMetadataService.filter_doc_ids_by_meta_pushdown(db, env["kb_ids"][:2], filters) or []) == wanted
        aggregate = DocMetadataService.get_flatted_meta_by_kbs(db, env["kb_ids"][:2])
        assert set(meta_filter(aggregate, [{"key": "x", "op": "=", "value": "12"}])) == wanted
    query = build_infinity_filter([{"key": "x", "op": "in", "value": [False]}], "and", env["kb_ids"][:2])
    candidates = table.output(["id"]).filter(query).to_result()[0]["id"]
    assert len(candidates) < len(wanted)
    assert "foreign" not in candidates


def test_real_missing_table_and_rpc_failure_release(infinity_metadata: dict[str, Any]) -> None:
    env = infinity_metadata
    assert list(iter_infinity_metadata(env["connector"], "missing", env["kb_ids"][:2])) == []
    env["connector"].dbName = "not_an_existing_database"
    try:
        with pytest.raises(infinity.common.InfinityException):
            list(iter_infinity_metadata(env["connector"], env["indexes"][0], env["kb_ids"][:2]))
    finally:
        env["connector"].dbName = env["name"]
