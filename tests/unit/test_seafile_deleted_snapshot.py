from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from common.data_source.interfaces import collect_slim_document_snapshot
from common.data_source.seafile_connector import SeaFileConnector
from core.svr import sync_data_source as sync_module
from tests.unit.test_sync_deleted_snapshot_contract import sync_env as sync_env
from tests.unit.test_sync_deleted_snapshot_contract import task


def response(data: Any) -> Any:
    return SimpleNamespace(json=lambda: data, raise_for_status=lambda: None, text='"https://download.test/file"', content=b"body")


def connector(monkeypatch: pytest.MonkeyPatch, scope: str = "account", repo_token: bool = False) -> tuple[SeaFileConnector, list[str]]:
    result = SeaFileConnector("https://seafile.test", sync_scope=scope, repo_id="repo" if scope != "account" else None, sync_path="/folder" if scope == "directory" else None, batch_size=1)
    result.token = "synthetic"
    result.repo_token = "synthetic" if repo_token else None
    paths: list[str] = []

    def get(endpoint: str, params: Any = None) -> Any:
        if endpoint == "/repos/":
            return response([{"id": "repo", "name": "Library"}])
        if endpoint == "/repos/repo/" or endpoint == "repo-info/":
            return response({"id": "repo", "repo_id": "repo", "name": "Library"})
        if "dir/" in endpoint:
            path = params.get("p", params.get("path"))
            paths.append(path)
            entries = [{"type": "file", "name": "same.txt", "id": "nested" if path != "/" else "root", "mtime": 1735689600, "size": 4}]
            if path == "/":
                entries.append({"type": "dir", "name": "folder"})
            return response({"dirent_list": entries} if repo_token else entries)
        return response(None)

    monkeypatch.setattr(result, "_account_get", get)
    monkeypatch.setattr(result, "_repo_token_get", get)
    monkeypatch.setattr("common.data_source.seafile_connector.rl_requests.get", lambda *a, **kw: response(None))
    return result, paths


@pytest.mark.parametrize("scope,repo_token", [("account", False), ("library", False), ("library", True), ("directory", False), ("directory", True)])
def test_scopes_and_ids_match_body_without_download_or_time_window(monkeypatch: pytest.MonkeyPatch, scope: str, repo_token: bool) -> None:
    result, paths = connector(monkeypatch, scope, repo_token)
    slim = [doc.id for doc in collect_slim_document_snapshot(result)]
    assert paths == (["/folder"] if scope == "directory" else ["/", "/folder"])
    body = [doc.id for batch in result.load_from_state() for doc in batch]
    assert slim == body == (["seafile:repo:nested"] if scope == "directory" else ["seafile:repo:root", "seafile:repo:nested"])
    assert list(result.poll_source(datetime(2026, 1, 1, tzinfo=UTC).timestamp(), datetime(2026, 2, 1, tzinfo=UTC).timestamp())) == []


@pytest.mark.parametrize("payload", [None, {}, {"dirent_list": None}, [{"type": "file", "name": "x"}], [{"type": "dir", "name": ".."}]])
def test_invalid_directory_is_not_empty(monkeypatch: pytest.MonkeyPatch, payload: Any) -> None:
    result, _ = connector(monkeypatch, "directory")
    monkeypatch.setattr(result, "_account_get", lambda endpoint, params=None: response({"id": "repo"} if endpoint == "/repos/repo/" else payload))
    monkeypatch.setattr(result, "_get_directory_entries", lambda *args: SeaFileConnector._get_directory_entries.__wrapped__(result, *args))
    with pytest.raises(ValueError):
        collect_slim_document_snapshot(result)


def test_empty_and_denied_scopes_are_distinct(monkeypatch: pytest.MonkeyPatch) -> None:
    result, _ = connector(monkeypatch)
    monkeypatch.setattr(result, "_account_get", lambda *a, **kw: response([]))
    assert collect_slim_document_snapshot(result) == ()
    monkeypatch.setattr(result, "_get_libraries", lambda: [{"id": "repo", "name": "library"}])
    monkeypatch.setattr(result, "_get_directory_entries", lambda *a: (_ for _ in ()).throw(PermissionError("denied")))
    with pytest.raises(PermissionError):
        collect_slim_document_snapshot(result)
    with pytest.raises(PermissionError):
        list(result.load_from_state())


def test_wrong_library_and_shared_owner_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    result, _ = connector(monkeypatch, "library", True)
    monkeypatch.setattr(result, "_get_repo_info_via_repo_token", lambda: {"repo_id": "other"})
    with pytest.raises(ValueError, match="identity"):
        collect_slim_document_snapshot(result)
    result.include_shared = False
    with pytest.raises(ValueError, match="owner"):
        SeaFileConnector._get_libraries.__wrapped__(result)


@pytest.mark.parametrize("mode", ["incremental", "disabled", "first", "reindex", "body-failure"])
async def test_real_driver_deletion_switch(sync_env: dict[str, Any], mode: str) -> None:
    patch = sync_env["monkeypatch"]
    result, _ = connector(patch)
    patch.setattr(result, "load_credentials", lambda _: None)
    patch.setattr(sync_module, "SeaFileConnector", lambda **kwargs: result)
    if mode == "body-failure":
        patch.setattr(result, "poll_source", lambda *args: (_ for _ in ()).throw(PermissionError("body")))
    driver = sync_module.SeaFile({"seafile_url": "https://seafile.test", "sync_deleted_files": mode != "disabled", "credentials": {}})
    current = task()
    if mode == "first":
        current["poll_range_start"] = None
    elif mode == "reindex":
        current["reindex"] = "1"
    await driver(current)
    if mode == "body-failure":
        assert sync_env["calls"] == ["start", "fail"]
    elif mode == "incremental":
        assert sync_env["calls"][1][0] == "cleanup"
    else:
        assert not any(isinstance(call, tuple) and call[0] == "cleanup" for call in sync_env["calls"])


def test_size_shared_scope_and_poll_end_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    result, _ = connector(monkeypatch)
    result.include_shared = False
    result.current_user_email = "owner@test"
    monkeypatch.setattr(result, "_account_get", lambda *a, **kw: response([{"id": "repo", "owner": "owner@test"}, {"id": "shared", "owner": "other@test"}]))
    assert [lib["id"] for lib in result._get_libraries()] == ["repo"]
    result.size_threshold = 3
    monkeypatch.setattr(result, "_get_directory_entries", lambda *a: [{"type": "file", "id": "big", "name": "big.txt", "size": 4, "mtime": 1735689600}])
    assert collect_slim_document_snapshot(result) == ()
    assert list(result.load_from_state()) == []
    result.size_threshold = 4
    assert len(collect_slim_document_snapshot(result)) == 1
    boundary = datetime(2025, 1, 1, tzinfo=UTC).timestamp()
    monkeypatch.setattr(result, "_get_file_download_link", lambda *a: "https://download.test/file")
    assert len(list(result.poll_source(boundary - 1, boundary))) == 1
    assert list(result.poll_source(boundary, boundary + 1)) == []
