"""Owned real ES/OS writes, immediate independent search, and metadata CRUD."""

import inspect
import json
import logging
import os
from collections.abc import Iterator
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from elasticsearch import Elasticsearch
from opensearchpy import OpenSearch

from core.utils.es_conn import ESConnection
from core.utils.opensearch_conn import OSConnection


@pytest.fixture(params=["elasticsearch", "opensearch"])
def search_store(request: pytest.FixtureRequest, record_property: Any) -> Iterator[dict[str, Any]]:
    backend = request.param
    es = backend == "elasticsearch"
    url = os.environ.get("METADATA_TEST_ES_URL" if es else "METADATA_TEST_OS_URL")
    assert url, "Prepare isolated ES/OS services before collection"
    indexes: list[str] = []
    manifest_path = os.environ.get("ES_OS_RESOURCE_MANIFEST")

    def record() -> None:
        if manifest_path:
            path = Path(manifest_path)
            data = json.loads(path.read_text()) if path.exists() else []
            data.append({"backend": backend, "indexes": list(indexes), "phase": "current"})
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(data, indent=2))
            temporary.replace(path)

    with ExitStack() as resources:
        writer = resources.enter_context(Elasticsearch(url, request_timeout=10) if es else OpenSearch(url, timeout=10))
        reader = resources.enter_context(Elasticsearch(url, request_timeout=10) if es else OpenSearch(url, timeout=10))
        version = reader.info()["version"]["number"]
        record_property("backend_version", f"{backend}:{version}")
        cls = inspect.getclosurevars(ESConnection if es else OSConnection).nonlocals["cls"]
        store = object.__new__(cls)
        store.es = writer if es else None
        store.os = writer if not es else None
        store.logger = logging.getLogger(__name__)

        def index_name() -> str:
            name = "multirag_test_after058_d_" + uuid4().hex
            indexes.append(name)
            record()  # register before creation, including failed creation
            return name

        try:
            yield {"store": store, "reader": reader, "writer": writer, "index_name": index_name, "backend": backend}
        finally:
            for index in indexes:
                if reader.indices.exists(index=index):
                    reader.indices.delete(index=index)
                assert not reader.indices.exists(index=index)
            record_property("removed_owned_indexes", indexes)
            indexes.clear()
            record()


def test_bulk_success_is_immediately_retrievable(search_store: dict[str, Any]) -> None:
    env = search_store
    index = env["index_name"]()
    env["writer"].indices.create(index=index, body={"settings": {"number_of_shards": 1, "number_of_replicas": 0, "refresh_interval": "500ms"}})
    for iteration in range(4):
        identifier = f"chunk-{iteration}"
        assert env["store"].insert([{"id": identifier, "doc_id": "doc", "kb_id": "kb", "content_with_weight": "ready now"}], index, "kb") == []
        result = env["reader"].search(index=index, body={"query": {"term": {"doc_id.keyword": "doc"}}})
        assert result["hits"]["total"]["value"] == iteration + 1
        assert identifier in {hit["_id"] for hit in result["hits"]["hits"]}


def test_rejected_bulk_is_not_success(search_store: dict[str, Any]) -> None:
    env = search_store
    index = env["index_name"]()
    env["writer"].indices.create(index=index, body={"settings": {"number_of_replicas": 0, "blocks.write": True}})
    assert env["store"].insert([{"id": "rejected", "doc_id": "doc", "kb_id": "kb"}], index, "kb")
    assert env["reader"].count(index=index)["count"] == 0
