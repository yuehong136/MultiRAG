"""Capture real memory backend filters without connecting to search services."""

import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from common.doc_store.doc_store_base import MatchDenseExpr
from memory.utils import es_conn as memory_es
from memory.utils import milvus_conn as memory_milvus


def _connection(factory: Any) -> Any:
    cls = next(cell.cell_contents for cell in factory.__closure__ if isinstance(cell.cell_contents, type))
    return object.__new__(cls)


def _es_search(condition: dict[str, Any], *, dense: bool = False) -> dict[str, Any]:
    conn = _connection(memory_es.ESConnection)
    conn.logger = MagicMock()
    conn.index_exist = MagicMock(return_value=True)
    conn.es = MagicMock()
    conn.es.search.return_value = {"hits": {"hits": [], "total": {"value": 0}}, "timed_out": False}
    expressions = [MatchDenseExpr("q_2_vec", [1.0, 0.0], "float", "cosine", topn=1)] if dense else []
    conn.search(["message_id"], [], condition, expressions, None, 0, 1, "memory_owner", ["bank"])
    return conn.es.search.call_args.kwargs["body"]


@pytest.mark.parametrize("field", ["user_id", "agent_id", "session_id"])
@pytest.mark.parametrize("value", ["", "session", 'quote"\\suffix'])
@pytest.mark.parametrize("dense", [False, True])
def test_es_exact_scope_lists_are_filters_before_top_n(field: str, value: str, dense: bool) -> None:
    query = _es_search({field: [value]}, dense=dense)
    filters = query["query"]["bool"]["filter"]

    assert {"terms": {field: [value]}} in filters
    assert not any("query_string" in clause for clause in filters)
    assert query["size"] == 1
    if dense:
        assert query["knn"]["filter"]["bool"]["filter"] == filters
        assert query["knn"]["k"] == 1


def test_es_exact_session_does_not_admit_substring_competitors() -> None:
    query = _es_search({"session_id": ["session"]}, dense=True)
    filters = query["knn"]["filter"]["bool"]["filter"]
    session_terms = next(clause["terms"]["session_id"] for clause in filters if "session_id" in clause.get("terms", {}))

    assert "session" in session_terms
    assert "prefix-session-suffix" not in session_terms
    assert "" not in session_terms


def test_es_legacy_scalar_session_keeps_substring_query() -> None:
    filters = _es_search({"session_id": "session"})["query"]["bool"]["filter"]

    assert {"query_string": {"query": "*session*", "fields": ["session_id"], "analyze_wildcard": True}} in filters


def test_es_legacy_empty_scalar_session_remains_unfiltered() -> None:
    filters = _es_search({"session_id": ""})["query"]["bool"]["filter"]

    assert filters == [{"terms": {"memory_id": ["bank"]}}]


@pytest.mark.parametrize("field", ["user_id", "agent_id", "session_id"])
@pytest.mark.parametrize("value", ["", "session", 'quote"\\suffix', 'scope" or session_id != "" or "\\'])
def test_milvus_exact_scope_lists_preserve_empty_and_escape_literals(field: str, value: str) -> None:
    conn = _connection(memory_milvus.MilvusConnection)

    expression = conn._build_filter_expr({field: [value]}, "bank")

    assert expression == f'memory_id == "bank" and forget_at == "" and {field} in {json.dumps([value])}'
    assert json.loads(expression.split(f"{field} in ", 1)[1]) == [value]


@pytest.mark.parametrize("field", ["user_id", "agent_id", "session_id"])
def test_milvus_scalar_strings_and_memory_binding_are_escaped(field: str) -> None:
    conn = _connection(memory_milvus.MilvusConnection)
    value = 'quoted"\\中文'
    memory_id = 'bank"\\suffix'

    expression = conn._build_filter_expr({field: value}, memory_id, hide_forgotten=False)

    assert expression == f"memory_id == {json.dumps(memory_id)} and {field} == {json.dumps(value)}"


def test_milvus_session_list_keeps_exact_values_and_other_filters() -> None:
    conn = _connection(memory_milvus.MilvusConnection)

    expression = conn._build_filter_expr({"session_id": ["session"], "user_id": [""], "status": [0], "source_id": [None, "2"]}, "bank")

    assert expression == 'memory_id == "bank" and forget_at == "" and session_id in ["session"] and user_id in [""] and status_int in [0] and source_id in [0, 2]'


def test_milvus_legacy_empty_scalar_session_remains_unfiltered() -> None:
    conn = _connection(memory_milvus.MilvusConnection)

    assert conn._build_filter_expr({"session_id": ""}, "bank") == 'memory_id == "bank" and forget_at == ""'
