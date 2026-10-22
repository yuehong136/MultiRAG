"""Failure contracts for backend-neutral metadata dispatch."""

import inspect
import logging
from typing import Any
from unittest.mock import Mock

import pytest
from elastic_transport import ConnectionTimeout as ESTimeout
from elasticsearch import NotFoundError as ESNotFound
from elasticsearch import TransportError
from opensearchpy import ConnectionTimeout as OSTimeout
from opensearchpy import NotFoundError as OSNotFound

from api.db.services.doc_metadata_service import DocMetadataService
from api.db.services.metadata_store_engine import EngineMetadataStore
from common import settings
from core.utils.es_conn import ESConnection
from core.utils.opensearch_conn import OSConnection


@pytest.fixture(params=[ESConnection, OSConnection])
def connector(request: pytest.FixtureRequest) -> tuple[Any, Mock]:
    cls = inspect.getclosurevars(request.param).nonlocals["cls"]
    store = object.__new__(cls)
    store.logger = logging.getLogger(__name__)
    client = Mock()
    store.es = store.os = client
    return store, client


@pytest.mark.parametrize("count", [0, 2, -1, None, False, "0", float("nan")])
def test_count_accepts_only_nonnegative_integers(connector: tuple[Any, Mock], count: Any) -> None:
    store, client = connector
    client.count.return_value = {"count": count}
    assert store.count_idx("index") == (count if type(count) is int and count >= 0 else -1)


def test_count_failure_is_unknown_and_notfound_is_empty(connector: tuple[Any, Mock]) -> None:
    store, client = connector
    client.count.side_effect = RuntimeError("count rejected")
    assert store.count_idx("index") == -1
    client.count.side_effect = OSNotFound(404, "missing", {}) if store.db_type() == "opensearch" else ESNotFound("missing", meta=Mock(), body={})
    assert store.count_idx("index") == 0
    client.count.side_effect = None
    client.count.return_value = {"count": 0, "_shards": {"failed": 1}}
    assert store.count_idx("index") == -1


@pytest.mark.parametrize("shards", [None, {}, {"total": 1, "successful": 0, "failed": 1}, {"total": 2, "successful": 1, "failed": 0}])
def test_refresh_requires_active_shard_acknowledgement(connector: tuple[Any, Mock], shards: Any) -> None:
    store, client = connector
    client.indices.refresh.return_value = {"_shards": shards} if shards is not None else {}
    assert store.refresh_idx("index") is (shards == {"total": 2, "successful": 1, "failed": 0})


@pytest.mark.parametrize("fault", ["transport", "permanent", "notfound"])
def test_replacement_retry_is_bounded_to_transient_errors(connector: tuple[Any, Mock], fault: str) -> None:
    store, client = connector
    if fault == "transport":
        client.update.side_effect = OSTimeout("TIMEOUT", "controlled", None) if store.db_type() == "opensearch" else ESTimeout("controlled")
    elif fault == "notfound":
        client.update.side_effect = OSNotFound(404, "missing", {}) if store.db_type() == "opensearch" else ESNotFound("missing", meta=Mock(), body={})
    else:
        client.update.side_effect = TransportError("script rejected")
    assert store.replace_meta_fields("index", "doc", {}) is False
    assert client.update.call_count == (2 if fault == "transport" else 1)


@pytest.mark.parametrize("count", [-1, None, False, "0", 1, 0])
def test_unknown_count_never_drops_populated_index(monkeypatch: pytest.MonkeyPatch, count: Any) -> None:
    store = Mock()
    store.count_idx.return_value = count
    monkeypatch.setattr(settings, "docStoreConn", store)
    EngineMetadataStore._drop_if_empty("owned")
    assert store.delete_idx.call_count == (1 if type(count) is int and count == 0 else 0)
    store.search.assert_not_called()


def test_raising_count_never_drops_index(monkeypatch: pytest.MonkeyPatch) -> None:
    store = Mock()
    store.count_idx.side_effect = RuntimeError("count unavailable")
    monkeypatch.setattr(settings, "docStoreConn", store)
    EngineMetadataStore._drop_if_empty("owned")
    store.delete_idx.assert_not_called()


def test_index_creation_failure_is_false(connector: tuple[Any, Mock]) -> None:
    store, client = connector
    client.indices.exists.return_value = False
    client.indices.create.side_effect = RuntimeError("mapping rejected")
    assert store.create_doc_meta_idx("owned") is False


def test_batch_metadata_read_rejects_foreign_scope(connector: tuple[Any, Mock]) -> None:
    store, client = connector
    client.mget.return_value = {"docs": [{"_id": "doc", "found": True, "_source": {"kb_id": "foreign", "meta_fields": {"a": 1}}}]}
    with pytest.raises(RuntimeError, match="scope"):
        store.get_doc_metadata_rows("owned", ["doc"], "kb")


@pytest.mark.parametrize("mode", ["insert", "replace", "delete"])
def test_batch_does_not_count_rejected_storage_writes(monkeypatch: pytest.MonkeyPatch, db: Any, mode: str) -> None:
    store = Mock()
    store.list_by_doc_ids.return_value = {} if mode == "insert" else {"doc": {"old": "value"}}
    store.upsert.return_value = store.delete.return_value = False
    monkeypatch.setattr(DocMetadataService, "_store", staticmethod(lambda: store))
    monkeypatch.setattr(DocMetadataService, "_kb_tenant", lambda *args: "tenant")
    with pytest.raises(RuntimeError, match="Metadata storage rejected"):
        DocMetadataService.batch_update_metadata(db, "kb", ["doc"], [] if mode == "delete" else [{"key": "new", "value": "value"}], [{"key": "old"}] if mode == "delete" else [])
