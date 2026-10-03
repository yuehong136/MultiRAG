from typing import Any

import httpx
import pytest

from common.data_source.bitbucket import utils
from common.data_source.bitbucket.connector import BitbucketConnector
from common.data_source.bitbucket.utils import map_pr_to_document
from common.data_source.interfaces import collect_slim_document_snapshot


def make_connector(monkeypatch: pytest.MonkeyPatch, responses: list[dict[str, Any] | int], **kwargs: Any) -> tuple[BitbucketConnector, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        item = responses.pop(0)
        return httpx.Response(item, json={"error": "denied"}) if isinstance(item, int) else httpx.Response(200, json=item)

    def get(client: httpx.Client, url: str, params: dict[str, Any] | None = None) -> httpx.Response:
        response = client.get(url, params=params)
        response.raise_for_status()
        return response

    monkeypatch.setattr(utils, "bitbucket_get", get)
    value = BitbucketConnector(workspace="workspace", **kwargs)
    monkeypatch.setattr(value, "_client", lambda: httpx.Client(transport=httpx.MockTransport(handle)))
    return value, requests


def test_snapshot_paginated_repositories_and_prs_share_ingestion_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    value, requests = make_connector(
        monkeypatch,
        [
            {"values": [{"slug": "repo1"}], "next": "https://api.bitbucket.org/repos?page=2"},
            {"values": [{"id": 1}], "next": "https://api.bitbucket.org/prs?page=2"},
            {"values": [{"id": 2}]},
            {"values": [{"slug": "repo2"}]},
            {"values": [{"id": 3}]},
        ],
    )
    assert [doc.id for doc in collect_slim_document_snapshot(value)] == [
        map_pr_to_document({"id": number, "updated_on": "2026-01-01T00:00:00Z"}, "workspace", repo).id for repo, number in [("repo1", 1), ("repo1", 2), ("repo2", 3)]
    ]
    assert len(requests) == 5
    for request in [requests[1], requests[4]]:
        assert "updated_on" not in request.url.params["q"]
        assert all(state in request.url.params["q"] for state in ["OPEN", "MERGED", "DECLINED"])
        assert "values.id" in request.url.params["fields"] and "values.description" not in request.url.params["fields"]


@pytest.mark.parametrize("last", [403, 404, {}, {"values": "bad"}, {"values": [{"id": None}]}])
def test_later_page_permission_or_incomplete_results_abort_snapshot(monkeypatch: pytest.MonkeyPatch, last: dict[str, Any] | int) -> None:
    value, _ = make_connector(monkeypatch, [{"values": [{"id": 1}], "next": "https://api.bitbucket.org/prs?page=2"}, last], repositories="repo")
    with pytest.raises((httpx.HTTPStatusError, ValueError)):
        collect_slim_document_snapshot(value)


def test_missing_repository_identity_cannot_become_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    value, _ = make_connector(monkeypatch, [{"values": [{}]}])
    with pytest.raises(ValueError, match="repository"):
        collect_slim_document_snapshot(value)


def test_empty_workspace_and_empty_repository_are_authoritative(monkeypatch: pytest.MonkeyPatch) -> None:
    for kwargs in [{}, {"repositories": "repo"}, {"projects": "project"}]:
        value, _ = make_connector(monkeypatch, [{"values": []}], **kwargs)
        assert collect_slim_document_snapshot(value) == ()
