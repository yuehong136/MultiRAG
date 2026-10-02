"""Complete history reads fail closed and recovery checks the actual contents."""

import copy
from types import SimpleNamespace
from typing import Any

import pytest

from common.doc_store import document_history as history


def response(rows: list[dict[str, Any]], count: int) -> dict[str, Any]:
    return {
        "timed_out": False,
        "_scroll_id": "owned-cursor",
        "_shards": {"total": 1, "successful": 1, "failed": 0},
        "hits": {"total": {"value": count, "relation": "eq"}, "hits": [{"_id": row["id"], "_source": copy.deepcopy(row)} for row in rows]},
    }


@pytest.mark.parametrize("backend", ["elasticsearch", "opensearch"])
def test_scroll_preserves_complete_payload_and_vectors(backend: str) -> None:
    original = [{"id": "child", "doc_id": "doc", "kb_id": "kb", "mom_id": "parent", "vector": [0.1, 0.2], "create_time": "original", "custom": {"nested": [1, 2]}}]
    cleared = []
    client = SimpleNamespace(search=lambda **kwargs: response(original, 1), scroll=lambda **kwargs: response([], 1), clear_scroll=lambda **kwargs: cleared.append(kwargs))
    store = SimpleNamespace(db_type=lambda: backend, index_exist=lambda *args: True, es=client, os=client)
    rows = history.document_history(store, "index", "kb", "doc")
    assert rows == original and cleared == [{"scroll_id": "owned-cursor"}]
    rows[0]["vector"].append(9)
    assert original[0]["vector"] == [0.1, 0.2]


@pytest.mark.parametrize("fault", ["partial_shard", "timeout", "unknown_total", "truncated", "missing_payload"])
def test_incomplete_scroll_is_never_an_empty_history(fault: str) -> None:
    first = response([], 1 if fault == "truncated" else 0)
    if fault == "partial_shard":
        first["_shards"]["failed"] = 1
    elif fault == "timeout":
        first["timed_out"] = True
    elif fault == "unknown_total":
        first["hits"]["total"]["relation"] = "gte"
    elif fault == "missing_payload":
        first["hits"]["total"]["value"] = 1
        first["hits"]["hits"] = [{"_id": "missing"}]
    cleared = []
    store = SimpleNamespace(db_type=lambda: "elasticsearch", index_exist=lambda *args: True, es=SimpleNamespace(search=lambda **kwargs: first, clear_scroll=lambda **kwargs: cleared.append(kwargs)))
    with pytest.raises(RuntimeError):
        history.document_history(store, "index", "kb", "doc")
    assert cleared == [{"scroll_id": "owned-cursor"}]


@pytest.mark.parametrize("acknowledgement", [True, False, None, "0", -1])
def test_delete_requires_typed_ack_even_when_readback_is_empty(monkeypatch: pytest.MonkeyPatch, acknowledgement: Any) -> None:
    store = SimpleNamespace(delete=lambda *args: acknowledgement)
    monkeypatch.setattr(history, "document_history", lambda *args: [])
    with pytest.raises(RuntimeError, match="deletion failed"):
        history.delete_document_history(store, "index", "kb", "doc")


def test_zero_delete_requires_actual_absence(monkeypatch: pytest.MonkeyPatch) -> None:
    store = SimpleNamespace(delete=lambda *args: 0, db_type=lambda: "milvus")
    monkeypatch.setattr(history, "document_history", lambda *args: [{"id": "still-present"}])
    with pytest.raises(RuntimeError, match="could not be confirmed"):
        history.delete_document_history(store, "index", "kb", "doc")
    monkeypatch.setattr(history, "document_history", lambda *args: [])
    history.delete_document_history(store, "index", "kb", "doc")


@pytest.mark.parametrize("acknowledgement", [False, None, True, ["failed"]])
def test_restore_rejects_nonacknowledged_insert(monkeypatch: pytest.MonkeyPatch, acknowledgement: Any) -> None:
    store = SimpleNamespace(db_type=lambda: "milvus", insert=lambda *args: acknowledgement)
    monkeypatch.setattr(history, "delete_document_history", lambda *args, **kwargs: None)
    with pytest.raises(RuntimeError, match="recovery failed"):
        history.restore_document_history(store, "index", "kb", "doc", [{"id": "original", "vector": [0.2]}])


