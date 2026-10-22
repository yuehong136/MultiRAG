"""Failure contracts for backend-neutral metadata dispatch."""

import inspect
import logging
from types import SimpleNamespace
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
    client.count.return_value = {"count": count, "_shards": {"total": 2, "successful": 2, "failed": 0}}
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


@pytest.mark.parametrize(
    "shards",
    [
        None,
        {},
        {"failed": 0},
        {"total": 1, "successful": 1},
        {"total": True, "successful": 1, "failed": 0},
        {"total": 1, "successful": False, "failed": 0},
        {"total": 1, "successful": 1, "failed": False},
        {"total": 1, "successful": 1, "failed": 1},
        {"total": 0, "successful": 0, "failed": 0},
    ],
)
def test_malformed_count_and_update_never_authorize_success(connector: tuple[Any, Mock], monkeypatch: pytest.MonkeyPatch, shards: Any) -> None:
    store, client = connector
    client.count.return_value = {"count": 0, **({"_shards": shards} if shards is not None else {})}
    client.update.return_value = {"result": "updated", **({"_shards": shards} if shards is not None else {})}
    deleted = Mock()
    monkeypatch.setattr(store, "delete_idx", deleted)
    monkeypatch.setattr(settings, "docStoreConn", store)
    EngineMetadataStore._drop_if_empty("populated")
    deleted.assert_not_called()
    assert store.count_idx("populated") == -1
    assert store.replace_meta_fields("populated", "doc", {}) is False


@pytest.mark.parametrize("shards", [{"total": 0, "successful": 0, "failed": 0}, {"total": 2, "successful": 1, "failed": 0}])
def test_explicit_noop_with_complete_shards_is_success(connector: tuple[Any, Mock], shards: dict[str, int]) -> None:
    store, client = connector
    client.update.return_value = {"result": "noop", "_shards": shards}
    assert store.replace_meta_fields("index", "doc", {}) is True


@pytest.mark.parametrize("fault", ["omitted", "duplicate", "duplicate_same_size", "missing_found", "falsey_found", "foreign_absent", "missing_source", "doc_error"])
def test_incomplete_mget_fails_before_any_batch_mutation(connector: tuple[Any, Mock], monkeypatch: pytest.MonkeyPatch, db: Any, fault: str) -> None:
    store, client = connector
    hit: dict[str, Any] = {"_id": "doc", "found": True, "_source": {"kb_id": "kb", "meta_fields": {"old": "preserve", "number": 0}}}
    hits = [hit]
    requested_ids = ["doc"]
    if fault == "omitted":
        hits = []
    elif fault in {"duplicate", "duplicate_same_size"}:
        hits = [hit, hit]
        if fault == "duplicate_same_size":
            requested_ids.append("omitted")
    elif fault == "missing_found":
        hit.pop("found")
    elif fault == "falsey_found":
        hit["found"] = 0
    elif fault == "foreign_absent":
        hit.update(_id="foreign", found=False)
    elif fault == "missing_source":
        hit.pop("_source")
    else:
        hit["error"] = {"reason": "failed shard"}
    client.mget.return_value = {"docs": hits}
    engine = EngineMetadataStore()
    monkeypatch.setattr(engine, "list_by_doc_ids", lambda db, ids: {row["id"]: row["meta_fields"] for row in store.get_doc_metadata_rows("index", ids, "kb")})
    mutate = Mock()
    monkeypatch.setattr(engine, "upsert", mutate)
    monkeypatch.setattr(engine, "delete", mutate)
    monkeypatch.setattr(DocMetadataService, "_store", staticmethod(lambda: engine))
    monkeypatch.setattr(DocMetadataService, "_kb_tenant", lambda *args: "tenant")
    with pytest.raises(RuntimeError):
        DocMetadataService.batch_update_metadata(db, "kb", requested_ids, [{"key": "new", "value": "value"}], [])
    mutate.assert_not_called()


def test_mget_accounts_for_found_and_explicit_absence_once(connector: tuple[Any, Mock]) -> None:
    store, client = connector
    client.mget.return_value = {"docs": [{"_id": "present", "found": True, "_source": {"kb_id": "kb", "meta_fields": {}}}, {"_id": "absent", "found": False}]}
    assert store.get_doc_metadata_rows("index", ["present", "absent", "present"], "kb") == [{"id": "present", "kb_id": "kb", "meta_fields": {}}]
    assert client.mget.call_args.kwargs["body"] == {"ids": ["present", "absent"]}


@pytest.mark.parametrize("deleted", [0, 1])
def test_metadata_delete_requires_ack_and_confirmed_scoped_absence(connector: tuple[Any, Mock], deleted: int) -> None:
    store, client = connector
    client.delete_by_query.return_value = {"deleted": deleted, "total": deleted, "version_conflicts": 0, "timed_out": False, "failures": []}
    client.search.return_value = {"timed_out": False, "_shards": {"total": 1, "successful": 1, "failed": 0}, "hits": {"total": {"value": 0, "relation": "eq"}, "hits": []}}
    assert store.delete_doc_metadata("index", "doc", "kb") is True
    assert client.delete_by_query.call_args.kwargs["refresh"] is True
    assert client.search.call_args.kwargs["body"]["query"] == client.delete_by_query.call_args.kwargs["body"]["query"]


@pytest.mark.parametrize("fault", ["exception", "malformed_ack", "boolean_count", "still_present", "partial_search"])
def test_metadata_delete_failure_cannot_become_success(connector: tuple[Any, Mock], fault: str) -> None:
    store, client = connector
    client.delete_by_query.return_value = {"deleted": 0, "total": 0, "version_conflicts": 0, "timed_out": False, "failures": []}
    client.search.return_value = {"timed_out": False, "_shards": {"total": 1, "successful": 1, "failed": 0}, "hits": {"total": {"value": 0, "relation": "eq"}, "hits": []}}
    if fault == "exception":
        client.delete_by_query.side_effect = RuntimeError("write blocked")
    elif fault == "malformed_ack":
        client.delete_by_query.return_value = {"deleted": 0}
    elif fault == "boolean_count":
        client.delete_by_query.return_value["deleted"] = False
    elif fault == "still_present":
        client.search.return_value["hits"]["total"]["value"] = 1
    else:
        client.search.return_value["_shards"]["failed"] = 1
    assert store.delete_doc_metadata("index", "doc", "kb") is False


def test_engine_delete_dispatches_rejection_instead_of_ambiguous_generic_zero(monkeypatch: pytest.MonkeyPatch, db: Any) -> None:
    store = SimpleNamespace(delete=Mock(return_value=0), delete_doc_metadata=Mock(return_value=False))
    monkeypatch.setattr(settings, "docStoreConn", store)
    monkeypatch.setattr(settings, "DOC_ENGINE_INFINITY", False)
    monkeypatch.setattr(settings, "DOC_ENGINE_OCEANBASE", False)
    assert EngineMetadataStore().delete(db, "doc", "tenant", "kb") is False
    store.delete.assert_not_called()


@pytest.mark.parametrize("existence", [False, True, None, 0, "", {}])
def test_missing_index_delete_needs_confirmed_boolean_absence(connector: tuple[Any, Mock], existence: Any) -> None:
    store, client = connector
    client.delete_by_query.side_effect = OSNotFound(404, "missing", {}) if store.db_type() == "opensearch" else ESNotFound("missing", meta=Mock(), body={})
    client.indices.exists.return_value = existence
    assert store.delete_doc_metadata("index", "doc", "kb") is (existence is False)
