"""Gmail complete inventory and strict content reads fail closed before pruning."""

from types import SimpleNamespace
from typing import Any

import pytest
from google.oauth2.credentials import Credentials
from googleapiclient.errors import HttpError
from httplib2 import Response

from common.data_source import gmail_connector as gmail
from common.data_source.interfaces import collect_slim_document_snapshot


class GmailAPI:
    def __init__(self, pages: list[Any], content: Any = None) -> None:
        self.pages = pages
        self.content = content
        self.calls: list[dict[str, Any]] = []
        self.get_calls: list[dict[str, Any]] = []

    def users(self) -> "GmailAPI":
        return self

    def threads(self) -> "GmailAPI":
        return self

    def list(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)

        def execute() -> Any:
            page = self.pages[int(kwargs.get("pageToken", "0"))]
            if isinstance(page, Exception):
                raise page
            return page

        return SimpleNamespace(execute=execute)

    def get(self, **kwargs: Any) -> Any:
        self.get_calls.append(kwargs)

        def execute() -> Any:
            if isinstance(self.content, Exception):
                raise self.content
            return self.content

        return SimpleNamespace(execute=execute)


def connector(monkeypatch: pytest.MonkeyPatch, api: GmailAPI, *, strict: bool = True) -> gmail.GmailConnector:
    value = gmail.GmailConnector(batch_size=1, require_complete=strict)
    value._creds = Credentials("synthetic")
    value._primary_admin_email = "user@example.test"
    monkeypatch.setattr(gmail, "get_gmail_service", lambda *args: api)
    return value


def test_snapshot_is_identity_only_and_exhausts_pages_without_time_range(monkeypatch: pytest.MonkeyPatch) -> None:
    api = GmailAPI(
        [{"threads": [{"id": "one"}], "resultSizeEstimate": 3, "nextPageToken": "1"}, {"threads": [{"id": "two"}], "resultSizeEstimate": 2, "nextPageToken": "2"}, {"resultSizeEstimate": 0}]
    )
    value = connector(monkeypatch, api)
    monkeypatch.setattr(gmail, "SLIM_BATCH_SIZE", 1)
    monkeypatch.setattr(gmail, "get_admin_service", lambda *args: pytest.fail("OAuth must only enumerate its own mailbox"))
    assert [doc.id for doc in collect_slim_document_snapshot(value)] == ["one", "two"]
    assert len(api.calls) == 3 and api.get_calls == []
    assert all(call["q"] is None and "resultSizeEstimate" in call["fields"] for call in api.calls)
    assert all(call["userId"] == value.primary_admin_email for call in api.calls)


@pytest.mark.parametrize(
    "page",
    [
        {},
        {"resultSizeEstimate": True},
        {"resultSizeEstimate": -1},
        {"resultSizeEstimate": 1},
        {"resultSizeEstimate": 0, "threads": None},
        {"resultSizeEstimate": 1, "threads": [{"id": ""}]},
        {"resultSizeEstimate": 1, "threads": [None]},
        {"resultSizeEstimate": 1, "threads": [{"id": "one"}], "nextPageToken": "1"},
        {"resultSizeEstimate": 0, "nextPageToken": 3},
    ],
)
def test_partial_or_malformed_final_page_never_publishes_snapshot(monkeypatch: pytest.MonkeyPatch, page: dict[str, Any]) -> None:
    value = connector(monkeypatch, GmailAPI([{"threads": [{"id": "one"}], "resultSizeEstimate": 2, "nextPageToken": "1"}, page]))
    with pytest.raises(ValueError):
        collect_slim_document_snapshot(value)


@pytest.mark.parametrize("status", [400, 403, 404, 429, 500])
def test_later_page_errors_invalidate_the_whole_inventory(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    value = connector(
        monkeypatch, GmailAPI([{"threads": [{"id": "one"}], "resultSizeEstimate": 2, "nextPageToken": "1"}, HttpError(Response({"status": str(status)}), b'{"error":{"message":"denied"}}')])
    )
    with pytest.raises(HttpError):
        collect_slim_document_snapshot(value)


def test_complete_empty_mailbox_authorizes_empty_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    assert collect_slim_document_snapshot(connector(monkeypatch, GmailAPI([{"resultSizeEstimate": 0}]))) == ()


def test_multi_hop_pagination_loop_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    pages = [{"threads": [{"id": str(i)}], "resultSizeEstimate": 4, "nextPageToken": str((i + 1) % 3)} for i in range(3)]
    with pytest.raises(ValueError, match="pagination"):
        collect_slim_document_snapshot(connector(monkeypatch, GmailAPI(pages)))


@pytest.mark.parametrize(
    "content",
    [HttpError(Response({"status": str(status)}), b'{"error":{"message":"Mail service not enabled"}}') for status in [400, 403, 404, 429, 500]]
    + [TimeoutError("read failed"), {}, {"id": "one", "messages": []}, {"id": "other", "messages": [{}]}],
)
def test_strict_content_failure_cannot_be_skipped(monkeypatch: pytest.MonkeyPatch, content: Any) -> None:
    api = GmailAPI([{"threads": [{"id": "one"}], "resultSizeEstimate": 1}], content)
    with pytest.raises((HttpError, TimeoutError, ValueError)):
        list(connector(monkeypatch, api).poll_source(1, 2))
    assert len(api.get_calls) == 1


def test_legacy_reads_keep_permission_skip_when_deletion_is_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    api = GmailAPI([HttpError(Response({"status": "403"}), b'{"error":{"message":"denied"}}')])
    value = connector(monkeypatch, api, strict=False)
    monkeypatch.setattr(value, "_get_all_user_emails", lambda **kwargs: [value.primary_admin_email])
    assert list(value.poll_source(1, 2)) == []


@pytest.mark.parametrize(
    "pages", [[{}], [{"kind": "admin#directory#users", "users": [{"primaryEmail": "other@example.test"}]}], [HttpError(Response({"status": "404"}), b'{"error":{"message":"denied"}}')]]
)
def test_workspace_directory_failure_never_falls_back_to_a_partial_user_scope(monkeypatch: pytest.MonkeyPatch, pages: list[Any]) -> None:
    value = connector(monkeypatch, GmailAPI([]))
    monkeypatch.setattr(gmail, "OAuthCredentials", type("OtherOAuth", (), {}))
    admin = GmailAPI(pages)
    monkeypatch.setattr(gmail, "get_admin_service", lambda *args: admin)
    with pytest.raises((ValueError, PermissionError, HttpError)):
        collect_slim_document_snapshot(value)
    assert "kind" in admin.calls[0]["fields"]


def test_workspace_users_and_all_mailboxes_must_complete(monkeypatch: pytest.MonkeyPatch) -> None:
    api = GmailAPI([{"resultSizeEstimate": 0}])
    value = connector(monkeypatch, api)
    monkeypatch.setattr(gmail, "OAuthCredentials", type("OtherOAuth", (), {}))
    admin = GmailAPI(
        [
            {"kind": "admin#directory#users", "users": [{"primaryEmail": value.primary_admin_email}], "nextPageToken": "1"},
            {"kind": "admin#directory#users", "users": [{"primaryEmail": "second@example.test"}]},
        ]
    )
    monkeypatch.setattr(gmail, "get_admin_service", lambda *args: admin)
    assert collect_slim_document_snapshot(value) == ()
    assert [call["userId"] for call in api.calls] == [value.primary_admin_email, "second@example.test"]
