"""Engine metadata whole-map replacement through the real ES/OS connectors."""

import inspect
import logging
from typing import Any
from unittest.mock import Mock

import pytest

from api.db.services.metadata_store_engine import EngineMetadataStore
from common import settings
from core.utils.es_conn import ESConnection
from core.utils.opensearch_conn import OSConnection


@pytest.fixture(params=[ESConnection, OSConnection])
def es_store(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> tuple[EngineMetadataStore, Any, Mock]:
    cls = inspect.getclosurevars(request.param).nonlocals["cls"]
    connector = object.__new__(cls)
    client = Mock()
    connector.es = connector.os = client
    connector.logger = logging.getLogger(__name__)
    client.get.return_value = {"_source": {"id": "d1", "kb_id": "kb1"}}
    client.update.return_value = {"result": "updated", "_shards": {"failed": 0}}
    client.bulk.return_value = {"errors": False}
    client.indices.refresh.return_value = {"_shards": {"total": 1, "successful": 1, "failed": 0}}
    monkeypatch.setattr(settings, "DOC_ENGINE_INFINITY", False)
    monkeypatch.setattr(settings, "DOC_ENGINE_OCEANBASE", False)
    monkeypatch.setattr(settings, "docStoreConn", connector)
    return EngineMetadataStore(), connector, client


@pytest.mark.parametrize("metadata", [{"author": "ada"}, {}])
def test_upsert_replaces_whole_map(db: Any, es_store: tuple[EngineMetadataStore, Any, Mock], metadata: dict[str, Any]) -> None:
    store, _, client = es_store
    assert store.upsert(db, "d1", "t1", "kb1", metadata) is True
    call = client.update.call_args.kwargs
    assert call["index"] == "multirag_doc_meta_t1" and call["id"] == "d1"
    assert call["refresh"] is True
    assert call["body"] == {"script": {"source": "ctx._source.meta_fields = params.meta_fields", "params": {"meta_fields": metadata}}}
    client.bulk.assert_not_called()


def test_upsert_inserts_absent_document(db: Any, es_store: tuple[EngineMetadataStore, Any, Mock], monkeypatch: pytest.MonkeyPatch) -> None:
    store, connector, client = es_store
    monkeypatch.setattr(connector, "get", lambda *args: None)
    assert store.upsert(db, "d1", "t1", "kb1", {"author": "ada"}) is True
    client.update.assert_not_called()
    call = client.bulk.call_args.kwargs
    assert call["refresh"] == "wait_for"
    assert call.get("operations", call.get("body"))[1] == {"id": "d1", "kb_id": "kb1", "meta_fields": {"author": "ada"}}


@pytest.mark.parametrize("fault", ["replace", "get", "scope"])
def test_upsert_failure_never_falls_back_to_destructive_write(db: Any, es_store: tuple[EngineMetadataStore, Any, Mock], fault: str) -> None:
    store, _, client = es_store
    if fault == "replace":
        client.update.side_effect = RuntimeError("script rejected")
    elif fault == "get":
        client.get.side_effect = RuntimeError("read failed")
    else:
        client.get.return_value = {"_source": {"id": "d1", "kb_id": "foreign"}}
    assert store.upsert(db, "d1", "t1", "kb1", {"author": "ada"}) is False
    client.bulk.assert_not_called()
    client.delete_by_query.assert_not_called()


def test_insert_uses_bulk_visibility_without_another_refresh(db: Any, es_store: tuple[EngineMetadataStore, Any, Mock], monkeypatch: pytest.MonkeyPatch) -> None:
    store, connector, client = es_store
    monkeypatch.setattr(connector, "get", lambda *args: None)
    client.indices.refresh.side_effect = RuntimeError("refresh failed")
    assert store.upsert(db, "d1", "t1", "kb1", {"author": "ada"}) is True
    client.indices.refresh.assert_not_called()
