from datetime import UTC, datetime, timedelta
from typing import Any

from common.data_source import blob_connector
from core.svr import sync_data_source


class _Paginator:
    def __init__(self, objects: list[dict[str, Any]]) -> None:
        self.objects = objects

    def paginate(self, **_kwargs):
        return [{"Contents": self.objects}]


class _S3Client:
    def __init__(self, objects: list[dict[str, Any]]) -> None:
        self.objects = objects

    def get_paginator(self, name: str) -> _Paginator:
        assert name == "list_objects_v2"
        return _Paginator(self.objects)


def _objects(now: datetime) -> list[dict[str, Any]]:
    return [
        {"Key": "docs/accepted.txt", "LastModified": now, "Size": 4},
        {"Key": "docs/rejected.exe", "LastModified": now, "Size": 4},
        {"Key": "docs/image.png", "LastModified": now, "Size": 4},
    ]


def test_blob_connector_filters_extensions_before_download(monkeypatch) -> None:
    now = datetime.now(UTC)
    downloads: list[str] = []

    def fake_download(_client, _bucket: str, key: str, _threshold: int | None) -> bytes:
        downloads.append(key)
        return b"data"

    monkeypatch.setattr(blob_connector, "download_object", fake_download)
    connector = blob_connector.BlobStorageConnector("s3", "bucket")
    connector.s3_client = _S3Client(_objects(now))

    batches = list(connector._yield_blob_objects(now - timedelta(seconds=1), now + timedelta(seconds=1)))

    assert [document.semantic_identifier for batch in batches for document in batch] == ["accepted.txt"]
    assert downloads == ["docs/accepted.txt"]


def test_blob_connector_allows_images_when_enabled(monkeypatch) -> None:
    now = datetime.now(UTC)
    downloads: list[str] = []

    def fake_download(_client, _bucket: str, key: str, _threshold: int | None) -> bytes:
        downloads.append(key)
        return b"data"

    monkeypatch.setattr(blob_connector, "download_object", fake_download)
    connector = blob_connector.BlobStorageConnector("s3", "bucket")
    connector.set_allow_images(True)
    connector.s3_client = _S3Client(_objects(now))

    list(connector._yield_blob_objects(now - timedelta(seconds=1), now + timedelta(seconds=1)))

    assert downloads == ["docs/accepted.txt", "docs/image.png"]


async def test_blob_sync_wires_allow_images(monkeypatch) -> None:
    calls: list[bool] = []

    class FakeConnector:
        def __init__(self, **_kwargs) -> None:
            pass

        def set_allow_images(self, value: bool) -> None:
            calls.append(value)

        def load_credentials(self, _credentials: dict[str, Any]) -> None:
            pass

        def load_from_state(self):
            return iter(())

    monkeypatch.setattr(sync_data_source, "BlobStorageConnector", FakeConnector)
    sync = sync_data_source.S3(
        {
            "bucket_name": "bucket",
            "credentials": {},
            "allow_images": True,
        }
    )

    await sync._generate({"reindex": "1", "poll_range_start": None})

    assert calls == [True]
