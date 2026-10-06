"""Infinity predicates exercised against an isolated database with SDK readback."""

import logging
import os
import socket
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import infinity
import pytest
from infinity.common import ConflictType, NetworkAddress
from infinity.index import IndexInfo, IndexType

from common.config_utils import CONFIGS
from common.doc_store.doc_store_base import FusionExpr, MatchDenseExpr, MatchTextExpr, OrderByExpr
from core.utils.infinity_conn import InfinityConnection


class OwnedPool:
    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def get_conn(self) -> Any:
        return self.connection

    def release_conn(self, connection: Any) -> None:
        assert connection is self.connection


@pytest.fixture
def infinity_scratch() -> Iterator[tuple[Any, Any]]:
    # A caller can select a temporary container without changing shared config.
    uri = os.environ.get("INFINITY_TEST_URI", CONFIGS.get("infinity", {}).get("uri", "localhost:23817"))
    host, port = uri.rsplit(":", 1)
    try:
        with socket.create_connection((host, int(port)), timeout=2):
            pass
    except OSError:
        if os.environ.get("INFINITY_TEST_URI"):
            pytest.fail(f"Selected Infinity service is unavailable: {uri}")
        pytest.skip("Infinity service unavailable; select one with INFINITY_TEST_URI")

    writer = infinity.connect(NetworkAddress(host, int(port)))
    reader = infinity.connect(NetworkAddress(host, int(port)))
    database_name = "zero_filter_" + uuid4().hex
    try:
        database = writer.create_database(database_name, ConflictType.Error)
        table = database.create_table(
            "chunks_kb",
            {
                "id": {"type": "varchar"},
                "doc_id": {"type": "varchar"},
                "available_int": {"type": "integer"},
                "content": {"type": "varchar"},
                "q_2_vec": {"type": "vector,2,float"},
                "pagerank_fea": {"type": "float", "default": 0.0},
                "marker": {"type": "varchar", "default": "original"},
            },
            ConflictType.Error,
        )
        table.create_index("ft_content_rag_coarse", IndexInfo("content", IndexType.FullText, {"ANALYZER": "rag"}), ConflictType.Error)
        table.insert(
            [
                {"id": "hidden", "doc_id": "doc", "available_int": 0, "content": "availability regression", "q_2_vec": [0.0, 1.0]},
                {"id": "visible", "doc_id": "doc", "available_int": 1, "content": "availability regression", "q_2_vec": [0.0, 1.0]},
                {"id": "foreign", "doc_id": "other", "available_int": 0, "content": "availability regression", "q_2_vec": [0.0, 1.0]},
            ]
        )
        cls = next(cell.cell_contents for cell in InfinityConnection.__closure__ if isinstance(cell.cell_contents, type))
        store = object.__new__(cls)
        store.connPool = OwnedPool(writer)
        store.dbName = database_name
        store.logger = logging.getLogger(__name__)
        yield store, reader.get_database(database_name).get_table("chunks_kb")
    finally:
        writer.get_database("default_db")
        reader.get_database("default_db")
        writer.drop_database(database_name, ConflictType.Ignore)
        assert database_name not in reader.list_databases().db_names
        reader.disconnect()
        writer.disconnect()


@pytest.mark.parametrize("text_search", [False, True])
@pytest.mark.parametrize("availability", [0, 1, None])
def test_search_filters_rows_and_total(infinity_scratch: tuple[Any, Any], availability: int | None, text_search: bool) -> None:
    store, _ = infinity_scratch
    condition = {"doc_id": ["doc"], "kb_id": ["kb"]}
    if availability is not None:
        condition["available_int"] = availability
    expressions = [MatchTextExpr(["content_ltks"], "regression", 10, {})] if text_search else []
    result = store.search(["id", "available_int"], [], condition, expressions, OrderByExpr(), 0, 10, ["chunks"], ["kb"])
    expected = {"hidden", "visible"} if availability is None else {"hidden" if availability == 0 else "visible"}
    assert set(store.get_doc_ids(result)) == expected
    assert store.get_total(result) == len(expected)


def test_update_only_matching_disabled_rows_with_independent_readback(infinity_scratch: tuple[Any, Any]) -> None:
    store, table = infinity_scratch
    assert store.update({"doc_id": ["doc"], "available_int": 0}, {"marker": "changed"}, "chunks", "kb") is True
    data = table.output(["id", "available_int", "marker"]).to_result()[0]
    rows = {identifier: (available, marker) for identifier, available, marker in zip(data["id"], data["available_int"], data["marker"], strict=True)}
    assert rows == {"hidden": (0, "changed"), "visible": (1, "original"), "foreign": (0, "original")}


