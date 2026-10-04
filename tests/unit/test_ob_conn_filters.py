"""OceanBase query construction contracts; SQL execution is captured, not emulated."""

from typing import Any
from unittest.mock import MagicMock

import pytest

from common.doc_store.doc_store_base import FusionExpr, MatchDenseExpr, MatchExpr, MatchTextExpr, OrderByExpr
from core.utils import ob_conn


def _connection() -> Any:
    cls = next(cell.cell_contents for cell in ob_conn.OBConnection.__closure__ if isinstance(cell.cell_contents, type))
    conn = object.__new__(cls)
    conn.enable_fulltext_search = True
    conn.es = None
    conn.use_fulltext_hint = False
    conn._fulltext_search_columns = ob_conn.FTS_COLUMNS_ORIGIN
    conn._check_table_exists_cached = MagicMock(return_value=True)
    conn._execute_search_sql = MagicMock(side_effect=[([(1,)], 0.0), ([], 0.0)])
    conn.client = MagicMock()
    return conn


def _search(conn: Any, expressions: list[MatchExpr] | None = None, **kwargs: Any) -> ob_conn.SearchResult:
    args = {
        "select_fields": ["id"],
        "highlight_fields": [],
        "condition": {},
        "match_expressions": expressions or [],
        "order_by": OrderByExpr(),
        "offset": 0,
        "limit": 10,
        "index_names": ["multirag_tenant"],
        "knowledgebase_ids": ["kb1"],
    }
    args.update(kwargs)
    return conn.search(**args)


@pytest.mark.parametrize("column", list(dict.fromkeys(ob_conn.column_names + ob_conn.doc_meta_column_names)))
def test_defined_columns_are_accepted(column: str) -> None:
    assert ob_conn.get_filters({"exists": column}) == [f"{column} IS NOT NULL"]
    assert ob_conn.get_filters({"must_not": {"exists": column}}) == [f"{column} IS NULL"]
    assert ob_conn.get_filters({column: "value"})


@pytest.mark.parametrize("column", ["unknown", "id OR 1=1 --", "id) IS NULL; DROP TABLE chunks; --", "q_3_vec"])
def test_unknown_or_sql_filter_keys_are_ignored(column: str) -> None:
    for condition in ({column: "x"}, {column: ["x"]}, {"exists": column}, {"must_not": {"exists": column}}):
        assert ob_conn.get_filters(condition) == []


@pytest.mark.parametrize("value", [["id"], {"id": "x"}, 1, True])
def test_exists_requires_a_column_string(value: Any) -> None:
    assert ob_conn.get_filters({"exists": value, "must_not": {"exists": value}}) == []


def test_scalar_list_array_and_empty_contracts() -> None:
    assert ob_conn.get_filters({"id": ["a", "b"], "source_id": ["s1", "s2"], "tag_kwd": "tag", "_order_id": 2, "group_id": "g", "mom_id": "m"}) == [
        "id IN ('a', 'b')",
        "(array_contains(source_id, 's1') OR array_contains(source_id, 's2'))",
        "array_contains(tag_kwd, 'tag')",
        "_order_id = 2",
        "group_id = 'g'",
        "mom_id = 'm'",
    ]
    # Existing falsy-value handling is intentionally retained.
    assert ob_conn.get_filters({"id": [], "available_int": 0, "doc_id": "", "metadata": None}) == []


def test_values_are_escaped_independently_of_column_allowlist() -> None:
    assert ob_conn.get_filters({"docnm_kwd": "O'Reilly\\draft", "id": ["x' OR 1=1 --"], "tag_kwd": "a'b"}) == [
        "docnm_kwd = 'O\\'Reilly\\\\draft'",
        "id IN ('x\\' OR 1=1 --')",
        "array_contains(tag_kwd, 'a\\'b')",
    ]
    assert ob_conn.get_filters({"meta_fields": {"author": "O'Reilly"}}) == ['meta_fields = \'{\\"author\\": \\"O\\\'Reilly\\"}\'']


def test_metadata_paths_keep_dotted_semantics_and_escape_sql_values() -> None:
    condition = {
        "logical_operator": "or",
        "conditions": [
            {"name": "author.name", "comparison_operator": "is", "value": "O'Reilly"},
            {"name": "x') OR 1=1 --", "comparison_operator": ">", "value": 3},
        ],
    }
    assert ob_conn.get_filters({"metadata_filtering_conditions": condition}) == [
        "(JSON_EXTRACT(metadata, '$.author.name') = 'O\\'Reilly' OR CAST(JSON_EXTRACT(metadata, '$.x\\') OR 1=1 --') AS DECIMAL(20,10)) > 3)"
    ]
    with pytest.raises(ValueError, match="Unsupported logical operator"):
        ob_conn.get_metadata_filter_expression({**condition, "logical_operator": "OR 1=1"})


