"""Missing native indexes may skip cleanup; failed probes never may."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from infinity.common import InfinityException
from infinity.errors import ErrorCode

from common.doc_store import document_history as history


def _store(backend: str, exists: bool = False) -> tuple[Any, Mock, Mock]:
    native = Mock()
    native.indices.exists.return_value = exists
    native.delete_by_query.return_value = {"deleted": 0, "timed_out": False, "failures": [], "version_conflicts": 0}
    native.delete.return_value = SimpleNamespace(delete_count=0)
    native.get_database.return_value.get_table.return_value.delete.return_value = SimpleNamespace(error_code=0, deleted_rows=0)
    native.indices.refresh.return_value = {"_shards": {"total": 1, "successful": 1, "failed": 0}}
    native.has_collection.return_value = exists
    native.get_database.return_value.get_table.side_effect = None if exists else InfinityException(ErrorCode.TABLE_NOT_EXIST, "missing table")
    pool = Mock()
    pool.get_conn.return_value = native
    store = SimpleNamespace(
        db_type=lambda: backend,
        es=native,
        os=native,
        _get_connection=lambda: native,
        connPool=pool,
        dbName="owned",
        index_exist=Mock(return_value=False),
        delete=Mock(return_value=0),
        equivalent_condition_to_str=lambda condition, table: "doc_id = 'doc'",
    )
    return store, native, pool


@pytest.mark.parametrize("backend", ["elasticsearch", "opensearch", "milvus", "infinity"])
def test_confirmed_missing_index_skips_delete_and_history(backend: str) -> None:
    store, native, pool = _store(backend)
    history.delete_document_history(store, "index", "kb", "doc")
    assert history.document_history(store, "index", "kb", "doc") == []
    store.delete.assert_not_called()
    store.index_exist.assert_not_called()
    if backend == "infinity":
        assert pool.release_conn.call_count == 2
        native.get_database.return_value.get_table.assert_called_with("index_kb")
    else:
        native.search.assert_not_called()


@pytest.mark.parametrize("backend", ["elasticsearch", "opensearch", "milvus", "infinity"])
@pytest.mark.parametrize("error", [ConnectionError("probe disconnected"), PermissionError("probe forbidden")])
def test_probe_failure_never_becomes_empty_history_or_success(backend: str, error: Exception) -> None:
    store, native, pool = _store(backend)
    native.indices.exists.side_effect = error
    native.has_collection.side_effect = error
    native.get_database.return_value.get_table.side_effect = error
    for operation in [history.delete_document_history, history.document_history]:
        with pytest.raises(type(error)) as caught:
            operation(store, "index", "kb", "doc")
        assert caught.value is error
    store.delete.assert_not_called()
    if backend == "infinity":
        assert pool.release_conn.call_count == 2


@pytest.mark.parametrize("backend", ["elasticsearch", "opensearch", "milvus", "infinity"])
def test_existing_index_uses_delete_even_when_legacy_probe_says_false(backend: str, monkeypatch: pytest.MonkeyPatch) -> None:
    store, native, _pool = _store(backend, exists=True)
    monkeypatch.setattr(history, "document_history", Mock(return_value=[]))
    history.delete_document_history(store, "index", "kb", "doc")
    store.delete.assert_not_called()
    if backend in {"elasticsearch", "opensearch"}:
        native.delete_by_query.assert_called_once()
    elif backend == "milvus":
        native.delete.assert_called_once()
    else:
        native.get_database.return_value.get_table.return_value.delete.assert_called_once()
    store.index_exist.assert_not_called()


def test_infinity_only_table_not_exist_counts_as_missing() -> None:
    store, native, pool = _store("infinity")
    rejected = InfinityException(ErrorCode.TXN_CONFLICT, "transaction rejected")
    native.get_database.return_value.get_table.side_effect = rejected
    with pytest.raises(InfinityException) as caught:
        history.delete_document_history(store, "index", "kb", "doc")
    assert caught.value is rejected
    store.delete.assert_not_called()
    pool.release_conn.assert_called_once_with(native)


@pytest.mark.parametrize("backend", ["elasticsearch", "opensearch", "milvus"])
@pytest.mark.parametrize("unknown", [None, 0, "false"])
def test_unknown_native_probe_response_fails_closed(backend: str, unknown: Any) -> None:
    store, native, _pool = _store(backend)
    native.indices.exists.return_value = unknown
    native.has_collection.return_value = unknown
    with pytest.raises(RuntimeError, match="existence could not be confirmed"):
        history.delete_document_history(store, "index", "kb", "doc")
    store.delete.assert_not_called()


@pytest.mark.parametrize("backend", ["elasticsearch", "opensearch", "milvus", "infinity"])
def test_native_delete_failure_cannot_be_acknowledged_as_zero(backend: str, monkeypatch: pytest.MonkeyPatch) -> None:
    import inspect
    import logging

    from core.utils.es_conn import ESConnection
    from core.utils.infinity_conn import InfinityConnection
    from core.utils.milvus_conn import MilvusConnection
    from core.utils.opensearch_conn import OSConnection

    constructors = {"elasticsearch": ESConnection, "opensearch": OSConnection, "milvus": MilvusConnection, "infinity": InfinityConnection}
    cls = inspect.getclosurevars(constructors[backend]).nonlocals["cls"]
    store = object.__new__(cls)
    _controlled, native, pool = _store(backend, exists=True)
    store.logger = logging.getLogger(__name__)
    store.es = store.os = native
    store.connPool, store.dbName = pool, "owned"
    monkeypatch.setattr(store, "equivalent_condition_to_str", lambda *args: "doc_id = 'doc'", raising=False)
    monkeypatch.setattr(store, "_get_connection", lambda: native, raising=False)
    error = ConnectionError("controlled native deletion failed")
    native.delete_by_query.side_effect = error
    native.delete.side_effect = error
    native.get_database.return_value.get_table.return_value.delete.side_effect = error
    monkeypatch.setattr(history, "document_history", Mock(return_value=[]))
    with pytest.raises(ConnectionError) as caught:
        history.delete_document_history(store, "index", "kb", "doc")
    assert caught.value is error


@pytest.mark.parametrize("backend", ["elasticsearch", "opensearch"])
@pytest.mark.parametrize("fault", ["timeout", "failure", "conflict", "missing_count", "bool_count"])
def test_delete_rejects_partial_or_untyped_native_acknowledgements(backend: str, fault: str, monkeypatch: pytest.MonkeyPatch) -> None:
    store, native, _pool = _store(backend, exists=True)
    response = {"deleted": 0, "timed_out": False, "failures": [], "version_conflicts": 0}
    if fault == "timeout":
        response["timed_out"] = True
    elif fault == "failure":
        response["failures"] = [{"cause": "shard unavailable"}]
    elif fault == "conflict":
        response["version_conflicts"] = 1
    elif fault == "missing_count":
        response.pop("deleted")
    else:
        response["deleted"] = False
    native.delete_by_query.return_value = response
    readback = Mock(return_value=[])
    monkeypatch.setattr(history, "document_history", readback)
    with pytest.raises(RuntimeError, match="deletion failed"):
        history.delete_document_history(store, "index", "kb", "doc")
    readback.assert_not_called()


def test_infinity_lookup_failure_between_probe_and_delete_is_not_missing() -> None:
    store, native, pool = _store("infinity", exists=True)
    table = native.get_database.return_value.get_table.return_value
    rejected = ConnectionError("table lookup reply lost")
    native.get_database.return_value.get_table.side_effect = [table, rejected]
    with pytest.raises(ConnectionError) as caught:
        history.delete_document_history(store, "index", "kb", "doc")
    assert caught.value is rejected
    assert pool.release_conn.call_count == 2
