"""Complete source enumeration must never authorize deletion from a partial list."""

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from github import GithubException

from common.data_source.confluence_connector import ConfluenceConnector, OnyxConfluence, build_confluence_document_id
from common.data_source.exceptions import ConnectorValidationError
from common.data_source.github.connector import GithubConnector
from common.data_source.jira.connector import JiraConnector
from common.data_source.jira.utils import build_issue_url
from common.data_source.models import NotionPage, NotionSearchResponse
from common.data_source.notion_connector import NotionConnector


def notion_page(page_id: str) -> NotionPage:
    return NotionPage(id=page_id, created_time="2026-01-01T00:00:00Z", last_edited_time="2026-01-01T00:00:00Z", archived=False, properties={}, url=f"https://notion.so/{page_id}")


def ids(batches: Any) -> list[str]:
    return [doc.id for batch in batches for doc in batch]


def test_notion_nested_pages_databases_and_attachments_match_source_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    connector = NotionConnector(root_page_id="root", batch_size=2)
    blocks = {
        "root": [{"id": "nested", "type": "toggle", "has_children": True}, {"id": "db", "type": "child_database", "has_children": False}],
        "nested": [{"id": "file", "type": "file", "has_children": False}, {"id": "child", "type": "child_page", "has_children": True}],
        "child": [{"id": "image", "type": "image", "has_children": False}],
        "db-page": [],
    }
    monkeypatch.setattr(connector, "_fetch_page", notion_page)
    monkeypatch.setattr(connector, "_fetch_child_blocks", lambda block_id, cursor, strict: {"results": blocks[block_id], "next_cursor": None, "has_more": False})
    monkeypatch.setattr(connector, "_fetch_database", lambda database_id, cursor, strict: {"results": [{"id": "db-page", "object": "page"}], "next_cursor": None, "has_more": False})
    assert set(ids(connector.retrieve_all_slim_docs_perm_sync())) == {"root", "file", "child", "image", "db-page"}


def test_notion_root_block_pages_exhausted_and_duplicate_pages_deduplicated(monkeypatch: pytest.MonkeyPatch) -> None:
    connector = NotionConnector(root_page_id="root", batch_size=1)
    queries = []

    def fetch(block_id: str, cursor: str | None, strict: bool) -> dict[str, Any]:
        queries.append((block_id, cursor))
        assert strict is True
        if block_id != "root":
            return {"results": [], "next_cursor": None, "has_more": False}
        child_ids = ["one"] if cursor is None else ["one", "two"]
        return {"results": [{"id": page_id, "type": "child_page", "has_children": True} for page_id in child_ids], "next_cursor": "next" if cursor is None else None, "has_more": cursor is None}

    monkeypatch.setattr(connector, "_fetch_page", notion_page)
    monkeypatch.setattr(connector, "_fetch_child_blocks", fetch)
    assert ids(connector.retrieve_all_slim_docs_perm_sync()) == ["root", "one", "two"]
    assert queries == [("root", None), ("root", "next"), ("one", None), ("two", None)]


@pytest.mark.parametrize("cursor", [None, "same"])
def test_notion_invalid_root_block_pagination_fails(monkeypatch: pytest.MonkeyPatch, cursor: str | None) -> None:
    connector = NotionConnector(root_page_id="root")
    monkeypatch.setattr(connector, "_fetch_page", notion_page)
    monkeypatch.setattr(connector, "_fetch_child_blocks", lambda block_id, cursor_arg, strict: {"results": [], "next_cursor": cursor, "has_more": True})
    with pytest.raises(RuntimeError, match="pagination"):
        list(connector.retrieve_all_slim_docs_perm_sync())


@pytest.mark.parametrize("failure", [None, PermissionError("not shared")])
def test_notion_missing_block_cannot_be_complete(monkeypatch: pytest.MonkeyPatch, failure: Any) -> None:
    connector = NotionConnector(root_page_id="root")
    monkeypatch.setattr(connector, "_fetch_page", notion_page)

    def fetch(block_id: str, cursor: str | None, strict: bool) -> Any:
        assert strict is True
        if failure:
            raise failure
        return None

    monkeypatch.setattr(connector, "_fetch_child_blocks", fetch)
    with pytest.raises((RuntimeError, PermissionError)):
        list(connector.retrieve_all_slim_docs_perm_sync())