@pytest.mark.parametrize("backend", ["elasticsearch", "opensearch"])
@pytest.mark.parametrize("fault", [None, "unassigned_replica", "failed_shard", "missing_ack", "exception"])
def test_real_bulk_connector_requires_visibility_before_history_read(backend: str, fault: str | None) -> None:
    import inspect
    import logging

    from core.utils.es_conn import ESConnection
    from core.utils.opensearch_conn import OSConnection

    constructor = ESConnection if backend == "elasticsearch" else OSConnection
    cls = inspect.getclosurevars(constructor).nonlocals["cls"]
    store = object.__new__(cls)
    store.logger = logging.getLogger("ingest-test")
    pending, visible, calls = [], [], []

    def bulk(**kwargs: Any) -> dict[str, Any]:
        assert kwargs["refresh"] is False
        operations = kwargs.get("operations", kwargs.get("body"))
        pending.extend({"id": meta["index"]["_id"], **copy.deepcopy(payload)} for meta, payload in zip(operations[::2], operations[1::2], strict=True))
        calls.append("bulk")
        return {"errors": False}

    def refresh(**kwargs: Any) -> Any:
        assert kwargs["index"] == "index"
        calls.append("refresh")
        if fault == "exception":
            raise ConnectionError("controlled refresh failure")
        if fault == "missing_ack":
            return None
        if fault == "failed_shard":
            return {"_shards": {"total": 2, "successful": 1, "failed": 1}}
        visible[:] = copy.deepcopy(pending)
        return {"_shards": {"total": 2 if fault == "unassigned_replica" else 1, "successful": 1, "failed": 0}}

    client = SimpleNamespace(
        bulk=bulk,
        indices=SimpleNamespace(refresh=refresh, exists=lambda **kwargs: True),
        search=lambda **kwargs: response(visible, len(visible)),
        scroll=lambda **kwargs: response([], len(visible)),
        clear_scroll=lambda **kwargs: None,
    )
    store.es = store.os = client
    row = {"id": "chunk", "doc_id": "doc", "kb_id": "kb", "q_2_vec": [0.2, 0.4], "content_with_weight": "visible after acknowledged refresh"}
    assert store.insert([row], "index", "kb") == []
    assert history.document_history(store, "index", "kb", "doc") == []
    if fault in {None, "unassigned_replica"}:
        history.confirm_history_visibility(store, "index")
        assert history.document_history(store, "index", "kb", "doc") == [row]
    else:
        with pytest.raises((RuntimeError, ConnectionError)):
            history.confirm_history_visibility(store, "index")
        assert visible == []
    assert calls == ["bulk", "refresh"]


@pytest.mark.parametrize("backend", ["oceanbase", "seekdb"])
def test_native_oceanbase_history_preserves_extra_arrays_vectors_and_dates(backend: str) -> None:
    import numpy as np

    original = {
        "id": "chunk",
        "doc_id": "doc",
        "kb_id": "kb",
        "q_2_vec": np.array([0.2, 0.4]),
        "position_int": [[1, 2]],
        "extra": {"nested": {"TOC": ["original"]}},
        "metadata": {"source": "original"},
        "create_time": "2026-10-03 00:00:01",
        "available_int": 0,
        "mom_id": "parent",
    }
    rows = [copy.deepcopy(original)]

    class Result:
        def mappings(self) -> Any:
            return iter(copy.deepcopy(rows))

        def close(self) -> None:
            pass

    def get(**kwargs: Any) -> Result:
        assert kwargs["table_name"] == "index"
        assert [expression.right.value for expression in kwargs["where_clause"]] == ["doc", "kb"]
        return Result()

    def upsert(table: str, data: Any) -> None:
        assert table == "index"
        rows[:] = copy.deepcopy(data)

    def delete(*args: Any) -> int:
        count = len(rows)
        rows.clear()
        return count

    store = SimpleNamespace(db_type=lambda: backend, index_exist=lambda *args: True, get_table_name=lambda *args: "index", client=SimpleNamespace(get=get, upsert=upsert), delete=delete)
    saved = history.document_history(store, "index", "kb", "doc")
    assert saved[0]["q_2_vec"] == [0.2, 0.4]
    history.delete_document_history(store, "index", "kb", "doc")
    assert history.document_history(store, "index", "kb", "doc") == []
    history.restore_document_history(store, "index", "kb", "doc", saved)
    assert history.document_history(store, "index", "kb", "doc") == saved
    store.index_exist = lambda *args: False
    assert history.document_history(store, "index", "kb", "new-doc") == []


def test_native_vastbase_history_restores_raw_columns_without_public_transform(monkeypatch: pytest.MonkeyPatch) -> None:
    import psycopg2.extras

    original = {
        "id": "chunk",
        "doc_id": "doc",
        "kb_id": "kb",
        "position_int": "00000001_00000002",
        "q_2_vec": [0.2, 0.4],
        "metadata": {"nested": ["original"]},
        "create_time": "2026-10-03 00:00:01",
        "available_int": 0,
        "mom_id": "parent",
    }
    rows = [copy.deepcopy(original)]
    releases = []

    class Cursor:
        description = [SimpleNamespace(name=key) for key in original]

        def __enter__(self) -> "Cursor":
            return self

        def __exit__(self, *args: Any) -> None:
            pass

        def execute(self, statement: Any, parameters: Any) -> None:
            assert parameters == ("doc", "kb")

        def fetchall(self) -> Any:
            return [tuple(row[key] for key in original) for row in rows]

    connection = SimpleNamespace(cursor=Cursor, rollback=lambda: None, commit=lambda: None)

    def batch(cursor: Any, statement: Any, values: Any) -> None:
        rows[:] = [{key: value.adapted if isinstance(value, psycopg2.extras.Json) else value for key, value in zip(original, row, strict=True)} for row in values]

    def delete(*args: Any) -> int:
        count = len(rows)
        rows.clear()
        return count

    monkeypatch.setattr(psycopg2.extras, "execute_batch", batch)
    store = SimpleNamespace(
        db_type=lambda: "vastbase",
        schema="owned_schema",
        _get_connection=lambda: connection,
        _release_connection=lambda conn: releases.append(conn),
        _register_vector_extension=lambda conn: None,
        _table_exists=lambda *args: True,
        delete=delete,
    )
    saved = history.document_history(store, "index", "kb", "doc")
    history.delete_document_history(store, "index", "kb", "doc")
    history.restore_document_history(store, "index", "kb", "doc", saved)
    assert history.document_history(store, "index", "kb", "doc") == [original]
    assert len(releases) == 6
