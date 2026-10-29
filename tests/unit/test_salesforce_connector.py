"""Salesforce scope, OAuth confinement, complete paging and checkpoint contracts."""

from __future__ import annotations

import json
import traceback
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from common.data_source import salesforce_connector as salesforce
from common.data_source.exceptions import ConnectorMissingCredentialError, ConnectorValidationError
from common.data_source.models import ConnectorCheckpoint
from tests.support.resource_journal import ResourceJournal
from tests.support.salesforce_transport import ACCOUNT_A, ACCOUNT_B, MODIFIED, ORIGIN, SECRET, TOKEN, SalesforceTransport, configuration, install_transport


@pytest.fixture(autouse=True)
def journal(request: pytest.FixtureRequest) -> Iterator[None]:
    record = ResourceJournal(request.node.nodeid)
    yield
    record.event("case-complete", external_resources=[])


@pytest.fixture
def connection(monkeypatch: pytest.MonkeyPatch) -> tuple[salesforce.SalesforceConnector, SalesforceTransport]:
    boundary = SalesforceTransport()
    install_transport(monkeypatch, boundary)
    return salesforce.SalesforceConnector.build_connector(configuration()), boundary


def epoch(value: str = MODIFIED) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def queries(boundary: SalesforceTransport) -> list[str]:
    return [parse_qs(urlsplit(request.url or "").query)["q"][0] for request in boundary.requests if urlsplit(request.url or "").path.endswith("/query")]


def test_config_is_lazy_and_requests_keep_credentials_out_of_urls(connection: tuple[salesforce.SalesforceConnector, SalesforceTransport]) -> None:
    connector, boundary = connection
    assert not boundary.requests
    connector.validate_connector_settings()
    assert len(boundary.requests) == 2
    token, inventory = boundary.requests
    assert token.method == "POST" and token.headers.get("Authorization") is None
    assert parse_qs(str(token.body)) == {"grant_type": ["client_credentials"], "client_id": ["controlled-client"], "client_secret": [SECRET]}
    assert inventory.headers["Authorization"] == "Bearer " + TOKEN
    assert all(SECRET not in (request.url or "") and TOKEN not in (request.url or "") for request in boundary.requests)
    assert all(0 < option["timeout"][0] <= 5 and 0 < option["timeout"][1] <= 30 and option["stream"] is True for option in boundary.send_options)
    assert boundary.closed and all(response.raw.closed for response in boundary.responses)


@pytest.mark.parametrize("objects", [[], "", "   "])
def test_explicit_empty_scope_never_authenticates(monkeypatch: pytest.MonkeyPatch, objects: Any) -> None:
    boundary = SalesforceTransport()
    sessions = install_transport(monkeypatch, boundary)
    connector = salesforce.SalesforceConnector.build_connector(configuration(objects=objects))
    connector.validate_connector_settings()
    checkpoint = connector.build_dummy_checkpoint()
    assert list(connector.load_from_checkpoint(0.0, 0.0, checkpoint)) == []
    assert not checkpoint.has_more and checkpoint.cursors == {}
    assert list(connector.retrieve_all_slim_docs_perm_sync()) == []
    assert sessions == [] and boundary.requests == []


def test_defaults_csv_and_ordered_deduplication() -> None:
    config = configuration()
    config.pop("objects")
    defaults = salesforce.SalesforceConnector.build_connector(config).objects
    assert defaults == ["Account", "Contact", "Opportunity", "Case", "Knowledge__kav"]
    assert salesforce.SalesforceConnector.build_connector(configuration(objects=None)).objects == defaults
    assert salesforce.SalesforceConnector.build_connector(configuration(objects=" Contact, Account,Contact ")).objects == ["Contact", "Account"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("objects", True),
        ("objects", {}),
        ("objects", [1]),
        ("objects", [""]),
        ("objects", ["Account WHERE Id != ''"]),
        ("objects", "Account,,Case"),
        ("batch_size", 0),
        ("batch_size", True),
        ("batch_size", "1"),
        ("batch_size", 1001),
        ("api_version", None),
        ("api_version", "59.0"),
        ("api_version", "v59.0/query"),
        ("sync_deleted_files", True),
        ("sync_deleted_files", 0),
    ],
)
def test_invalid_config_never_constructs_session(monkeypatch: pytest.MonkeyPatch, field: str, value: Any) -> None:
    boundary = SalesforceTransport()
    sessions = install_transport(monkeypatch, boundary)
    with pytest.raises((ConnectorValidationError, ConnectorMissingCredentialError)):
        salesforce.SalesforceConnector.build_connector(configuration(**{field: value}))
    assert sessions == [] and boundary.requests == []


