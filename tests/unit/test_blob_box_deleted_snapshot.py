"""Source inventories must retain existing files and fail on incomplete listings."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from io import BytesIO
from types import SimpleNamespace
from typing import Any

import pytest

from common.data_source import blob_connector
from common.data_source.box_connector import BoxConnector
from common.data_source.exceptions import ConnectorMissingCredentialError, ConnectorValidationError


class _S3Pages:
    def __init__(self, pages: list[dict[str, Any] | Exception]) -> None:
        self.pages = pages
        self.calls: list[dict[str, Any]] = []

    def get_paginator(self, operation: str) -> "_S3Pages":
        assert operation == "list_objects_v2"
        return self

    def paginate(self, **kwargs: Any) -> Iterator[dict[str, Any]]:
        self.calls.append(kwargs)
        for page in self.pages:
            if isinstance(page, Exception):
                raise page
            yield page


def _blob_object(key: str, size: int = 4) -> dict[str, Any]:
    return {"Key": key, "Size": size, "LastModified": datetime.now(UTC) - timedelta(days=10)}


def test_blob_inventory_exhausts_pages_preserves_scope_and_existing_large_files(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _S3Pages(
        [
            {"Contents": [_blob_object("docs/a.txt"), _blob_object("docs/rejected.exe"), _blob_object("docs/folder/")]},
            {"Contents": [_blob_object("docs/image.png"), _blob_object("docs/b.txt", 100)]},
        ]
    )
    connector = blob_connector.BlobStorageConnector("s3", "bucket", prefix="docs", batch_size=1)
    connector.s3_client = client
    connector.size_threshold = 5
    downloads: list[str] = []

    def download(_client: Any, _bucket: str, key: str, _threshold: int | None) -> bytes:
        downloads.append(key)
        return b"data"

    monkeypatch.setattr(blob_connector, "download_object", download)
    docs = [doc for batch in connector.load_from_state() for doc in batch]
    inventory = [doc.id for batch in connector.retrieve_all_slim_docs_perm_sync() for doc in batch]

    assert inventory == [docs[0].id, f"{connector.bucket_type}:bucket:docs/b.txt"]
    assert downloads == ["docs/a.txt"]
    assert client.calls == [{"Bucket": "bucket", "Prefix": "docs/"}] * 2

    connector.set_allow_images(True)
    inventory = [doc.id for batch in connector.retrieve_all_slim_docs_perm_sync() for doc in batch]
    assert f"{connector.bucket_type}:bucket:docs/image.png" in inventory
    assert downloads == ["docs/a.txt"]


def test_blob_inventory_does_not_treat_download_failure_as_deletion(monkeypatch: pytest.MonkeyPatch) -> None:
    connector = blob_connector.BlobStorageConnector("s3", "bucket")
    connector.s3_client = _S3Pages([{"Contents": [_blob_object("a.txt")]}])

    def denied(*_args: Any) -> bytes:
        raise PermissionError("download denied")

    monkeypatch.setattr(blob_connector, "download_object", denied)
    assert list(connector.load_from_state()) == []
    assert [doc.id for batch in connector.retrieve_all_slim_docs_perm_sync() for doc in batch] == [f"{connector.bucket_type}:bucket:a.txt"]


@pytest.mark.parametrize("error", [PermissionError("denied"), RuntimeError("page failed")])
def test_blob_inventory_later_page_error_aborts_snapshot(error: Exception) -> None:
    connector = blob_connector.BlobStorageConnector("s3", "bucket", batch_size=1)
    connector.s3_client = _S3Pages([{"Contents": [_blob_object("a.txt")]}, error])
    snapshot = connector.retrieve_all_slim_docs_perm_sync()
    with pytest.raises(type(error), match=str(error)):
        next(snapshot)


def test_blob_inventory_rejects_truncated_page_without_token() -> None:
    connector = blob_connector.BlobStorageConnector("s3", "bucket")
    connector.s3_client = _S3Pages([{"IsTruncated": True, "Contents": [_blob_object("a.txt")]}])
    with pytest.raises(ConnectorValidationError, match="continuation token"):
        list(connector.retrieve_all_slim_docs_perm_sync())


def test_blob_empty_inventory_is_valid_and_credentials_are_required() -> None:
    connector = blob_connector.BlobStorageConnector("s3", "bucket")
    with pytest.raises(ConnectorMissingCredentialError):
        list(connector.retrieve_all_slim_docs_perm_sync())
    connector.s3_client = _S3Pages([{}])
    assert list(connector.retrieve_all_slim_docs_perm_sync()) == []


class _BoxFolders:
    def __init__(self, pages: dict[tuple[str, str | int | None], Any]) -> None:
        self.pages = pages
        self.calls: list[dict[str, Any]] = []

    def get_folder_items(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        page = self.pages[(kwargs["folder_id"], kwargs.get("marker", kwargs.get("offset")))]
        if isinstance(page, Exception):
            raise page
        return page


def _box_file(file_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        type="file",
        id=file_id,
        name=f"{file_id}.txt",
        created_at=datetime.now(UTC) - timedelta(days=10),
        size=4,
        metadata={},
    )


def _box_client(folders: _BoxFolders, files: dict[str, Any]) -> Any:
    def get_file(file_id: str) -> Any:
        file = files[file_id]
        if isinstance(file, Exception):
            raise file
        return file

    return SimpleNamespace(
        folders=folders,
        files=SimpleNamespace(get_file_by_id=get_file),
        downloads=SimpleNamespace(download_file=lambda _id: BytesIO(b"data")),
    )


def test_box_inventory_exhausts_recursive_marker_pages_and_matches_ingestion_ids() -> None:
    files = {key: _box_file(key) for key in ["a", "b", "c"]}
    child = SimpleNamespace(type="folder", id="child", name="nested")
    folders = _BoxFolders(
        {
            ("root", None): SimpleNamespace(entries=[files["a"], child], next_marker="second"),
            ("child", None): SimpleNamespace(entries=[files["b"]], next_marker=None),
            ("root", "second"): SimpleNamespace(entries=[files["c"]], next_marker=None),
        }
    )
    connector = BoxConnector("root", batch_size=1)
    connector.box_client = _box_client(folders, files)
    documents = [doc for batch in connector.load_from_state() for doc in batch]
    connector.box_client.downloads.download_file = lambda _id: pytest.fail("inventory must not download")
    snapshot = [doc.id for batch in connector.retrieve_all_slim_docs_perm_sync() for doc in batch]
    assert snapshot == [doc.id for doc in documents] == ["box:a", "box:b", "box:c"]
    assert documents[1].semantic_identifier == "nested / b.txt"
    assert {call["folder_id"] for call in folders.calls} == {"root", "child"}


def test_box_inventory_offset_pages_use_server_limit_even_when_entries_have_gaps() -> None:
    files = {key: _box_file(key) for key in ["a", "c"]}
    folders = _BoxFolders(
        {
            ("root", None): SimpleNamespace(entries=[files["a"]], limit=2, offset=0, total_count=5),
            ("root", 2): SimpleNamespace(entries=[], limit=2, offset=2, total_count=5),
            ("root", 4): SimpleNamespace(entries=[files["c"]], limit=2, offset=4, total_count=5),
        }
    )
    connector = BoxConnector("root", batch_size=100, use_marker=False)
    connector.box_client = _box_client(folders, files)
    assert [doc.id for batch in connector.retrieve_all_slim_docs_perm_sync() for doc in batch] == ["box:a", "box:c"]
    assert [call.get("offset", 0) for call in folders.calls] == [0, 2, 4]


@pytest.mark.parametrize("failure_scope", ["page", "child", "file"])
def test_box_inventory_permission_failures_propagate(failure_scope: str) -> None:
    file = _box_file("a")
    entries = [file]
    if failure_scope == "child":
        entries.append(SimpleNamespace(type="folder", id="child", name="nested"))
    folders = _BoxFolders(
        {
            ("root", None): SimpleNamespace(entries=entries, next_marker="second" if failure_scope == "page" else None),
            ("root", "second"): PermissionError("page denied"),
            ("child", None): PermissionError("child denied"),
        }
    )
    connector = BoxConnector("root", batch_size=1)
    connector.box_client = _box_client(folders, {"a": PermissionError("file denied") if failure_scope == "file" else file})
    with pytest.raises(PermissionError, match="denied"):
        list(connector.retrieve_all_slim_docs_perm_sync())


def test_box_inventory_rejects_repeated_markers() -> None:
    folders = _BoxFolders(
        {
            ("root", None): SimpleNamespace(entries=[], next_marker="same"),
            ("root", "same"): SimpleNamespace(entries=[], next_marker="same"),
        }
    )
    connector = BoxConnector("root")
    connector.box_client = _box_client(folders, {})
    with pytest.raises(ConnectorValidationError, match="repeated"):
        list(connector.retrieve_all_slim_docs_perm_sync())


def test_box_inventory_rejects_missing_offset_metadata() -> None:
    connector = BoxConnector("root", use_marker=False)
    connector.box_client = _box_client(_BoxFolders({("root", None): SimpleNamespace(entries=[], total_count=None, limit=100, offset=0)}), {})
    with pytest.raises(ConnectorValidationError, match="offset pagination"):
        list(connector.retrieve_all_slim_docs_perm_sync())


@pytest.mark.parametrize("use_marker", [True, False])
def test_box_empty_inventory_is_valid_and_credentials_are_required(use_marker: bool) -> None:
    connector = BoxConnector("root", use_marker=use_marker)
    with pytest.raises(ConnectorMissingCredentialError):
        list(connector.retrieve_all_slim_docs_perm_sync())
    page = SimpleNamespace(entries=[], next_marker=None, limit=100, offset=0, total_count=0)
    connector.box_client = _box_client(_BoxFolders({("root", None): page}), {})
    assert list(connector.retrieve_all_slim_docs_perm_sync()) == []
