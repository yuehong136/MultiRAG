"""Direct Infinity reads preserve the REST chunk identity and storage failures."""

from __future__ import annotations

import logging
import sys
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from infinity.common import InfinityException
from infinity.errors import ErrorCode

from core.utils.infinity_conn import InfinityConnection


def connection(tables: dict[str, pd.DataFrame | Exception]) -> tuple[Any, list[str], list[Any], list[str]]:
    cls = next(cell.cell_contents for cell in InfinityConnection.__closure__ if isinstance(cell.cell_contents, type))
    store = object.__new__(cls)
    visited: list[str] = []
    releases: list[Any] = []
    predicates: list[str] = []

    def get_table(name: str) -> Any:
        visited.append(name)
        value = tables.get(name, InfinityException(ErrorCode.TABLE_NOT_EXIST, "not found"))
        if isinstance(value, Exception):
            raise value

        def output(fields: list[str]) -> Any:
            assert fields == ["*"]

            def filter_rows(predicate: str) -> Any:
                predicates.append(predicate)
                return SimpleNamespace(to_df=lambda: (value.copy(), None))

            return SimpleNamespace(filter=filter_rows)

        return SimpleNamespace(output=output)

    remote = SimpleNamespace(get_database=lambda name: SimpleNamespace(get_table=get_table))
    store.dbName = "owned-unit"
    store.logger = logging.getLogger(__name__)
    store.connPool = SimpleNamespace(get_conn=lambda: remote, release_conn=releases.append)
    return store, visited, releases, predicates


@pytest.mark.parametrize("index_name", ["chunks", ["chunks"]])
def test_get_preserves_identity_and_logical_fields(index_name: str | list[str]) -> None:
    store, visited, releases, _ = connection({"chunks_kb": pd.DataFrame([{"id": "chunk", "doc_id": "doc", "content": "body", "available_int": 0}])})
    chunk = store.get("chunk", index_name, ["kb"])
    assert chunk["id"] == "chunk" and chunk["doc_id"] == "doc"
    assert chunk["content_with_weight"] == "body" and chunk["available_int"] == 0
    assert visited == ["chunks_kb"] and len(releases) == 1


def test_get_uses_bound_pairs_without_crossing_dataset_indices() -> None:
    store, visited, releases, _ = connection({"one_a": pd.DataFrame(), "two_b": pd.DataFrame([{"id": "chunk", "content": "bound"}])})
    assert store.get("chunk", ["one", "two"], ["a", "b"])["id"] == "chunk"
    assert visited == ["one_a", "two_b"] and len(releases) == 1


def test_get_rejects_ambiguous_binding_before_borrowing_connection() -> None:
    store, visited, releases, _ = connection({})
    with pytest.raises(ValueError, match="binding is ambiguous"):
        store.get("chunk", ["one", "two"], ["a", "b", "c"])
    assert visited == releases == []


@pytest.mark.parametrize("table", [pd.DataFrame(columns=["id"]), InfinityException(ErrorCode.TABLE_NOT_EXIST, "not found")])
def test_get_absent_row_or_table_is_none(table: pd.DataFrame | Exception) -> None:
    store, _, releases, _ = connection({"chunks_kb": table})
    assert store.get("absent", "chunks", ["kb"]) is None
    assert len(releases) == 1


@pytest.mark.parametrize("failure", [ConnectionError("transport failed"), InfinityException(ErrorCode.TXN_CONFLICT, "transaction conflict")])
def test_get_storage_error_propagates_and_returns_connection(failure: Exception) -> None:
    store, _, releases, _ = connection({"chunks_kb": failure})
    with pytest.raises(type(failure)) as caught:
        store.get("chunk", "chunks", ["kb"])
    assert caught.value is failure and len(releases) == 1


def test_get_metadata_table_and_quoted_id() -> None:
    store, visited, releases, predicates = connection({"metadata": pd.DataFrame([{"id": "O'Reilly", "content": "body"}])})
    assert store.get("O'Reilly", "metadata", [""])["id"] == "O'Reilly"
    assert visited == ["metadata"] and predicates == ["id = 'O''Reilly'"] and len(releases) == 1


def test_chunk_detail_consumes_infinity_get_with_the_actual_index_list(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    module = sys.modules["api.apps.restful_apis.chunk"]
    store, visited, releases, _ = connection({"multirag_tenant_dataset_kb": pd.DataFrame([{"id": "chunk", "doc_id": "doc", "content": "body", "q_2_vec": [0.0, 1.0]}])})
    monkeypatch.setattr(module, "_read_context", lambda *args: ({"tenant_id": "tenant", "name": "dataset"}, {}))
    monkeypatch.setattr(module.settings, "docStoreConn", store)
    response = client.get("/api/v1/datasets/kb/documents/doc/chunks/chunk")
    assert response.status_code == 200 and response.json()["code"] == 0
    assert response.json()["data"]["id"] == "chunk" and response.json()["data"]["content_with_weight"] == "body"
    assert "q_2_vec" not in response.json()["data"]
    assert visited == ["multirag_tenant_dataset_kb"] and len(releases) == 1
