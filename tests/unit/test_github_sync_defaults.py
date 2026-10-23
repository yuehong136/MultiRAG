"""Configured sync uses real PyGithub pagination with controlled HTTP replies."""

import copy
from datetime import UTC, datetime

import pytest

from common.data_source.github import connector as github_module
from core.svr.sync_data_source import Github
from tests.support.github_sync_defaults import GithubTransport, github_config
from tests.support.github_sync_defaults import github_transport as github_transport


@pytest.mark.parametrize(
    "options,expected",
    [
        ({}, {"PullRequest", "Issue"}),
        ({"include_pull_requests": True, "include_issues": True}, {"PullRequest", "Issue"}),
        ({"include_pull_requests": False, "include_issues": False}, set()),
        ({"include_pull_requests": True, "include_issues": False}, {"PullRequest"}),
        ({"include_pull_requests": False, "include_issues": True}, {"Issue"}),
        ({"include_pull_requests": False}, {"Issue"}),
        ({"include_issues": False}, {"PullRequest"}),
    ],
)
@pytest.mark.parametrize("mode", ["first", "incremental", "reindex"])
async def test_configured_sync_defaults_opt_out_and_pagination(github_transport: GithubTransport, options: dict[str, bool], expected: set[str], mode: str) -> None:
    config = github_config(**options)
    original = copy.deepcopy(config)
    driver = Github(config)
    task = {"poll_range_start": datetime(2026, 1, 1, tzinfo=UTC) if mode != "first" else None, "reindex": "1" if mode == "reindex" else "0"}
    try:
        documents = [doc for batch in await driver._generate(task) for doc in batch]
        assert {doc.metadata["object_type"] for doc in documents} == expected
        assert len(documents) == 2 * len(expected)
        assert len({doc.id for doc in documents}) == len(documents)
        assert all(doc.blob.startswith(b"Controlled ") for doc in documents)
        assert config == original, "Reading a legacy config must not rewrite persisted options"
        for suffix, kind in [("pulls", "PullRequest"), ("issues", "Issue")]:
            pages = [int(call["query"].get("page", ["1"])[0]) for call in github_transport.calls if call["path"].endswith("/" + suffix)]
            assert pages == ([1, 2, 3] if kind in expected else [])
    finally:
        if driver.connector.github_client is not None:
            driver.connector.github_client.close()


@pytest.mark.parametrize("status", [401, 403, 404])
async def test_configured_sync_propagates_provider_access_denials(github_transport: GithubTransport, status: int) -> None:
    github_transport.status = status
    driver = Github(github_config())
    try:
        with pytest.raises(github_module.GithubException) as caught:
            list(await driver._generate({"poll_range_start": None}))
        assert caught.value.status == status
        assert [call["path"] for call in github_transport.calls] == ["/repos/fixture/project"]
    finally:
        if driver.connector.github_client is not None:
            driver.connector.github_client.close()


async def test_configured_sync_missing_credential_has_no_provider_requests(github_transport: GithubTransport) -> None:
    config = github_config()
    config["credentials"] = {}
    with pytest.raises(ValueError, match="Missing github_access_token"):
        await Github(config)._generate({})
    assert github_transport.calls == []