@pytest.mark.parametrize(
    "url",
    [
        "http://owned-org.my.salesforce.com",
        "https://127.0.0.1",
        "https://salesforce.com.evil.invalid",
        "https://salesforce.com",
        "https://owned-org.my.salesforce.com.",
        "https://user@owned-org.my.salesforce.com",
        "https://owned-org.my.salesforce.com:443",
        ORIGIN + "/",
        ORIGIN + "/services",
        ORIGIN + "?secret=value",
        ORIGIN + "#fragment",
        "https://OWNED-ORG.my.salesforce.com",
    ],
)
def test_untrusted_instance_url_fails_before_credentials_leave(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    boundary = SalesforceTransport()
    sessions = install_transport(monkeypatch, boundary)
    config = configuration()
    config["credentials"]["instance_url"] = url
    with pytest.raises(ConnectorValidationError):
        salesforce.SalesforceConnector.build_connector(config)
    assert sessions == [] and boundary.requests == []


@pytest.mark.parametrize("field", ["client_id", "client_secret"])
def test_missing_credentials_are_local_errors(field: str) -> None:
    config = configuration()
    config["credentials"].pop(field)
    with pytest.raises(ConnectorMissingCredentialError):
        salesforce.SalesforceConnector.build_connector(config)


@pytest.mark.parametrize("canonical", ["https://other-org.my.salesforce.com", "https://outside.invalid", ORIGIN + "/", ORIGIN + "?token=" + TOKEN, None])
def test_canonical_origin_cannot_redirect_the_access_token(connection: tuple[salesforce.SalesforceConnector, SalesforceTransport], canonical: Any) -> None:
    connector, boundary = connection
    boundary.canonical = canonical
    with pytest.raises(ConnectorValidationError):
        list(connector.load_from_state())
    assert len(boundary.requests) == 1 and boundary.requests[0].method == "POST"
    assert boundary.closed and all(response.raw.closed for response in boundary.responses)


@pytest.mark.parametrize("mode", ["token-redirect", "query-redirect", "cross-origin", "scheme-relative", "wrong-endpoint", "encoded-path"])
def test_redirects_and_continuations_never_send_tokens_outside_scope(connection: tuple[salesforce.SalesforceConnector, SalesforceTransport], mode: str) -> None:
    connector, boundary = connection
    boundary.mode = mode
    with pytest.raises(ConnectorValidationError):
        list(connector.load_from_state())
    assert all((request.url or "").startswith(ORIGIN + "/") for request in boundary.requests)
    assert sum("/query" in (request.url or "") for request in boundary.requests) <= 1
    assert boundary.closed and all(response.raw.closed for response in boundary.responses)


def test_complete_pages_have_stable_ids_metadata_and_deterministic_text(connection: tuple[salesforce.SalesforceConnector, SalesforceTransport]) -> None:
    connector, boundary = connection
    checkpoint = connector.build_dummy_checkpoint()
    output = connector.load_from_checkpoint(0.0, epoch(), checkpoint)
    first = next(output)
    assert checkpoint.has_more and checkpoint.cursors == {}
    documents = [first, *output]
    assert [document.id for document in documents] == ["Account/" + ACCOUNT_A, "Account/" + ACCOUNT_B]
    assert all(
        document.source == "salesforce" and document.extension == ".txt" and document.size_bytes == len(document.blob) and document.doc_updated_at == datetime(2026, 1, 1, tzinfo=UTC)
        for document in documents
    )
    assert first.metadata == {"object": "Account", "record_id": ACCOUNT_A, "web_url": ORIGIN + "/" + ACCOUNT_A}
    assert b"Number: 0" in first.blob and b"Flag: False" in first.blob and b'Nested: {"a": 2, "z": 1}' in first.blob
    assert b"attributes" not in first.blob and b"Empty:" not in first.blob
    query = queries(boundary)[0]
    assert "BillingAddress" not in query and "ORDER BY SystemModstamp ASC, Id ASC" in query
    assert not checkpoint.has_more and checkpoint.cursors == {"Account": "2026-01-01T00:00:00+00:00"}
    assert connector.validate_checkpoint_json(checkpoint.model_dump_json()) == checkpoint
    assert boundary.closed and all(response.raw.closed for response in boundary.responses)


def test_slim_inventory_uses_the_same_scoped_identities(connection: tuple[salesforce.SalesforceConnector, SalesforceTransport]) -> None:
    connector, boundary = connection
    seen: list[tuple[str, str]] = []
    documents = [document for batch in connector.retrieve_all_slim_docs_perm_sync(lambda identity, obj: seen.append((identity, obj))) for document in batch]
    assert [document.id for document in documents] == ["Account/" + ACCOUNT_A, "Account/" + ACCOUNT_B]
    assert seen == [(document.id, "Account") for document in documents]
    assert queries(boundary)[0].startswith("SELECT Id,SystemModstamp FROM Account ORDER BY")
    assert boundary.closed


@pytest.mark.parametrize(
    "mode", ["later-page", "repeat", "missing-next", "missing-done", "bad-records", "wrong-total", "malformed-json", "done-with-next", "inventory-error", "token-error", "request-error"]
)
def test_failed_pages_never_publish_checkpoint_or_secret_trace(connection: tuple[salesforce.SalesforceConnector, SalesforceTransport], mode: str, caplog: pytest.LogCaptureFixture) -> None:
    connector, boundary = connection
    boundary.mode = mode
    checkpoint = connector.build_dummy_checkpoint()
    with pytest.raises(ConnectorValidationError) as error:
        list(connector.load_from_checkpoint(0.0, epoch(), checkpoint))
    assert checkpoint.has_more and checkpoint.cursors == {}
    rendered = "".join(traceback.format_exception(error.value)) + caplog.text
    assert SECRET not in rendered and TOKEN not in rendered
    assert boundary.closed and all(response.raw.closed for response in boundary.responses)


@pytest.mark.parametrize(
    "record",
    [
        {"SystemModstamp": MODIFIED},
        {"Id": "../outside", "SystemModstamp": MODIFIED},
        {"Id": ACCOUNT_A},
        {"Id": ACCOUNT_A, "SystemModstamp": "yesterday"},
        {"Id": ACCOUNT_A, "SystemModstamp": "2026-01-01T00:00:00"},
        {"Id": ACCOUNT_A, "SystemModstamp": "2026-01-02T00:00:00Z"},
    ],
)
def test_invalid_record_versions_fail_without_advancing_state(connection: tuple[salesforce.SalesforceConnector, SalesforceTransport], record: dict[str, Any]) -> None:
    connector, boundary = connection
    boundary.records["Account"] = [record]
    checkpoint = connector.build_dummy_checkpoint()
    with pytest.raises(ConnectorValidationError):
        list(connector.load_from_checkpoint(0.0, epoch(), checkpoint))
    assert checkpoint.has_more and checkpoint.cursors == {} and boundary.closed


def test_duplicate_record_across_pages_fails_checkpoint(connection: tuple[salesforce.SalesforceConnector, SalesforceTransport]) -> None:
    connector, boundary = connection
    boundary.records["Account"][1]["Id"] = ACCOUNT_A
    checkpoint = connector.build_dummy_checkpoint()
    with pytest.raises(ConnectorValidationError):
        list(connector.load_from_checkpoint(0.0, epoch(), checkpoint))
    assert checkpoint.has_more and checkpoint.cursors == {} and boundary.closed


def test_zero_window_and_same_timestamp_boundary_are_not_dropped(connection: tuple[salesforce.SalesforceConnector, SalesforceTransport]) -> None:
    connector, boundary = connection
    boundary.records["Account"] = [{"Id": ACCOUNT_A, "SystemModstamp": "1970-01-01T00:00:00Z"}]
    documents = [document for batch in connector.poll_source(0.0, 0.0) for document in batch]
    assert [document.id for document in documents] == ["Account/" + ACCOUNT_A]
    query = queries(boundary)[0]
    assert "SystemModstamp >= 1970-01-01T00:00:00Z" in query
    assert "SystemModstamp <= 1970-01-01T00:00:00Z" in query
    boundary.records["Account"] = [{"Id": ACCOUNT_A, "SystemModstamp": MODIFIED}, {"Id": ACCOUNT_B, "SystemModstamp": MODIFIED}]
    replay = [document for batch in connector.poll_source(epoch(), epoch()) for document in batch]
    assert [document.id for document in replay] == ["Account/" + ACCOUNT_A, "Account/" + ACCOUNT_B]
    assert "SystemModstamp >= 2026-01-01T00:00:00Z" in queries(boundary)[-1]


def test_fractional_window_uses_whole_second_soql_and_counts_complete_superset(connection: tuple[salesforce.SalesforceConnector, SalesforceTransport]) -> None:
    connector, boundary = connection
    boundary.records["Account"] = [
        {"Id": ACCOUNT_A, "SystemModstamp": "2026-01-01T00:00:00.200Z"},
        {"Id": ACCOUNT_B, "SystemModstamp": "2026-01-01T00:00:00.500Z"},
        {"Id": "001000000000003", "SystemModstamp": "2026-01-01T00:00:00.900Z"},
    ]
    checkpoint = connector.build_dummy_checkpoint()
    documents = list(connector.load_from_checkpoint(epoch() + 0.5, epoch() + 0.75, checkpoint))
    assert [document.id for document in documents] == ["Account/" + ACCOUNT_B]
    assert checkpoint.has_more is False and checkpoint.cursors == {"Account": "2026-01-01T00:00:00.500000+00:00"}
    assert "SystemModstamp >= 2026-01-01T00:00:00Z" in queries(boundary)[0]
    assert "SystemModstamp <= 2026-01-01T00:00:01Z" in queries(boundary)[0]
    assert sum("/query/Account-" in (request.url or "") for request in boundary.requests) == 2


def test_malformed_continuation_never_leaks_url_or_token(connection: tuple[salesforce.SalesforceConnector, SalesforceTransport]) -> None:
    connector, boundary = connection
    boundary.mode = "malformed-host"
    with pytest.raises(ConnectorValidationError) as error:
        list(connector.load_from_state())
    assert TOKEN not in "".join(traceback.format_exception(error.value))
    assert len(boundary.requests) == 4


def test_only_omitted_default_knowledge_can_be_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    boundary = SalesforceTransport()
    install_transport(monkeypatch, boundary)
    config = configuration()
    config.pop("objects")
    connector = salesforce.SalesforceConnector.build_connector(config)
    documents = [document for batch in connector.load_from_state() for document in batch]
    assert len(documents) == 2
    assert not any("Knowledge__kav/describe" in (request.url or "") for request in boundary.requests)
    explicit = salesforce.SalesforceConnector.build_connector(configuration(objects=["Knowledge__kav"]))
    with pytest.raises(ConnectorValidationError):
        list(explicit.load_from_state())
    boundary.available["Knowledge__kav"] = False
    with pytest.raises(ConnectorValidationError):
        list(connector.load_from_state())


@pytest.mark.parametrize("status", [403, 404, 429, 500])
def test_optional_knowledge_transient_or_permission_failure_is_not_skipped(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    boundary = SalesforceTransport()
    boundary.available["Knowledge__kav"] = True
    boundary.describe_status["Knowledge__kav"] = status
    install_transport(monkeypatch, boundary)
    config = configuration()
    config.pop("objects")
    connector = salesforce.SalesforceConnector.build_connector(config)
    checkpoint = connector.build_dummy_checkpoint()
    with pytest.raises(ConnectorValidationError):
        list(connector.load_from_checkpoint(0.0, epoch(), checkpoint))
    assert checkpoint.has_more and checkpoint.cursors == {} and boundary.closed


def test_checkpoint_validation_does_not_silently_restart_invalid_state(connection: tuple[salesforce.SalesforceConnector, SalesforceTransport]) -> None:
    connector, boundary = connection
    for payload in [{"cursors": {"Case": MODIFIED}}, {"cursors": {"Account": "invalid"}}]:
        with pytest.raises(ConnectorValidationError):
            connector.validate_checkpoint_json(json.dumps(payload))
    with pytest.raises(ConnectorValidationError):
        list(connector.load_from_checkpoint(0.0, epoch(), ConnectorCheckpoint()))
    assert boundary.requests == []


def test_abandoned_generator_closes_transport_without_completing_state(connection: tuple[salesforce.SalesforceConnector, SalesforceTransport]) -> None:
    connector, boundary = connection
    checkpoint = connector.build_dummy_checkpoint()
    output = connector.load_from_checkpoint(0.0, epoch(), checkpoint)
    next(output)
    output.close()
    assert checkpoint.has_more and checkpoint.cursors == {} and boundary.closed
    assert all(response.raw.closed for response in boundary.responses)