def test_delete_only_matching_disabled_rows_with_independent_readback(infinity_scratch: tuple[Any, Any]) -> None:
    store, table = infinity_scratch
    assert store.delete({"doc_id": ["doc"], "available_int": 0}, "chunks", "kb") == 1
    assert set(table.output(["id"]).to_result()[0]["id"]) == {"visible", "foreign"}


@pytest.mark.parametrize("fusion", [False, True])
def test_vector_scope_does_not_require_a_lexical_match(infinity_scratch: tuple[Any, Any], fusion: bool) -> None:
    store, reader = infinity_scratch
    writer = store.connPool.get_conn().get_database(store.dbName).get_table("chunks_kb")
    writer.insert(
        [
            {"id": "semantic", "doc_id": "doc", "available_int": 1, "content": "different words", "q_2_vec": [0.8, 0.6]},
            {"id": "outside", "doc_id": "other", "available_int": 1, "content": "availability regression", "q_2_vec": [1.0, 0.0]},
            {"id": "disabled-vector", "doc_id": "doc", "available_int": 0, "content": "availability regression", "q_2_vec": [1.0, 0.0]},
        ]
    )
    dense = MatchDenseExpr("q_2_vec", [1.0, 0.0], "float", "cosine", 10, {"similarity": 0.75})
    expressions = [MatchTextExpr(["content_ltks"], "regression", 10), dense, FusionExpr("weighted_sum", 10, {"weights": "0.5,0.5"})] if fusion else [dense]
    result = store.search(["id", "doc_id", "available_int"], [], {"doc_id": ["doc"], "available_int": 1}, expressions, OrderByExpr(), 0, 10, ["chunks"], ["kb"])
    assert set(store.get_doc_ids(result)) == ({"semantic", "visible"} if fusion else {"semantic"})
    raw = reader.output(["id", "doc_id", "available_int"]).to_result()[0]
    assert set(raw["id"]) == {"hidden", "visible", "foreign", "semantic", "outside", "disabled-vector"}


def test_vector_pagination_merges_tables_before_offset(infinity_scratch: tuple[Any, Any]) -> None:
    store, _ = infinity_scratch
    database = store.connPool.get_conn().get_database(store.dbName)
    other = database.create_table(
        "chunks_second",
        {"id": {"type": "varchar"}, "doc_id": {"type": "varchar"}, "available_int": {"type": "integer"}, "q_2_vec": {"type": "vector,2,float"}, "pagerank_fea": {"type": "float", "default": 0.0}},
    )
    database.get_table("chunks_kb").insert([{"id": "best", "doc_id": "doc", "available_int": 1, "content": "different words", "q_2_vec": [1.0, 0.0]}])
    other.insert(
        [
            {"id": "second", "doc_id": "doc", "available_int": 1, "q_2_vec": [0.9, 0.4358899]},
            {"id": "third", "doc_id": "doc", "available_int": 1, "q_2_vec": [0.8, 0.6]},
        ]
    )
    pages = []
    for offset in (0, 1):
        result = store.search(
            ["id", "doc_id"], [], {"doc_id": ["doc"], "available_int": 1}, [MatchDenseExpr("q_2_vec", [1.0, 0.0], "float", "cosine", 10)], OrderByExpr(), offset, 1, ["chunks"], ["kb", "second"]
        )
        pages.extend(store.get_doc_ids(result))
    assert pages == ["best", "second"]
    assert set(other.output(["id"]).to_result()[0]["id"]) == {"second", "third"}


@pytest.mark.parametrize("weights", ["0.9,0.1", "0.1,0.9"])
def test_fusion_weights_reverse_lexical_vector_ranking(infinity_scratch: tuple[Any, Any], weights: str) -> None:
    store, reader = infinity_scratch
    writer = store.connPool.get_conn().get_database(store.dbName).get_table("chunks_kb")
    writer.insert([{"id": "semantic", "doc_id": "doc", "available_int": 1, "content": "different words", "q_2_vec": [1.0, 0.0]}])
    expressions = [MatchTextExpr(["content_ltks"], "regression", 10), MatchDenseExpr("q_2_vec", [1.0, 0.0], "float", "cosine", 10), FusionExpr("weighted_sum", 10, {"weights": weights})]
    result = store.search(["id", "doc_id"], [], {"doc_id": ["doc"], "available_int": 1}, expressions, OrderByExpr(), 0, 10, ["chunks"], ["kb"])
    assert store.get_doc_ids(result) == (["visible", "semantic"] if weights == "0.9,0.1" else ["semantic", "visible"])
    assert set(reader.output(["id"]).to_result()[0]["id"]) == {"hidden", "visible", "foreign", "semantic"}


