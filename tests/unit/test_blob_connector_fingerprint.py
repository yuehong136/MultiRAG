"""Exercise the production LIST/lazy GET path and its version/failure boundaries."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from io import BytesIO
from typing import Any

import pytest
import xxhash

from api.db.services.connector_service import connector_doc_id_candidates
from common.data_source.blob_connector import BlobStorageConnector, _normalize_etag
from common.data_source.models import Document, KeyRecord
from core.svr import sync_data_source


class Body(BytesIO):
    def iter_chunks(self, chunk_size: int = 65536) -> Iterator[bytes]:
        while chunk := self.read(chunk_size):
            yield chunk


class Client:
    def __init__(self, pages: list[dict[str, Any] | Exception], responses: dict[str, dict[str, Any] | Exception]) -> None:
        self.pages = pages
        self.responses = responses
        self.calls: list[str] = []

    def get_paginator(self, name: str) -> "Client":
        assert name == "list_objects_v2"
        return self

    def paginate(self, **kwargs: Any) -> Iterator[dict[str, Any]]:
        assert kwargs == {"Bucket": "bucket", "Prefix": "docs/"}
        for page in self.pages:
            if isinstance(page, Exception):
                raise page
            yield page

    def get_object(self, Bucket: str, Key: str) -> dict[str, Any]:
        assert Bucket == "bucket"
        self.calls.append(Key)
        response = self.responses[Key]
        if isinstance(response, Exception):
            raise response
        return response


def _object(key: str, etag: str | None = '"old"') -> dict[str, Any]:
    return {"Key": key, "ETag": etag, "Size": 4, "LastModified": datetime.now(UTC) - timedelta(days=90)}


def _connector(client: Client) -> BlobStorageConnector:
    connector = BlobStorageConnector("s3", "bucket", prefix="docs", batch_size=1)
    connector.s3_client = client
    return connector


@pytest.mark.parametrize("etag", ['"single"', '"multipart-7"', "single", " single "])
def test_etag_normalizes_to_stable_32_hex_token(etag: str) -> None:
    expected = etag.strip().strip('"')
    assert _normalize_etag(etag) == xxhash.xxh128(expected.encode()).hexdigest()


@pytest.mark.parametrize("etag", [None, "", '""', " "])
def test_missing_etag_cannot_authorize_bypass(etag: str | None) -> None:
    assert _normalize_etag(etag) is None


def test_list_is_metadata_only_and_get_uses_response_version_closes_body() -> None:
    body = Body(b"new!")
    client = Client(
        [{"Contents": [_object("docs/a.txt"), _object("docs/folder/"), _object("docs/a.exe")]}, {"Contents": [_object("docs/sub/a.txt", '"multi-3"')]}], {"docs/a.txt": {"Body": body, "ETag": '"new"'}}
    )
    connector = _connector(client)
    records = list(connector.list_keys())
    assert len(records) == 2 and client.calls == []
    doc = connector.get_value(records[0].key)
    assert doc is not None and doc.id == records[0].key and doc.blob == b"new!"
    assert doc.semantic_identifier == "a.txt" and doc.fingerprint == _normalize_etag("new")
    assert doc.fingerprint != records[0].fingerprint and body.closed
    assert client.calls == ["docs/a.txt"]


def test_missing_get_etag_does_not_reuse_listing_fingerprint() -> None:
    connector = _connector(Client([{"Contents": [_object("docs/a.txt")]}], {"docs/a.txt": {"Body": Body(b"data")}}))
    [record] = list(connector.list_keys())
    doc = connector.get_value(record.key)
    assert doc is not None and doc.fingerprint is None


def test_failed_enumeration_cannot_publish_partial_cached_keys() -> None:
    connector = _connector(Client([{"Contents": [_object("docs/a.txt")]}, PermissionError("listing failed")], {}))
    with pytest.raises(PermissionError, match="listing failed"):
        list(connector.list_keys())
    with pytest.raises(KeyError):
        connector.get_value(f"{connector.bucket_type}:bucket:docs/a.txt")


def test_download_failure_propagates_and_size_exclusion_still_lists() -> None:
    client = Client([{"Contents": [_object("docs/a.txt")]}], {"docs/a.txt": PermissionError("fetch failed")})
    connector = _connector(client)
    [record] = list(connector.list_keys())
    with pytest.raises(PermissionError, match="fetch failed"):
        connector.get_value(record.key)
    connector.size_threshold = 3
    assert connector.get_value(record.key) is None
    assert len(list(connector.list_keys())) == 1
    assert client.calls == ["docs/a.txt"]


@pytest.mark.parametrize("historical_index", [0, 1, 2])
@pytest.mark.parametrize("reindex", ["0", "1"])
def test_production_bypass_resolves_kb_connector_namespace_and_forces_reindex(monkeypatch: pytest.MonkeyPatch, historical_index: int, reindex: str) -> None:
    key = "s3:bucket:docs/a.txt"
    identifier = connector_doc_id_candidates("kb", "connector", key)[historical_index]
    calls: list[str] = []

    @contextmanager
    def db_connection() -> Iterator[None]:
        yield None

    def versions(_db: Any, kb_id: str, source_type: str) -> dict[str, str]:
        assert (kb_id, source_type) == ("kb", "s3/connector")
        return {identifier: "version"}

    class Connector:
        batch_size = 1

        def get_value(self, source_key: str) -> Document:
            calls.append(source_key)
            return Document(id=source_key, source="s3", blob=b"data", semantic_identifier="a", extension=".txt", doc_updated_at=datetime.now(UTC), size_bytes=4, fingerprint="version")

    monkeypatch.setattr(sync_data_source, "db_connection", db_connection)
    monkeypatch.setattr(sync_data_source.DocumentService, "list_id_content_hash_map_by_kb_and_source_type", versions)
    driver = sync_data_source.S3({})
    driver.connector = Connector()
    result = list(driver._fingerprint_filtered_generator({"kb_id": "kb", "connector_id": "connector", "reindex": reindex}, [KeyRecord(key=key, fingerprint="version")]))
    assert calls == ([key] if reindex == "1" else [])
    assert len(result) == (1 if reindex == "1" else 0)


def test_listing_cannot_claim_empty_inventory_when_key_count_is_positive() -> None:
    from common.data_source.exceptions import ConnectorValidationError

    connector = _connector(Client([{"KeyCount": 1}], {}))
    with pytest.raises(ConnectorValidationError, match="without object metadata"):
        list(connector.list_keys())
