from collections.abc import Iterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from common.data_source.gitlab_connector import GitlabConnector
from common.data_source.interfaces import collect_slim_document_snapshot
from core.svr import sync_data_source as sync_module
from tests.unit.test_sync_deleted_snapshot_contract import sync_env as sync_env
from tests.unit.test_sync_deleted_snapshot_contract import task


class Project:
    default_branch = "main"
    empty_repo = False

    def __init__(self, *, failure: str = "") -> None:
        self.failure = failure
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.downloads: list[str] = []
        self.mergerequests = SimpleNamespace(list=lambda **kwargs: self.objects("mr", kwargs))
        self.issues = SimpleNamespace(list=lambda **kwargs: self.objects("issue", kwargs))
        self.files = SimpleNamespace(get=self.download)
        self.commits = SimpleNamespace(list=self.commit)

    def repository_tree(self, **kwargs: Any) -> Iterator[dict[str, str]]:
        self.calls.append(("tree", kwargs))
        assert kwargs["iterator"] is True and kwargs["ref"] == "main"
        if not kwargs["path"]:
            yield {"path": "root.md", "name": "root.md", "type": "blob"}
            yield {"path": "logs", "type": "tree"}
            yield {"path": "nested", "type": "tree"}
        else:
            if self.failure == "tree":
                raise PermissionError("nested directory denied")
            yield {"path": "nested/root.md", "name": "root.md", "type": "blob"}

    def objects(self, kind: str, kwargs: dict[str, Any]) -> Iterator[Any]:
        self.calls.append((kind, kwargs))
        assert kwargs["iterator"] is True
        # Old items can precede recent items; no family or later page may be skipped.
        for i, updated in enumerate(["2025-01-01T00:00:00Z", "2026-02-01T00:00:00Z"]):
            if i and self.failure == kind:
                raise PermissionError("later page denied")
            yield SimpleNamespace(
                web_url=f"https://gitlab.test/group/project/{kind}/{i}", updated_at=updated, title=f"{kind}-{i}", description="body", author={"name": "author"}, state="opened", type="Issue"
            )

    def download(self, *, file_path: str, ref: str) -> Any:
        self.downloads.append(file_path)
        if self.failure == "body":
            raise PermissionError("file denied")
        return SimpleNamespace(decode=lambda: b"code body")

    def commit(self, **kwargs: Any) -> list[Any]:
        assert "path" not in kwargs and kwargs["query_parameters"]["path"].endswith("root.md")
        if self.failure == "commit":
            raise PermissionError("commit denied")
        return [SimpleNamespace(committed_date="2026-02-01T00:00:00Z")]


def connector(project: Project, **kwargs: Any) -> GitlabConnector:
    source = GitlabConnector("group", "project", batch_size=1, **kwargs)
    source.gitlab_client = SimpleNamespace(url="https://gitlab.test", projects=SimpleNamespace(get=lambda path: project))
    return source


@pytest.mark.parametrize("code,mrs,issues", [(True, True, True), (True, False, False), (False, True, False), (False, False, True), (False, False, False)])
def test_complete_scope_matches_ingestion_without_downloading_bodies(code: bool, mrs: bool, issues: bool) -> None:
    project = Project()
    source = connector(project, include_code_files=code, include_mrs=mrs, include_issues=issues, state_filter="opened")
    snapshot = collect_slim_document_snapshot(source)
    assert not project.downloads
    documents = [doc for batch in source.load_from_state() for doc in batch]
    assert {doc.id for doc in snapshot} == {doc.id for doc in documents}
    assert len(snapshot) == 2 * (code + mrs + issues)
    assert all(options["state"] == "opened" for kind, options in project.calls if kind != "tree")
    assert all(options["path"] != "logs" for kind, options in project.calls if kind == "tree")


def test_incremental_old_mr_does_not_skip_later_mrs_or_issues() -> None:
    source = connector(Project())
    documents = [doc for batch in source.poll_source(datetime(2026, 1, 1, tzinfo=UTC).timestamp(), datetime(2026, 3, 1, tzinfo=UTC).timestamp()) for doc in batch]
    assert [doc.semantic_identifier for doc in documents] == ["mr-1", "issue-1"]


@pytest.mark.parametrize("failure", ["tree", "mr", "issue"])
def test_partial_listing_never_publishes_a_snapshot(failure: str) -> None:
    with pytest.raises(PermissionError):
        collect_slim_document_snapshot(connector(Project(failure=failure), include_code_files=True))


@pytest.mark.parametrize("failure", ["body", "commit"])
def test_content_or_timestamp_read_failure_propagates(failure: str) -> None:
    with pytest.raises(PermissionError):
        list(connector(Project(failure=failure), include_code_files=True).load_from_state())


@pytest.mark.parametrize("mode", ["incremental", "disabled", "first", "reindex", "listing-failure", "body-failure", "ingest-failure"])
async def test_real_driver_gates_deletion(sync_env: dict[str, Any], mode: str) -> None:
    monkeypatch = sync_env["monkeypatch"]
    project = Project(failure={"listing-failure": "tree", "body-failure": "body"}.get(mode, ""))
    source = connector(project, include_code_files=True)
    monkeypatch.setattr(source, "load_credentials", lambda credentials: None)
    arguments: list[dict[str, Any]] = []

    def build(**kwargs: Any) -> GitlabConnector:
        arguments.append(kwargs)
        return source

    monkeypatch.setattr(sync_module, "GitlabConnector", build)
    monkeypatch.setattr(sync_module.SyncLogsService, "duplicate_and_parse", lambda db, kb, docs, *args: (["failed"] if mode == "ingest-failure" else [], [doc["id"] for doc in docs]))
    driver = sync_module.Gitlab({"project_owner": "group", "project_name": "project", "state_filter": "opened", "sync_deleted_files": mode != "disabled", "include_code_files": True, "batch_size": 1})
    current = task()
    if mode == "first":
        current["poll_range_start"] = None
    if mode == "reindex":
        current["reindex"] = "1"
    await driver(current)
    assert arguments[0]["state_filter"] == "opened" and arguments[0]["batch_size"] == 1
    if mode.endswith("failure"):
        assert sync_env["calls"] == ["start", "fail"]
    elif mode == "incremental":
        assert sync_env["calls"][1][0] == "cleanup" and sync_env["calls"][-1] == "complete"
    else:
        assert sync_env["calls"] == ["start", "complete"]


def test_explicit_empty_repository_is_authoritative_but_missing_branch_is_not() -> None:
    project = Project()
    project.default_branch = None
    source = connector(project, include_code_files=True, include_mrs=False, include_issues=False)
    with pytest.raises(ValueError, match="default branch"):
        collect_slim_document_snapshot(source)
    project.empty_repo = True
    assert collect_slim_document_snapshot(source) == ()
    assert list(source.load_from_state()) == []
