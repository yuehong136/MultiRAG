from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from office365.runtime.client_request_exception import ClientRequestException

from common.data_source import sharepoint_connector as module
from common.data_source.models import ConnectorCheckpoint
from tests.support.connector_sdk_fakes import MODIFIED, SITE_URL, GraphHTTP
from tests.support.resource_journal import ResourceJournal


@pytest.fixture(autouse=True)
def case_journal(request: pytest.FixtureRequest) -> Iterator[None]:
    journal = ResourceJournal(request.node.nodeid)
    yield
    journal.event("case-complete", external_resources=[])


@pytest.fixture
def graph(monkeypatch: pytest.MonkeyPatch) -> tuple[module.SharePointConnector, GraphHTTP, dict[str, Any]]:
    http = GraphHTTP()
    auth: dict[str, Any] = {"constructed": 0, "acquired": 0}

    class App:
        def __init__(self, **kwargs: Any) -> None:
            auth["constructed"] += 1
            assert kwargs["timeout"] == 30

        def acquire_token_for_client(self, scopes: list[str]) -> dict[str, Any]:
            auth["acquired"] += 1
            assert scopes == module.GRAPH_SCOPES
            return auth.get("result", {"access_token": "controlled-token", "token_type": "Bearer", "expires_in": 3600})

    monkeypatch.setattr(module.msal, "ConfidentialClientApplication", App)
    monkeypatch.setattr(module.requests, "get", http)
    connector = module.SharePointConnector(batch_size=1)
    connector.load_credentials({"tenant_id": "tenant", "client_id": "client", "client_secret": "controlled-secret", "site_url": SITE_URL})
    return connector, http, auth


def test_lazy_auth_real_sdk_paging_and_blobs(graph: tuple[Any, Any, Any]) -> None:
    connector, http, auth = graph
    assert auth == {"constructed": 0, "acquired": 0} and http.calls == []
    docs = [doc for batch in connector.poll_source(0.0, 9e12) for doc in batch]
    assert {doc.id for doc in docs} == {"drive-A:same", "drive-A:nested", "drive-B:same"}
    assert all(doc.blob == b"controlled file bytes" and doc.size_bytes == len(doc.blob) for doc in docs)
    assert [doc.extension for doc in docs] == [".txt", ".md", ".txt"]
    assert docs[1].semantic_identifier == "日本語.md"
    assert auth["constructed"] == 1 and auth["acquired"] >= 1
    assert len(http.calls) == 10
    assert sum("/content" in url for url in http.calls) == 3


def test_checkpoint_bounds_and_slim_no_download(graph: tuple[Any, Any, Any]) -> None:
    connector, http, _ = graph
    boundary = datetime.fromisoformat(MODIFIED.replace("Z", "+00:00")).timestamp()
    assert list(connector.poll_source(boundary, boundary + 1)) == []
    assert not any("/content" in url for url in http.calls)
    docs = list(connector.load_from_checkpoint(boundary - 1, boundary, ConnectorCheckpoint(has_more=True)))
    assert len(docs) == 3 and all(doc.doc_updated_at.tzinfo == UTC for doc in docs)
    before = len(http.calls)
    slim = [doc.id for batch in connector.retrieve_all_slim_docs_perm_sync() for doc in batch]
    assert set(slim) == {doc.id for doc in docs}
    assert not any("/content" in url for url in http.calls[before:])
    generator = connector.load_from_checkpoint(0.0, boundary, ConnectorCheckpoint(has_more=False))
    with pytest.raises(StopIteration) as stopped:
        next(generator)
    assert stopped.value.value.has_more is False
    assert connector.validate_checkpoint_json('{"has_more":false}').has_more is False
    with pytest.raises(ValueError):
        connector.validate_checkpoint_json("broken json")


@pytest.mark.parametrize("mode", ["permission", "malformed", "repeat", "download"])
def test_later_page_and_content_failures_propagate(graph: tuple[Any, Any, Any], mode: str) -> None:
    connector, http, _ = graph
    http.mode = mode
    with pytest.raises(ClientRequestException if mode in {"permission", "download"} else module.ConnectorValidationError):
        list(connector.poll_source(0.0, 9e12))
    assert http.calls


def test_token_failure_does_not_leak_response(graph: tuple[Any, Any, Any]) -> None:
    connector, http, auth = graph
    auth["result"] = {"error": "invalid_client", "error_description": "sensitive-secret"}
    with pytest.raises(module.ConnectorMissingCredentialError) as error:
        list(connector.poll_source(0.0, 9e12))
    assert "sensitive-secret" not in str(error.value)
    assert http.calls == []


@pytest.mark.parametrize("value", [None, "invalid", "2026-01-01T00:00:00", 0])
def test_invalid_file_time_fails_before_download(graph: tuple[Any, Any, Any], monkeypatch: pytest.MonkeyPatch, value: Any) -> None:
    connector, http, _ = graph
    original = http.file
    monkeypatch.setattr(http, "file", lambda identifier, name: {**original(identifier, name), "lastModifiedDateTime": value})
    with pytest.raises(module.ConnectorValidationError):
        list(connector.poll_source(0.0, 9e12))
    assert not any("/content" in url for url in http.calls)


def test_cancellation_during_paging_stops_next_request(graph: tuple[Any, Any, Any]) -> None:
    connector, http, _ = graph
    stopped = False

    def cancel(path: str) -> None:
        nonlocal stopped
        if path.endswith("/children-page-2"):
            stopped = True

    def check() -> None:
        if stopped:
            raise InterruptedError("controlled cancellation")

    connector._check_active = check
    http.after_request = cancel
    with pytest.raises(InterruptedError):
        list(connector.poll_source(0.0, 9e12))
    assert not any("items/folder" in url for url in http.calls)


@pytest.mark.parametrize("batch_size", [0, -1, True, "1", None])
def test_invalid_batch_is_rejected(batch_size: Any) -> None:
    with pytest.raises(module.ConnectorValidationError):
        module.SharePointConnector(batch_size=batch_size)


@pytest.mark.parametrize(
    "credentials",
    [
        {},
        {"tenant_id": "t", "client_id": "c", "client_secret": "s", "site_url": "http://contoso.sharepoint.com/sites/x"},
        {"tenant_id": "../other", "client_id": "c", "client_secret": "s", "site_url": SITE_URL},
    ],
)
def test_credentials_rejected_without_network(credentials: dict[str, Any]) -> None:
    with pytest.raises((module.ConnectorMissingCredentialError, module.ConnectorValidationError)):
        module.SharePointConnector().load_credentials(credentials)


def test_untrusted_nextlink_rejected_before_token_or_request(graph: tuple[Any, Any, Any]) -> None:
    connector, http, auth = graph
    from office365.runtime.http.request_options import RequestOptions

    request = RequestOptions("https://untrusted.test/next")
    with pytest.raises(module.ConnectorValidationError):
        connector.graph_client.pending_request().execute_request_direct(request)
    assert auth["constructed"] == 0 and http.calls == []


@pytest.mark.parametrize("body", [b'{"empty":[],"count":0}', b""])
def test_json_mime_download_remains_raw_bytes(graph: tuple[Any, Any, Any], body: bytes) -> None:
    connector, http, _ = graph
    http.body, http.content_type = body, "application/json"
    docs = [doc for batch in connector.poll_source(0.0, 9e12) for doc in batch]
    assert len(docs) == 3
    assert all(doc.blob == body and doc.size_bytes == len(body) for doc in docs)