def test_notion_database_pagination_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    connector = NotionConnector()
    monkeypatch.setattr(connector, "_fetch_database", lambda database_id, cursor, strict: {"results": [], "next_cursor": None, "has_more": True})
    with pytest.raises(RuntimeError, match="pagination"):
        connector._read_slim_database("database")


@pytest.mark.parametrize("root_page_id", [None, "", "   ", "\t\n"])
def test_notion_missing_root_rejected_before_api_calls(monkeypatch: pytest.MonkeyPatch, root_page_id: str | None) -> None:
    connector = NotionConnector(root_page_id=root_page_id)

    def unexpected_call(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("Incomplete Notion scope must be rejected before calling the API.")

    for method in ["_search_notion", "_fetch_page", "_fetch_child_blocks", "_fetch_database"]:
        monkeypatch.setattr(connector, method, unexpected_call)
    with pytest.raises(ConnectorValidationError, match="explicit root_page_id"):
        next(connector.retrieve_all_slim_docs_perm_sync())


def test_notion_nonrecursive_root_rejected_before_api_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    connector = NotionConnector(root_page_id="root", recursive_index_enabled=False)
    assert connector.recursive_index_enabled is True
    connector.recursive_index_enabled = False

    def unexpected_call(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("Nonrecursive Notion scope must be rejected before calling the API.")

    monkeypatch.setattr(connector, "_fetch_page", unexpected_call)
    monkeypatch.setattr(connector, "_search_notion", unexpected_call)
    with pytest.raises(ConnectorValidationError, match="recursive indexing"):
        next(connector.retrieve_all_slim_docs_perm_sync())


@pytest.mark.parametrize("poll", [False, True])
def test_notion_ordinary_ingestion_without_root_still_uses_search(monkeypatch: pytest.MonkeyPatch, poll: bool) -> None:
    connector = NotionConnector()
    queries = []

    def search(query: dict[str, Any]) -> NotionSearchResponse:
        queries.append(query)
        return NotionSearchResponse(results=[], next_cursor=None)

    monkeypatch.setattr(connector, "_search_notion", search)
    batches = connector.poll_source(1, 2) if poll else connector.load_from_state()
    assert list(batches) == []
    assert len(queries) == 1


def test_notion_root_respects_cancellation(monkeypatch: pytest.MonkeyPatch) -> None:
    connector = NotionConnector(root_page_id="root")
    monkeypatch.setattr(connector, "_fetch_page", notion_page)
    with pytest.raises(RuntimeError, match="cancelled"):
        list(connector.retrieve_all_slim_docs_perm_sync(callback=SimpleNamespace(should_stop=lambda: True)))


class JiraResults(list[Any]):
    total = 2


def jira_issue(issue_id: str, attachments: list[dict[str, Any]] | None = None) -> SimpleNamespace:
    return SimpleNamespace(key=f"TEST-{issue_id}", raw={"id": issue_id, "fields": {"labels": [], "attachment": attachments or []}})


def test_jira_server_clamped_pages_and_attachments_use_full_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    connector = JiraConnector("https://jira.example", project_key="TEST", include_attachments=True)
    calls = []

    def search(**kwargs: Any) -> JiraResults:
        calls.append(kwargs)
        issue_id = str(kwargs["startAt"] + 1)
        return JiraResults([jira_issue(issue_id, [{"id": f"a{issue_id}", "filename": "a.pdf"}])])

    connector.jira_client = SimpleNamespace(_options={"rest_api_version": "2"}, search_issues=search)
    assert ids(connector.retrieve_all_slim_docs_perm_sync()) == [
        build_issue_url(connector.jira_base_url, "TEST-1"),
        "TEST-1::attachment::a1",
        build_issue_url(connector.jira_base_url, "TEST-2"),
        "TEST-2::attachment::a2",
    ]
    assert [call["startAt"] for call in calls] == [0, 1]
    assert all("updated >=" not in call["jql_str"] and "updated <=" not in call["jql_str"] for call in calls)
    assert all("attachment" in call["fields"] and "labels" in call["fields"] for call in calls)


def test_jira_cloud_partial_bulk_is_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    connector = JiraConnector("https://test.atlassian.net", project_key="TEST")
    connector.jira_client = SimpleNamespace(_options={"rest_api_version": "3"})
    monkeypatch.setattr(connector, "_enhanced_search_ids", lambda jql, cursor, strict: (["1", "2"], None))
    monkeypatch.setattr(connector, "_bulk_fetch_issues", lambda issue_ids, fields: [jira_issue("1")])
    with pytest.raises(RuntimeError, match="all requested"):
        list(connector.retrieve_all_slim_docs_perm_sync())


def test_jira_cloud_exhausts_all_cursor_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    connector = JiraConnector("https://test.atlassian.net", project_key="TEST")
    connector.jira_client = SimpleNamespace(_options={"rest_api_version": "3"})
    calls = []

    def search(jql: str, cursor: str | None, strict: bool) -> tuple[list[str], str | None]:
        calls.append(cursor)
        return (["1"], "cursor") if cursor is None else (["2"], None)

    monkeypatch.setattr(connector, "_enhanced_search_ids", search)
    monkeypatch.setattr(connector, "_bulk_fetch_issues", lambda issue_ids, fields: [jira_issue(issue_id) for issue_id in issue_ids])
    assert len(ids(connector.retrieve_all_slim_docs_perm_sync())) == 2
    assert calls == [None, "cursor"]


def test_jira_server_premature_empty_page_is_failure() -> None:
    connector = JiraConnector("https://jira.example", project_key="TEST")
    connector.jira_client = SimpleNamespace(_options={"rest_api_version": "2"}, search_issues=lambda **kwargs: JiraResults())
    with pytest.raises(RuntimeError, match="reported total"):
        list(connector.retrieve_all_slim_docs_perm_sync())


def test_jira_server_repeated_page_is_failure() -> None:
    connector = JiraConnector("https://jira.example", project_key="TEST")
    connector.jira_client = SimpleNamespace(_options={"rest_api_version": "2"}, search_issues=lambda **kwargs: JiraResults([jira_issue("1")]))
    with pytest.raises(RuntimeError, match="repeated a source ID"):
        list(connector.retrieve_all_slim_docs_perm_sync())


@pytest.mark.parametrize("payload", [{}, {"issues": [], "isLast": False}, {"issues": []}])
def test_jira_cloud_incomplete_pagination_response_is_failure(payload: dict[str, Any]) -> None:
    connector = JiraConnector("https://test.atlassian.net", project_key="TEST")
    connector.jira_client = SimpleNamespace(_get_url=lambda path: path, _session=SimpleNamespace(get=lambda *args, **kwargs: SimpleNamespace(raise_for_status=lambda: None, json=lambda: payload)))
    with pytest.raises(RuntimeError, match="incomplete"):
        connector._enhanced_search_ids("project=TEST", None, strict=True)


def test_jira_cloud_complete_empty_scope_is_valid() -> None:
    connector = JiraConnector("https://test.atlassian.net", project_key="TEST")
    connector.jira_client = SimpleNamespace(
        _get_url=lambda path: path, _session=SimpleNamespace(get=lambda *args, **kwargs: SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"issues": [], "isLast": True}))
    )
    assert connector._enhanced_search_ids("project=TEST", None, strict=True) == ([], None)