def test_search_and_update_use_constrained_filters() -> None:
    conn = _connection()
    _search(conn, condition={"id": ["chunk1"], "id OR 1=1 --": "bad"})
    sql = conn._execute_search_sql.call_args_list[1].args[0]
    assert "id IN ('chunk1') AND kb_id IN ('kb1')" in sql
    assert "OR 1=1" not in sql
    assert conn.update({"doc_id": "doc1"}, {"metadata": {"_group_id": "group1", "_title": "title1"}}, "multirag_tenant", "kb1")
    sql = conn.client.perform_raw_text_sql.call_args.args[0]
    assert "group_id = 'group1', docnm_kwd = 'title1'" in sql
    assert "WHERE doc_id = 'doc1' AND kb_id = 'kb1'" in sql
    assert conn.update({"id": "doc1"}, {"meta_fields": {"author": "Alice"}}, "multirag_doc_meta_kb1", "kb1")
    assert "WHERE id = 'doc1'" in conn.client.perform_raw_text_sql.call_args.args[0]
    assert "kb_id =" not in conn.client.perform_raw_text_sql.call_args.args[0]


@pytest.mark.parametrize("index_names", [[], None, 1])
def test_search_invalid_indices_raise_value_error(index_names: Any) -> None:
    with pytest.raises(ValueError, match="index_names"):
        _search(_connection(), index_names=index_names)


@pytest.mark.parametrize(
    ("expressions", "error"),
    [
        ([MatchTextExpr(["content_ltks"], "hello", 10)], "original_query"),
        ([MatchDenseExpr("q_3_vec", [0.1, 0.2, 0.3], "int", "cosine")], "not float"),
        ([MatchDenseExpr("q_3_vec", [0.1, 0.2, 0.3], "float", "cosine", extra_options={"similarity": "0 OR 1=1"})], "could not convert"),
    ],
)
def test_invalid_search_expressions_fail_before_sql(expressions: list[MatchExpr], error: str) -> None:
    conn = _connection()
    with pytest.raises(ValueError, match=error):
        _search(conn, expressions)
    conn._execute_search_sql.assert_not_called()


def test_hybrid_expression_order_is_validated() -> None:
    conn = _connection()
    conn.es = MagicMock()
    expressions = [MatchDenseExpr("q_3_vec", [0.1, 0.2, 0.3], "float", "cosine"), MatchTextExpr(["content_ltks"], "hello", 10), FusionExpr("weighted_sum", 10, {"weights": "0.5,0.5"})]
    with pytest.raises(ValueError, match="must contain MatchTextExpr"):
        _search(conn, expressions)
    conn.es.search.assert_not_called()


def test_vector_threshold_is_numeric_in_executed_queries() -> None:
    conn = _connection()
    _search(conn, [MatchDenseExpr("q_3_vec", [0.1, 0.2, 0.3], "float", "cosine", extra_options={"similarity": "0.50"})])
    assert len(conn._execute_search_sql.call_args_list) == 2
    for call in conn._execute_search_sql.call_args_list:
        assert ">= 0.5" in call.args[0]
        assert ">= 0.50" not in call.args[0]


def test_multiple_aggregations_raise_before_sql() -> None:
    conn = _connection()
    with pytest.raises(ValueError, match="Only one aggregation field"):
        _search(conn, agg_fields=["doc_id", "kb_id"])
    conn.client.perform_raw_text_sql.assert_not_called()


@pytest.mark.parametrize(
    ("new_value", "error"),
    [
        ({"remove": ["tag_kwd"]}, "Expected str or dict for 'remove'"),
        ({"remove": {"doc_id": "x"}}, "not an array column"),
        ({"add": ["tag_kwd"]}, "Expected str or dict for 'add'"),
        ({"add": {"doc_id": "x"}}, "not an array column"),
        ({"metadata": "x"}, "Expected dict for 'metadata'"),
    ],
)
def test_invalid_updates_raise_before_sql(new_value: dict, error: str) -> None:
    conn = _connection()
    with pytest.raises(ValueError, match=error):
        conn.update({"id": "chunk1"}, new_value, "multirag_tenant", "kb1")
    conn.client.perform_raw_text_sql.assert_not_called()


def test_valid_array_updates_keep_value_escaping() -> None:
    conn = _connection()
    assert conn.update({"id": "chunk1"}, {"remove": {"tag_kwd": "a'b"}, "add": {"source_id": "source1"}}, "multirag_tenant", "kb1")
    sql = conn.client.perform_raw_text_sql.call_args.args[0]
    assert "tag_kwd = array_remove(tag_kwd, 'a\\'b'), source_id = array_append(source_id, 'source1')" in sql
