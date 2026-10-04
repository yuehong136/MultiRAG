from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from common.data_source.asana_connector import AsanaAPI, AsanaConnector, AsanaTask
from common.data_source.interfaces import collect_slim_document_snapshot
from core.svr import sync_data_source as sync_module
from tests.unit.test_sync_deleted_snapshot_contract import sync_env as sync_env
from tests.unit.test_sync_deleted_snapshot_contract import task


def connector(monkeypatch: pytest.MonkeyPatch, mode: str = "") -> AsanaConnector:
    result = AsanaConnector("workspace", asana_project_ids="project", batch_size=1)
    api = AsanaAPI("synthetic", "workspace", "team")
    result.asana_client = api
    result.workspace_users_email = {"owner@test"}
    project = {"gid": "project", "name": "Project", "team": {"gid": "team"}, "archived": False, "privacy_setting": "private"}

    def projects(opts: Any, **kwargs: Any) -> dict[str, Any]:
        if mode == "missing-project":
            return {"data": [], "next_page": None}
        return {"data": [project], "next_page": None}

    def tasks(project_id: str, opts: Any, **kwargs: Any) -> dict[str, Any]:
        assert "modified_since" not in opts
        return {
            "data": [
                {
                    "gid": "task",
                    "name": "Task",
                    "notes": "body",
                    "created_by": None,
                    "due_on": None,
                    "completed_at": None,
                    "modified_at": "2026-02-01T00:00:00Z",
                    "permalink_url": "https://asana.test/task",
                }
            ],
            "next_page": None,
        }

    def attachments(parent: str, opts: Any, **kwargs: Any) -> dict[str, Any]:
        assert parent == "task"
        if opts.get("offset"):
            if mode == "attachment-page":
                raise PermissionError("denied")
            if mode == "malformed":
                return {"data": []}
            return {"data": [], "next_page": {"offset": "next"} if mode == "loop" else None}
        return {"data": [] if mode == "empty" else [{"gid": "attachment"}], "next_page": None if mode == "empty" else {"offset": "next"}}

    monkeypatch.setattr(api.project_api, "get_projects", projects)
    monkeypatch.setattr(api.project_api, "get_project", lambda *a, **kw: project)
    monkeypatch.setattr(api.tasks_api, "get_tasks_for_project", tasks)
    monkeypatch.setattr(api.attachments_api, "get_attachments_for_object", attachments)
    monkeypatch.setattr(api.attachments_api, "get_attachment", lambda **kw: {"gid": "attachment", "name": "same.txt", "download_url": "https://download.test", "size": 4})
    monkeypatch.setattr(api.stories_api, "get_stories_for_task", lambda *a, **kw: [])
    monkeypatch.setattr("common.data_source.asana_connector.requests.get", lambda *a, **kw: SimpleNamespace(content=b"body", raise_for_status=lambda: None))
    return result


def test_complete_paginated_attachment_ids_match_content_without_hydration(monkeypatch: pytest.MonkeyPatch) -> None:
    result = connector(monkeypatch)
    with monkeypatch.context() as patch:
        patch.setattr(result.asana_client.attachments_api, "get_attachment", lambda **kw: (_ for _ in ()).throw(AssertionError("no detail reads")))
        patch.setattr(result.asana_client, "get_tasks", lambda *a: (_ for _ in ()).throw(AssertionError("no comments or task body")))
        assert [d.id for d in collect_slim_document_snapshot(result)] == ["asana:task:attachment"]
    assert [d.id for batch in result.load_from_state() for d in batch] == ["asana:task:attachment"]


