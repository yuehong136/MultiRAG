"""Bulk completion is a search visibility barrier and cannot mask failures."""

import inspect
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from elastic_transport import ConnectionTimeout as ESTimeout
from opensearchpy import ConnectionTimeout as OSTimeout

from core.utils import es_conn, opensearch_conn


@pytest.fixture(params=["elasticsearch", "opensearch"])
def connector(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Mock, type[Exception]]:
    es = request.param == "elasticsearch"
    module = es_conn if es else opensearch_conn
    constructor = module.ESConnection if es else module.OSConnection
    cls = inspect.getclosurevars(constructor).nonlocals["cls"]
    store = object.__new__(cls)
    client = Mock()
    store.es = store.os = client
    store.logger = logging.getLogger(__name__)
    store._connect = Mock()
    monkeypatch.setattr(module, "time", SimpleNamespace(sleep=lambda _: None))
    return store, client, ESTimeout if es else OSTimeout


def test_insert_returns_only_after_bulk_visibility_barrier(connector: tuple[Any, Mock, type[Exception]]) -> None:
    store, client, _ = connector
    visible = []

    def bulk(**kwargs: Any) -> dict[str, Any]:
        assert kwargs["refresh"] == "wait_for"
        visible.extend(kwargs.get("operations", kwargs.get("body"))[1::2])
        return {"errors": False}

    client.bulk.side_effect = bulk
    row = {"id": "chunk", "doc_id": "doc", "kb_id": "kb", "content_with_weight": "ready"}
    assert store.insert([row], "index", "kb") == []
    assert visible[0]["content_with_weight"] == row["content_with_weight"]
    client.indices.refresh.assert_not_called()
    assert row["id"] == "chunk"


@pytest.mark.parametrize("failure", ["timeout", "refresh_rejected", "item", "malformed"])
def test_bulk_failure_never_returns_success(connector: tuple[Any, Mock, type[Exception]], failure: str) -> None:
    store, client, timeout = connector
    if failure == "timeout":
        client.bulk.side_effect = timeout("TIMEOUT", "controlled timeout", None) if timeout is OSTimeout else timeout("controlled timeout")
    elif failure == "refresh_rejected":
        client.bulk.side_effect = RuntimeError("refresh failed")
    elif failure == "item":
        client.bulk.return_value = {"errors": True, "items": [{"index": {"_id": "chunk", "error": "refresh failed"}}]}
    else:
        client.bulk.return_value = {"errors": True, "items": []}
    assert store.insert([{"id": "chunk", "doc_id": "doc"}], "index", "kb")
    assert client.bulk.call_count == (2 if failure == "timeout" else 1)
