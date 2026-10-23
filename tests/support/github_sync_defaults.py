"""Controlled GitHub HTTP fixtures shared by unit and integration contracts."""

import json
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
import requests

from common.data_source.github import connector as github_module

BASE_URL = "https://github.contract.test"
REPOSITORY = {"id": 7, "name": "project", "full_name": "fixture/project", "url": f"{BASE_URL}/repos/fixture/project"}


def github_config(**options: bool) -> dict[str, Any]:
    return {
        "repository_owner": "fixture",
        "repository_name": "project",
        "credentials": {"github_access_token": "synthetic-github-token"},
        **options,
    }


def github_object(number: int, kind: str) -> dict[str, Any]:
    return {
        "id": 1000 + number,
        "number": number,
        "url": f"{BASE_URL}/repos/fixture/project/{kind}/{number}",
        "html_url": f"https://github.example/fixture/project/{'pull' if kind == 'pulls' else 'issues'}/{number}",
        "title": f"{kind} {number}",
        "body": f"Controlled {kind} body {number}",
        "created_at": "2026-01-03T00:00:00Z",
        "updated_at": "2026-01-04T00:00:00Z",
        "closed_at": None,
        "merged_at": None,
        "merged_by": None,
        "merged": False,
        "state": "open",
        "assignees": [],
        "labels": [],
        "commits": 1,
        "changed_files": 1,
        "user": None,
        "base": {"repo": REPOSITORY},
        "repository": REPOSITORY,
        "pull_request": None,
    }


class GithubTransport:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.status = 200

    def respond(self, request: requests.PreparedRequest) -> requests.Response:
        url = urlsplit(request.url or "")
        assert url.hostname == "github.contract.test"
        assert request.method == "GET", "GitHub sync must never write to the provider"
        assert request.headers.get("Authorization") == "token synthetic-github-token"
        query = parse_qs(url.query)
        self.calls.append({"method": request.method, "path": url.path, "query": query})
        result: Any = dict(REPOSITORY)
        if self.status != 200:
            result = {"message": "Controlled access denied"}
        elif url.path.rsplit("/", 1)[-1].isdigit():
            kind, number = url.path.split("/")[-2:]
            assert kind in {"pulls", "issues"}
            result = github_object(int(number), kind)
            if kind == "issues" and number == "1":
                result["pull_request"] = {"url": f"{BASE_URL}/repos/fixture/project/pulls/1"}
        elif url.path.endswith(("/pulls", "/issues")):
            page = int(query.get("page", ["1"])[0])
            assert query["state"] == ["all"]
            assert query["sort"] == ["updated"]
            assert query["direction"] == ["desc"]
            if url.path.endswith("/pulls"):
                result = [github_object(page, "pulls")] if page <= 2 else []
            elif page == 1:
                duplicate = github_object(1, "issues")
                duplicate["pull_request"] = {"url": f"{BASE_URL}/repos/fixture/project/pulls/1"}
                result = [duplicate, github_object(3, "issues")]
            else:
                result = [github_object(4, "issues")] if page == 2 else []
        else:
            assert url.path == "/repos/fixture/project", f"Unexpected provider endpoint: {url.path}"
        response = requests.Response()
        response.status_code = self.status
        response.url = request.url or ""
        response.request = request
        response.headers.update({"content-type": "application/json", "x-ratelimit-remaining": "100"})
        response._content = json.dumps(result).encode()
        return response


@pytest.fixture
def github_transport(monkeypatch: pytest.MonkeyPatch) -> GithubTransport:
    transport = GithubTransport()
    original_send = requests.Session.send

    def send(session: requests.Session, request: requests.PreparedRequest, **kwargs: Any) -> requests.Response:
        hostname = urlsplit(request.url or "").hostname
        if hostname == "github.contract.test":
            return transport.respond(request)
        assert hostname not in {"github.com", "api.github.com"}, "Live GitHub traffic is forbidden in this test"
        return original_send(session, request, **kwargs)

    monkeypatch.setattr(github_module, "GITHUB_CONNECTOR_BASE_URL", BASE_URL)
    monkeypatch.setattr(requests.Session, "send", send)
    return transport