@pytest.mark.parametrize("mode", ["attachment-page", "missing-project", "malformed", "loop"])
def test_partial_snapshots_fail(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    with pytest.raises((PermissionError, ValueError)):
        collect_slim_document_snapshot(connector(monkeypatch, mode))


@pytest.mark.parametrize(
    "updates,eligible", [({"archived": True}, False), ({"team": None}, False), ({"team": {"gid": "other"}}, False), ({"privacy_setting": "public", "team": {"gid": "other"}}, True)]
)
def test_body_and_snapshot_share_project_eligibility(monkeypatch: pytest.MonkeyPatch, updates: dict[str, Any], eligible: bool) -> None:
    result = connector(monkeypatch)
    project = result.asana_client.project_api.get_project("project", {})
    project.update(updates)
    assert bool(collect_slim_document_snapshot(result)) == eligible
    assert bool(list(result.load_from_state())) == eligible


def test_utc_exclusive_end_boundary_and_no_truncation(monkeypatch: pytest.MonkeyPatch) -> None:
    result = connector(monkeypatch)
    boundary = datetime(2026, 2, 1, tzinfo=UTC)
    times = [boundary, boundary - timedelta(seconds=1), boundary.astimezone(timezone(timedelta(hours=8))), boundary.replace(tzinfo=None)]

    def tasks(projects: Any, start: str) -> list[AsanaTask]:
        assert start.endswith("+00:00")
        return [AsanaTask(str(i), "t", "", "", when, "p", "p") for i, when in enumerate(times)]

    monkeypatch.setattr(result.asana_client, "get_tasks", tasks)
    seen = []
    monkeypatch.setattr(result, "_task_to_documents", lambda task: seen.append(task.id) or [])
    list(result.poll_source(boundary.timestamp() - 1, boundary.timestamp()))
    assert seen == ["1"]


def test_attachment_detail_and_download_failures_propagate(monkeypatch: pytest.MonkeyPatch) -> None:
    result = connector(monkeypatch)
    with monkeypatch.context() as patch:
        patch.setattr(result.asana_client.attachments_api, "get_attachment", lambda **kw: {})
        with pytest.raises(ValueError):
            list(result.load_from_state())
    monkeypatch.setattr("common.data_source.asana_connector.requests.get", lambda *a, **kw: (_ for _ in ()).throw(PermissionError("download denied")))
    with pytest.raises(PermissionError):
        list(result.load_from_state())


@pytest.mark.parametrize("mode", ["incremental", "disabled", "first", "reindex", "listing-failure"])
async def test_asana_driver_gate(sync_env: dict[str, Any], mode: str) -> None:
    patch = sync_env["monkeypatch"]
    result = connector(patch, "attachment-page" if mode == "listing-failure" else "")
    patch.setattr(result, "load_credentials", lambda _: None)
    patch.setattr(sync_module, "AsanaConnector", lambda *a, **kw: result)
    patch.setattr(sync_module.SyncLogsService, "duplicate_and_parse", lambda db, kb, docs, *args: ([], [doc["id"] for doc in docs]))
    driver = sync_module.Asana({"asana_workspace_id": "workspace", "sync_deleted_files": mode != "disabled", "credentials": {"asana_api_token_secret": "synthetic"}})
    current = task()
    if mode == "first":
        current["poll_range_start"] = None
    if mode == "reindex":
        current["reindex"] = "1"
    await driver(current)
    if mode == "listing-failure":
        assert sync_env["calls"] == ["start", "fail"]
    else:
        assert sync_env["calls"][-1] == "complete"
        assert any(isinstance(call, tuple) and call[0] == "cleanup" for call in sync_env["calls"]) == (mode == "incremental")


@pytest.mark.parametrize("page", [{}, {"data": None, "next_page": None}, {"data": [{}], "next_page": None}, {"data": [], "next_page": {}}, {"data": [], "next_page": {"offset": ""}}])
def test_incomplete_api_pages_never_become_empty(page: Any) -> None:
    with pytest.raises(ValueError):
        list(AsanaAPI._iter_list(lambda **kwargs: page, opts={}))


@pytest.mark.parametrize("workspace,projects", [("", None), ("workspace", " , ")])
def test_invalid_scope_fails_before_enumeration(workspace: str, projects: str | None) -> None:
    with pytest.raises(ValueError):
        AsanaConnector(workspace, asana_project_ids=projects)
