from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from dropbox.files import FileMetadata, FolderMetadata, ListFolderResult

from common.data_source.dropbox_connector import DropboxConnector
from common.data_source.interfaces import collect_slim_document_snapshot
from core.svr import sync_data_source as sync_module
from tests.unit.test_sync_deleted_snapshot_contract import sync_env as sync_env
from tests.unit.test_sync_deleted_snapshot_contract import task


def file(identifier: str, path: str, modified: datetime | None = None) -> FileMetadata:
    return FileMetadata(
        id=identifier,
        name=path.rsplit("/", 1)[-1],
        path_display=path,
        path_lower=path.lower(),
        client_modified=modified or datetime(2026, 2, 1),
        server_modified=datetime(2026, 2, 1),
        rev="0123456789",
        size=4,
    )


class Source:
    def __init__(self, failure: str = "") -> None:
        self.failure = failure
        self.downloads: list[str] = []
        self.closed: list[str] = []
        self.listings: list[str] = []
        self.root = file("id:root", "/same.txt")
        self.nested = file("id:nested", "/folder/same.txt")
        self.paged = file("id:paged", "/unique.txt", datetime(2025, 1, 1, tzinfo=UTC))

    def files_list_folder(self, path: str, **kwargs: Any) -> ListFolderResult:
        self.listings.append(path)
        assert kwargs == {"recursive": False, "include_non_downloadable_files": False, "include_deleted": False}
        if self.failure == "empty":
            return ListFolderResult(entries=[], cursor="done", has_more=False)
        if path == "":
            return ListFolderResult(entries=[self.root, FolderMetadata(name="folder", id="id:folder", path_lower="/folder")], cursor="root-next", has_more=True)
        if self.failure == "folder":
            raise PermissionError("folder denied")
        return ListFolderResult(entries=[self.nested], cursor="nested-done", has_more=False)

    def files_list_folder_continue(self, cursor: str) -> Any:
        assert cursor == "root-next"
        if self.failure == "page":
            raise PermissionError("later page denied")
        if self.failure == "malformed":
            return SimpleNamespace(entries=None, has_more=False)
        return ListFolderResult(entries=[self.paged], cursor="root-next" if self.failure == "loop" else "done", has_more=self.failure == "loop")

    def files_download(self, identity: str) -> tuple[None, Any]:
        self.downloads.append(identity)
        if self.failure == "body":
            raise PermissionError("download denied")
        return None, SimpleNamespace(content=b"body", close=lambda: self.closed.append(identity))


def connector(source: Source) -> DropboxConnector:
    result = DropboxConnector(batch_size=2)
    result.dropbox_client = source
    return result


def test_metadata_snapshot_has_all_nested_and_paginated_ids_without_body_downloads() -> None:
    source = Source()
    result = connector(source)
    assert [[doc.id for doc in batch] for batch in result.retrieve_all_slim_docs_perm_sync()] == [["dropbox:id:root", "dropbox:id:nested"], ["dropbox:id:paged"]]
    assert not source.downloads and source.listings == ["", "/folder"]
    documents = [doc for batch in result.load_from_state() for doc in batch]
    assert [doc.id for doc in documents] == ["dropbox:id:root", "dropbox:id:nested", "dropbox:id:paged"]
    assert [doc.semantic_identifier for doc in documents] == ["same.txt", "folder / same.txt", "unique.txt"]
    assert source.downloads == source.closed == ["id:root", "id:nested", "id:paged"]


def test_snapshot_ignores_content_time_window_while_full_reindex_reads_all() -> None:
    source = Source()
    result = connector(source)
    start, end = datetime(2026, 1, 1, tzinfo=UTC).timestamp(), datetime(2026, 3, 1, tzinfo=UTC).timestamp()
    assert len(collect_slim_document_snapshot(result)) == 3
    assert len([doc for batch in result.poll_source(start, end) for doc in batch]) == 2
    assert source.downloads == ["id:root", "id:nested"]
    assert len([doc for batch in result.load_from_state() for doc in batch]) == 3


@pytest.mark.parametrize("failure", ["folder", "page", "malformed", "loop"])
def test_incomplete_listing_aborts_without_downloading(failure: str) -> None:
    source = Source(failure)
    with pytest.raises((ValueError, PermissionError)):
        collect_slim_document_snapshot(connector(source))
    assert not source.downloads


def test_empty_scope_is_authoritative_and_body_failure_propagates() -> None:
    assert collect_slim_document_snapshot(connector(Source("empty"))) == ()
    with pytest.raises(PermissionError, match="download denied"):
        list(connector(Source("body")).load_from_state())


@pytest.mark.parametrize("mode", ["incremental", "disabled", "first", "reindex", "listing-failure", "body-failure", "ingest-failure"])
async def test_real_dropbox_driver_gates_deletion(sync_env: dict[str, Any], mode: str) -> None:
    monkeypatch = sync_env["monkeypatch"]
    source = Source({"listing-failure": "page", "body-failure": "body"}.get(mode, ""))
    result = connector(source)
    monkeypatch.setattr(result, "load_credentials", lambda credentials: None)
    monkeypatch.setattr(sync_module, "DropboxConnector", lambda **kwargs: result)
    monkeypatch.setattr(sync_module.SyncLogsService, "duplicate_and_parse", lambda db, kb, docs, *args: (["failed"] if mode == "ingest-failure" else [], [doc["id"] for doc in docs]))
    driver = sync_module.Dropbox({"sync_deleted_files": mode != "disabled", "credentials": {"dropbox_access_token": "synthetic"}})
    current = task()
    if mode == "first":
        current["poll_range_start"] = None
    elif mode == "reindex":
        current["reindex"] = "1"
    await driver(current)
    if mode.endswith("failure"):
        assert sync_env["calls"] == ["start", "fail"]
    elif mode == "incremental":
        assert sync_env["calls"][1][0] == "cleanup" and sync_env["calls"][-1] == "complete"
    else:
        assert sync_env["calls"] == ["start", "complete"]
    if mode in {"first", "reindex"}:
        assert source.downloads == ["id:root", "id:nested", "id:paged"]