@pytest.mark.parametrize("phase", ["table", "schema", "scatter", "query"])
def test_search_rpc_failure_is_not_empty_with_independent_readback(infinity_scratch: tuple[Any, Any], monkeypatch: pytest.MonkeyPatch, phase: str) -> None:
    store, reader = infinity_scratch
    source = reader.output(["id", "doc_id", "available_int", "q_2_vec"]).to_result()[0]
    pool = store.connPool
    database = pool.get_conn().get_database(store.dbName)
    failure = ConnectionError(f"injected {phase} RPC transport failure")
    lookups = 0
    releases = []

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise failure

    def get_table(name: str) -> Any:
        nonlocal lookups
        lookups += 1
        if phase == "table" or (phase == "scatter" and lookups == 2):
            raise failure
        table = database.get_table(name)
        if phase == "schema":
            return SimpleNamespace(show_columns=fail)
        if phase == "query":

            def output(fields: list[str]) -> Any:
                builder = table.output(fields)
                monkeypatch.setattr(builder, "to_df", fail)
                return builder

            return SimpleNamespace(show_columns=table.show_columns, output=output)
        return table

    connection = SimpleNamespace(get_database=lambda _: SimpleNamespace(get_table=get_table))
    monkeypatch.setattr(store, "connPool", SimpleNamespace(get_conn=lambda: connection, release_conn=releases.append))

    with pytest.raises(ConnectionError) as caught:
        store.search(["id"], [], {"doc_id": ["doc"], "available_int": 1}, [MatchDenseExpr("q_2_vec", [0.0, 1.0], "float", "cosine", 10)], OrderByExpr(), 0, 10, ["chunks"], ["kb"])

    assert caught.value is failure
    assert releases == [connection]
    assert reader.output(["id", "doc_id", "available_int", "q_2_vec"]).to_result()[0] == source


def test_dense_zero_matches_remain_successful_with_independent_readback(infinity_scratch: tuple[Any, Any]) -> None:
    store, reader = infinity_scratch
    source = reader.output(["id", "doc_id", "available_int"]).to_result()[0]

    result = store.search(["id"], [], {"doc_id": ["absent"], "available_int": 1}, [MatchDenseExpr("q_2_vec", [0.0, 1.0], "float", "cosine", 10)], OrderByExpr(), 0, 10, ["chunks"], ["kb"])

    assert store.get_total(result) == 0
    assert store.get_doc_ids(result) == []
    assert reader.output(["id", "doc_id", "available_int"]).to_result()[0] == source


async def test_dealer_queries_bound_index_dataset_pairs_with_independent_readback(infinity_scratch: tuple[Any, Any]) -> None:
    from unittest.mock import Mock

    import numpy as np

    from core.nlp.search import Dealer

    store, _ = infinity_scratch
    database = store.connPool.get_conn().get_database(store.dbName)
    uri = os.environ.get("INFINITY_TEST_URI", CONFIGS.get("infinity", {}).get("uri", "localhost:23817"))
    host, port = uri.rsplit(":", 1)
    reader = infinity.connect(NetworkAddress(host, int(port)))
    try:
        # A perfect vector in an existing cross-pair must remain unselected.
        for name, identifier, kb_id, vector in (
            ("index_a_first", "chosen-a", "first", [0.9, 0.4358899]),
            ("index_b_second", "chosen-b", "second", [0.8, 0.6]),
            ("index_a_second", "outside", "second", [1.0, 0.0]),
        ):
            table = database.create_table(
                name,
                {
                    "id": {"type": "varchar"},
                    "doc_id": {"type": "varchar"},
                    "kb_id": {"type": "varchar"},
                    "docnm": {"type": "varchar"},
                    "available_int": {"type": "integer"},
                    "q_2_vec": {"type": "vector,2,float"},
                    "pagerank_fea": {"type": "float", "default": 0.0},
                },
            )
            table.insert([{"id": identifier, "doc_id": identifier, "kb_id": kb_id, "docnm": identifier, "available_int": 1, "q_2_vec": vector}])
        dealer = Dealer(store)
        encode = Mock(return_value=(np.array([1.0, 0.0]), 0))
        embedding = SimpleNamespace(encode_queries=encode)
        for page, expected in ((1, "chosen-a"), (2, "chosen-b")):
            result = await dealer.search(
                {"question": "query", "fields": ["id", "doc_id", "kb_id", "docnm_kwd"], "kb_ids": ["first", "second"], "available_int": 1, "search_mode": {"dense": {}}, "page": page, "size": 1},
                ["index_a", "index_b"],
                ["first", "second"],
                embedding,
            )
            assert result.ids == [expected] and result.total == 2
        assert encode.call_count == 2, "One embedding call per multi-dataset request"
        raw = reader.get_database(store.dbName).get_table("index_a_second").output(["id", "kb_id"]).to_result()[0]
        assert raw == {"id": ["outside"], "kb_id": ["second"]}
    finally:
        reader.disconnect()
