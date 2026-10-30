"""OceanBase graph schema and consumer contracts at the client boundary."""

import json
import logging
import threading
from typing import Any
from unittest.mock import Mock

import pytest
from sqlalchemy import MetaData, Table
from sqlalchemy.dialects.mysql import LONGTEXT, dialect
from sqlalchemy.schema import CreateTable

from common import resources
from common.doc_store.doc_store_base import OrderByExpr
from core.graphrag.search import KGSearch
from core.utils import ob_conn, redis_conn


@pytest.fixture
def connection() -> Any:
    cls = next(cell.cell_contents for cell in ob_conn.OBConnection.__closure__ if isinstance(cell.cell_contents, type))
    conn = object.__new__(cls)
    conn.client = Mock()
    conn.logger = logging.getLogger(__name__)
    conn.es = None
    conn.enable_fulltext_search = True
    conn.use_fulltext_hint = False
    conn._fulltext_search_columns = ob_conn.FTS_COLUMNS_ORIGIN
    conn._table_exists_cache = set()
    conn._table_exists_cache_lock = threading.RLock()
    conn._index_exists = Mock(return_value=True)
    conn._ensure_vector_column_exists = Mock()
    return conn


def test_new_chunk_schema_uses_nullable_longtext(connection: Any) -> None:
    # Compile the actual new-table schema, without claiming server DDL execution.
    table = Table("multirag_graph", MetaData(), *[column._copy() for column in connection.get_column_definitions()])
    assert isinstance(table.c.n_hop_with_weight.type, LONGTEXT)
    assert table.c.n_hop_with_weight.nullable
    assert "n_hop_with_weight LONGTEXT" in str(CreateTable(table).compile(dialect=dialect()))


def test_schema_lock_factory_keeps_the_backend_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    lock_factory = Mock()
    monkeypatch.setattr(redis_conn, "RedisDistributedLock", lock_factory)
    assert resources.redis_ddl_lock("owned-ddl-lock") is lock_factory.return_value
    lock_factory.assert_called_once_with("owned-ddl-lock")


@pytest.fixture
def old_schema(connection: Any, monkeypatch: pytest.MonkeyPatch) -> tuple[set[str], Mock]:
    columns = set(ob_conn.column_names) - {"n_hop_with_weight"}
    connection.client.check_table_exists.return_value = True
    connection._column_exist = lambda _table, name: name in columns
    lock = Mock()
    lock.acquire.return_value = True
    lock_factory = Mock(return_value=lock)
    # Only Redis is replaced: run the real readiness/create_idx/add-column chain.
    monkeypatch.setattr(redis_conn, "RedisDistributedLock", lock_factory)

    def add_columns(*, table_name: str, columns: list[Any]) -> None:
        assert table_name == "multirag_graph"
        assert len(columns) == 1 and isinstance(columns[0].type, LONGTEXT)
        assert columns[0].nullable
        old_columns.update(column.name for column in columns)

    old_columns = columns
    connection.client.add_columns.side_effect = add_columns
    return columns, lock_factory


def test_old_schema_is_unready_until_nhop_column_is_added(connection: Any, old_schema: tuple[set[str], Mock]) -> None:
    columns, locks = old_schema
    assert not connection.index_exist("multirag_graph", "kb")
    assert "multirag_graph" not in connection._table_exists_cache
    connection.create_idx("multirag_graph", "kb", 2)
    assert "n_hop_with_weight" in columns
    assert connection.index_exist("multirag_graph", "kb")
    assert "multirag_graph" in connection._table_exists_cache
    connection.client.refresh_metadata.assert_called_once_with(["multirag_graph"])
    locks.assert_called_once_with("ob_add_n_hop_with_weight_multirag_graph")
    locks.return_value.release.assert_called_once_with()
    connection.create_idx("multirag_graph", "kb", 2)
    connection.client.add_columns.assert_called_once()


def test_failed_nhop_ddl_raises_and_leaves_schema_unready(connection: Any, old_schema: tuple[set[str], Mock]) -> None:
    columns, locks = old_schema
    connection.client.add_columns.side_effect = RuntimeError("controlled DDL failure")
    with pytest.raises(Exception, match="controlled DDL failure"):
        connection.create_idx("multirag_graph", "kb", 2)
    assert "n_hop_with_weight" not in columns
    assert not connection.index_exist("multirag_graph", "kb")
    assert "multirag_graph" not in connection._table_exists_cache
    connection.client.refresh_metadata.assert_called_once_with(["multirag_graph"])
    locks.return_value.release.assert_called_once_with()


@pytest.mark.parametrize("wildcard", [False, True])
def test_nhop_is_a_first_class_write_and_projected_into_kg_consumer(connection: Any, wildcard: bool) -> None:
    paths = [{"path": ["ALICE", "BOB", "CAROL"], "weights": [3.0, 7.0]}]
    entity = {
        "id": "entity-alice",
        "kb_id": "kb",
        "knowledge_graph_kwd": "entity",
        "entity_kwd": "ALICE",
        "content_with_weight": json.dumps({"description": "Alice knows Bob"}),
        "rank_flt": 0.42,
        "n_hop_with_weight": json.dumps(paths),
    }
    assert connection.insert([entity], "multirag_graph", "kb") == []
    stored = connection.client.upsert.call_args.args[1][0]
    assert stored["n_hop_with_weight"] == entity["n_hop_with_weight"]
    assert not stored.get("extra")
    fields = ob_conn.column_names.copy() if wildcard else ["id", "content_with_weight", "entity_kwd", "rank_flt", "n_hop_with_weight"]
    connection._check_table_exists_cached = Mock(return_value=True)
    connection._execute_search_sql = Mock(side_effect=[([(1,)], 0.0), ([tuple(stored[field] for field in fields)], 0.0)])
    result = connection.search(["*"] if wildcard else fields, [], {"knowledge_graph_kwd": "entity"}, [], OrderByExpr(), 0, 10, ["multirag_graph"], ["kb"])
    sql = connection._execute_search_sql.call_args.args[0]
    assert "n_hop_with_weight" in sql.split(" FROM ")[0]
    assert "kb_id IN ('kb')" in sql and "knowledge_graph_kwd = 'entity'" in sql
    assert connection.get_fields(result, ["n_hop_with_weight"])["entity-alice"] == {"n_hop_with_weight": entity["n_hop_with_weight"]}
    consumer = KGSearch(connection)._ent_info_from_(result, sim_thr=0)
    assert consumer["ALICE"]["n_hop_ents"] == paths
    assert consumer["ALICE"]["pagerank"] == 0.42