@pytest.mark.parametrize("username_key", ["jira_username", "username", "jira_user_email"])
def test_jira_server_credentials_support_all_username_aliases(monkeypatch: pytest.MonkeyPatch, username_key: str) -> None:
    from common.data_source.jira import connector as jira_module

    calls = []
    monkeypatch.setattr(jira_module, "JIRA", lambda **kwargs: calls.append(kwargs) or SimpleNamespace(myself=lambda: {}))
    connector = JiraConnector("https://jira.example", project_key="TEST")
    connector.load_credentials({username_key: "alice", "jira_password": "password"})
    assert calls[0]["basic_auth"] == ("alice", "password")
    assert calls[0]["options"]["rest_api_version"] == "2"


def test_confluence_both_slim_entry_points_keep_scope_and_identifiers(monkeypatch: pytest.MonkeyPatch) -> None:
    connector = ConfluenceConnector("https://wiki.example", is_cloud=False, space="TEAM")
    queries = []
    page = {"id": "123", "space": {"key": "TEAM"}, "_links": {"webui": "/display/TEAM/Page"}}
    attachment = {"id": "456", "title": "a.pdf", "metadata": {"mediaType": "application/pdf"}, "_links": {"webui": "/download/attachments/123/a.pdf"}}

    def paginate(**kwargs: Any) -> Iterator[dict[str, Any]]:
        queries.append(kwargs)
        yield attachment if "type=attachment" in kwargs["cql"] else page

    connector._confluence_client = SimpleNamespace(cql_paginate_all_expansions=paginate)
    expected = [build_confluence_document_id(connector.wiki_base, entry["_links"]["webui"], False) for entry in [page, attachment]]
    assert ids(connector.retrieve_all_slim_docs(start=1, end=2)) == expected
    assert ids(connector.retrieve_all_slim_docs_perm_sync()) == expected
    assert all(query["strict"] for query in queries)
    assert "space='TEAM'" in queries[0]["cql"]


