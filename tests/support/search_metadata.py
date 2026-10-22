"""Owned real ES/OS connectors and exact index cleanup for storage/worker tests."""

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
        store.mapping = json.loads(Path("configs/es_mapping.json" if es else "configs/os_mapping.json").read_text())

        def register_index(name: str) -> str:
            assert not reader.indices.exists(index=name)
            indexes.append(name)
            record()
            return name

        def index_name(prefix: str = "multirag_test_after058_d_") -> str:
            name = prefix + uuid4().hex
            return register_index(name)  # register before creation, including failed creation

        try:
            yield {"store": store, "reader": reader, "writer": writer, "index_name": index_name, "backend": backend, "register_index": register_index}
        finally:
            for index in indexes:
                if reader.indices.exists(index=index):
                    reader.indices.delete(index=index)
                assert not reader.indices.exists(index=index)
            record_property("removed_owned_indexes", indexes)
            indexes.clear()
            record()