@pytest.mark.parametrize("payload", [{}, {"results": [], "_links": {"next": "rest/api/content?start=1"}}])
def test_confluence_strict_pagination_rejects_incomplete_pages(payload: dict[str, Any]) -> None:
    client = object.__new__(OnyxConfluence)
    client._is_cloud = False
    client.get = lambda **kwargs: SimpleNamespace(raise_for_status=lambda: None, json=lambda: payload)
    with pytest.raises(RuntimeError):
        list(client._paginate_url("rest/api/content", strict=True))


def test_confluence_slim_does_not_use_lossy_500_recovery() -> None:
    client = object.__new__(OnyxConfluence)
    client._is_cloud = False

    def raise_error() -> None:
        raise RuntimeError("HTTP 500")

    client.get = lambda **kwargs: SimpleNamespace(raise_for_status=raise_error, status_code=500, text="error", __dict__={})
    with pytest.raises(RuntimeError, match="HTTP 500"):
        list(client._paginate_url("rest/api/content?start=0", strict=True))


def test_github_all_configured_repositories_must_be_accessible() -> None:
    connector = GithubConnector("owner", repositories="good,bad")

    def get_repo(name: str) -> Any:
        if name.endswith("bad"):
            raise GithubException(403, {"message": "forbidden"})
        return SimpleNamespace(get_pulls=lambda **kwargs: [])

    connector.github_client = SimpleNamespace(get_repo=get_repo)
    with pytest.raises(GithubException):
        list(connector.retrieve_all_slim_docs_perm_sync())


def test_github_empty_repo_validation_and_enumeration() -> None:
    connector = GithubConnector("owner", repositories="empty")
    connector.github_client = SimpleNamespace(get_repo=lambda name: SimpleNamespace(get_pulls=lambda **kwargs: []))
    connector.validate_connector_settings()
    assert ids(connector.retrieve_all_slim_docs_perm_sync()) == []


def test_github_slim_identifiers_do_not_require_content_conversion() -> None:
    connector = GithubConnector("owner", repositories="repo", include_issues=True)
    repository = SimpleNamespace(
        get_pulls=lambda **kwargs: [SimpleNamespace(html_url="https://github.com/owner/repo/pull/1")],
        get_issues=lambda **kwargs: [
            SimpleNamespace(html_url="https://github.com/owner/repo/issues/2", pull_request=None),
            SimpleNamespace(html_url="https://github.com/owner/repo/pull/1", pull_request={}),
        ],
    )
    connector.github_client = SimpleNamespace(get_repo=lambda name: repository)
    assert ids(connector.retrieve_all_slim_docs_perm_sync()) == ["https://github.com/owner/repo/pull/1", "https://github.com/owner/repo/issues/2"]


def test_github_pagination_failure_is_not_complete() -> None:
    connector = GithubConnector("owner", repositories="repo")

    def pulls(**kwargs: Any) -> Iterator[Any]:
        yield SimpleNamespace(html_url="https://github.com/owner/repo/pull/1")
        raise PermissionError("next page denied")

    connector.github_client = SimpleNamespace(get_repo=lambda name: SimpleNamespace(get_pulls=pulls))
    with pytest.raises(PermissionError):
        list(connector.retrieve_all_slim_docs_perm_sync())
