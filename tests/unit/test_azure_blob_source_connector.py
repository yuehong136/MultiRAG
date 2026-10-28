"""Azure source contracts through the installed SDK and an owned HTTP transport."""

from __future__ import annotations

import traceback
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
import xxhash
from azure.storage.blob import ContainerClient

from common.data_source import azure_blob_connector as azure
from common.data_source.exceptions import ConnectorMissingCredentialError, ConnectorValidationError
from common.data_source.interfaces import CheckpointOutputWrapper
from tests.support.azure_blob_source_transport import BlobSourceTransport, BlobVersion

ACCOUNT_KEY = "Y29udHJvbGxlZC1rZXk="
SECRET = "synthetic-sas-secret"


def configuration(mode: str = "account_key", **options: Any) -> dict[str, Any]:
    return {
        "auth_mode": mode,
        "prefix": "docs/",
        "batch_size": 1,
        "credentials": {
            "account_name": "ownedaccount",
            "account_key": ACCOUNT_KEY,
            "container_name": "container",
            "connection_string": f"DefaultEndpointsProtocol=https;AccountName=ownedaccount;AccountKey={ACCOUNT_KEY};EndpointSuffix=core.windows.net",
            "container_url": "https://ownedaccount.blob.core.windows.net/container",
            "sas_token": f"?sp=rl&sig={SECRET}",
        },
        **options,
    }


def connect(
    monkeypatch: pytest.MonkeyPatch, config: dict[str, Any] | None = None, transport: BlobSourceTransport | None = None
) -> tuple[azure.AzureBlobConnector, BlobSourceTransport, list[dict[str, Any]]]:
    boundary = transport if transport is not None else BlobSourceTransport()
    calls: list[dict[str, Any]] = []

    def client(*args: Any, **kwargs: Any) -> ContainerClient:
        calls.append(dict(kwargs))
        # Keep the production constructor and policies; retries only slow
        # deterministic permission failures and are disabled at this seam.
        return ContainerClient(*args, **{**kwargs, "transport": boundary, "retry_total": 0})

    monkeypatch.setattr(azure, "ContainerClient", client)
    return azure.AzureBlobConnector.build_connector(config or configuration()), boundary, calls


@pytest.mark.parametrize("mode", ["account_key", "connection_string", "sas_token"])
def test_selected_auth_is_lazy_and_ignores_inactive_credentials(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    config = configuration(mode)
    inactive = {
        "account_key": ["connection_string", "container_url", "sas_token"],
        "connection_string": ["account_name", "account_key", "container_url", "sas_token"],
        "sas_token": ["account_name", "account_key", "container_name", "connection_string"],
    }
    for field in inactive[mode]:
        config["credentials"][field] = "stale-ignored-value"
    connector, transport, calls = connect(monkeypatch, config)
    assert calls == [] and transport.requests == []
    connector.validate_connector_settings()
    assert len(calls) == 1 and transport.closed
    assert calls[0]["permit_redirects"] is False
    assert calls[0]["retry_total"] == 2
    assert calls[0]["connection_timeout"] == 5 and calls[0]["read_timeout"] == 15
    request = transport.requests[0]
    assert urlsplit(request.url).hostname == "ownedaccount.blob.core.windows.net"
    assert (parse_qs(urlsplit(request.url).query).get("sig") == [SECRET]) == (mode == "sas_token")
    assert ("Authorization" in request.headers) == (mode != "sas_token")


@pytest.mark.parametrize(
    "field,value",
    [
        ("batch_size", 0),
        ("batch_size", -1),
        ("batch_size", True),
        ("batch_size", "1"),
        ("batch_size", 1001),
        ("allow_images", "false"),
        ("allow_images", 0),
        ("prefix", None),
        ("auth_mode", None),
        ("auth_mode", "automatic"),
        ("sync_deleted_files", True),
        ("sync_deleted_files", 0),
    ],
)
def test_invalid_config_fails_before_client_creation(monkeypatch: pytest.MonkeyPatch, field: str, value: Any) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> ContainerClient:
        raise AssertionError("invalid config must not construct a network client")

    monkeypatch.setattr(azure, "ContainerClient", forbidden)
    with pytest.raises((ConnectorValidationError, ConnectorMissingCredentialError)):
        azure.AzureBlobConnector.build_connector(configuration(**{field: value}))


@pytest.mark.parametrize(
    "mode,field", [("account_key", "account_key"), ("account_key", "container_name"), ("connection_string", "connection_string"), ("sas_token", "container_url"), ("sas_token", "sas_token")]
)
def test_selected_mode_requires_its_own_credentials(mode: str, field: str) -> None:
    config = configuration(mode)
    config["credentials"].pop(field)
    with pytest.raises(ConnectorMissingCredentialError):
        azure.AzureBlobConnector.build_connector(config)


@pytest.mark.parametrize(
    "url",
    [
        "http://ownedaccount.blob.core.windows.net/container",
        "https://127.0.0.1/container",
        "https://outside.invalid/container",
        "https://ownedaccount.blob.core.windows.net.evil.invalid/container",
        "https://user@ownedaccount.blob.core.windows.net/container",
        "https://ownedaccount.blob.core.windows.net:443/container",
        "https://ownedaccount.blob.core.windows.net/container?sig=secret",
        "https://ownedaccount.blob.core.windows.net/container#fragment",
        "https://ownedaccount.blob.core.windows.net/container/nested",
        "https://ownedaccount.blob.core.windows.net/%63ontainer",
    ],
)
def test_untrusted_sas_container_url_is_rejected_without_requests(url: str) -> None:
    config = configuration("sas_token")
    config["credentials"]["container_url"] = url
    with pytest.raises(ConnectorValidationError):
        azure.AzureBlobConnector.build_connector(config)


@pytest.mark.parametrize("suffix", ["core.windows.net", "core.chinacloudapi.cn", "core.usgovcloudapi.net"])
def test_official_azure_cloud_endpoints_are_supported(monkeypatch: pytest.MonkeyPatch, suffix: str) -> None:
    config = configuration("sas_token")
    config["credentials"]["container_url"] = f"https://ownedaccount.blob.{suffix}/container"
    connector, transport, _ = connect(monkeypatch, config)
    connector.validate_connector_settings()
    assert urlsplit(transport.requests[0].url).hostname == f"ownedaccount.blob.{suffix}"
    assert transport.closed


@pytest.mark.parametrize(
    "extra",
    [
        "BlobEndpoint=https://outside.invalid",
        "BlobEndpoint=https://ownedaccount.blob.core.windows.net/container",
        "EndpointSuffix=outside.invalid",
        "DefaultEndpointsProtocol=http",
        "BlobSecondaryEndpoint=https://outside.invalid",
        "AccountName=anotheraccount",
    ],
)
def test_connection_string_endpoint_escape_and_duplicates_fail_closed(extra: str) -> None:
    config = configuration("connection_string")
    config["credentials"]["connection_string"] += ";" + extra
    with pytest.raises(ConnectorValidationError):
        azure.AzureBlobConnector.build_connector(config)


def test_complete_paged_inventory_is_metadata_only_and_fetch_is_conditioned(monkeypatch: pytest.MonkeyPatch) -> None:
    connector, transport, _ = connect(monkeypatch)
    with connector.session():
        records = list(connector.list_keys())
        assert [record.key for record in records] == ["docs/a.txt", "docs/b.txt"]
        assert len(transport.requests) == 2
        assert [parse_qs(urlsplit(request.url).query).get("marker", [""])[0] for request in transport.requests] == ["", "1"]
        document = connector.get_value(records[0].key)
        assert document is not None and document.blob == b"first"
        assert document.id == "docs/a.txt" and document.source == "azure_blob"
        assert document.doc_updated_at == datetime(2026, 1, 1, tzinfo=UTC)
        assert document.metadata == {"container": "container", "prefix": "docs/", "etag": '"version-one"'}
        assert document.fingerprint == records[0].fingerprint == xxhash.xxh128(b"https://ownedaccount.blob.core.windows.net\0container\0version-one").hexdigest()
        assert transport.requests[-1].headers["If-Match"] == '"version-one"'
    assert transport.closed and transport.close_count == 1


def test_later_page_failure_publishes_nothing_and_does_not_complete_checkpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = BlobSourceTransport()
    transport.fail_list_page = 2
    connector, transport, _ = connect(monkeypatch, transport=transport)
    checkpoint = connector.build_dummy_checkpoint()
    documents = connector.load_from_checkpoint(0, 4102444800, checkpoint)
    with pytest.raises(ConnectorValidationError, match="inventory failed"):
        next(documents)
    assert checkpoint.has_more and transport.closed
    assert len(transport.requests) == 2
    with pytest.raises(ConnectorValidationError, match="complete inventory"):
        connector.get_value("docs/a.txt")


@pytest.mark.parametrize("failure", ["race", "permission", "missing"])
def test_failed_get_never_yields_stale_version_or_secret_trace(monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    transport = BlobSourceTransport()
    connector, transport, _ = connect(monkeypatch, configuration("sas_token"), transport)
    with connector.session():
        [record, _] = list(connector.list_keys())
        if failure == "race":
            transport.race_key = record.key
        elif failure == "permission":
            transport.fail_get = record.key
        else:
            del transport.objects[record.key]
        with pytest.raises(ConnectorValidationError, match="retry synchronization") as captured:
            connector.get_value(record.key)
        rendered = "".join(traceback.format_exception(captured.value))
        assert SECRET not in rendered and "AccountKey=" not in rendered and "sig=" not in rendered
        assert captured.value.__suppress_context__
    assert transport.closed


def test_external_redirect_is_not_followed_and_client_closes(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = BlobSourceTransport()
    transport.redirect_list = True
    connector, transport, _ = connect(monkeypatch, configuration("sas_token"), transport)
    with pytest.raises(ConnectorValidationError) as captured:
        list(connector.load_from_state())
    assert len(transport.requests) == 1 and transport.closed
    assert SECRET not in "".join(traceback.format_exception(captured.value))


@pytest.mark.parametrize("prefix,expected", [("docs/", ["docs/a.txt"]), ("docs", ["docs/a.txt", "docs2/b.txt"]), ("/docs/", ["/docs/c.txt"]), ("", ["docs/a.txt", "docs2/b.txt", "/docs/c.txt"])])
def test_literal_prefix_and_full_slim_identity_match(monkeypatch: pytest.MonkeyPatch, prefix: str, expected: list[str]) -> None:
    transport = BlobSourceTransport({name: BlobVersion(name.encode()) for name in ["docs/a.txt", "docs2/b.txt", "/docs/c.txt", "docs/ignored.exe", "docs/folder/"]})
    connector, transport, _ = connect(monkeypatch, configuration(prefix=prefix), transport)
    full = [document.id for batch in connector.load_from_state() for document in batch]
    assert full == expected and transport.closed
    slim = [document.id for batch in connector.retrieve_all_slim_docs_perm_sync() for document in batch]
    assert slim == full and transport.closed
    listing = [request for request in transport.requests if parse_qs(urlsplit(request.url).query).get("comp") == ["list"]]
    assert all(parse_qs(urlsplit(request.url).query, keep_blank_values=True).get("prefix", [""])[0] == prefix for request in listing)


@pytest.mark.parametrize("field", ["Etag", "Last-Modified", "Content-Length"])
def test_incomplete_listing_metadata_does_not_publish_partial_inventory(monkeypatch: pytest.MonkeyPatch, field: str) -> None:
    transport = BlobSourceTransport()
    transport.omit_listing_field = field
    connector, transport, _ = connect(monkeypatch, transport=transport)
    with pytest.raises(ConnectorValidationError):
        list(connector.load_from_state())
    assert transport.closed


@pytest.mark.parametrize("names", [["outside/a.txt"], ["docs/a.txt", "docs/a.txt"]])
def test_untrusted_inventory_scope_and_duplicate_names_fail_before_yield(monkeypatch: pytest.MonkeyPatch, names: list[str]) -> None:
    transport = BlobSourceTransport({name: BlobVersion(b"owned") for name in names})
    transport.list_names = names
    connector, transport, _ = connect(monkeypatch, transport=transport)
    with pytest.raises(ConnectorValidationError, match="inventory failed"):
        next(connector.load_from_state())
    assert transport.closed


def test_size_exclusion_preserves_slim_inventory_and_empty_blob_is_legal(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = BlobSourceTransport({"docs/empty.txt": BlobVersion(b""), "docs/large.txt": BlobVersion(b"too large")})
    connector, transport, _ = connect(monkeypatch, transport=transport)
    monkeypatch.setattr(azure, "BLOB_STORAGE_SIZE_THRESHOLD", 5)
    documents = [document for batch in connector.load_from_state() for document in batch]
    assert len(documents) == 1 and documents[0].id == "docs/empty.txt" and documents[0].blob == b"" and documents[0].size_bytes == 0
    assert [document.id for batch in connector.retrieve_all_slim_docs_perm_sync() for document in batch] == ["docs/empty.txt", "docs/large.txt"]
    assert transport.closed


def test_early_consumer_close_releases_root_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    connector, transport, _ = connect(monkeypatch)
    generator = connector.load_from_state()
    assert next(generator)[0].id == "docs/a.txt" and not transport.closed
    generator.close()
    assert transport.closed and transport.close_count == 1


def test_epoch_zero_is_an_explicit_upper_bound_and_empty_inventory_completes(monkeypatch: pytest.MonkeyPatch) -> None:
    connector, transport, _ = connect(monkeypatch)
    assert list(connector.poll_source(0, 0)) == []
    assert transport.closed and len(transport.requests) == 2
    transport.objects.clear()
    checkpoint = connector.build_dummy_checkpoint()
    assert list(connector.load_from_checkpoint(0, 4102444800, checkpoint)) == []
    assert checkpoint.has_more is False and transport.closed


def test_failed_credential_reload_cannot_reuse_the_old_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    connector, transport, _ = connect(monkeypatch)
    connector.validate_connector_settings()
    with pytest.raises(ConnectorMissingCredentialError):
        connector.load_credentials({})
    before = len(transport.requests)
    with pytest.raises(ConnectorValidationError):
        list(connector.load_from_state())
    assert len(transport.requests) == before and transport.closed


def test_checkpoint_documents_follow_the_actual_wrapper_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    connector, transport, _ = connect(monkeypatch)
    checkpoint = connector.build_dummy_checkpoint()
    wrapped = list(CheckpointOutputWrapper()(connector.load_from_checkpoint(0, 4102444800, checkpoint)))
    assert [document.id for document, failure, marker in wrapped if document is not None] == ["docs/a.txt", "docs/b.txt"]
    assert wrapped[-1] == (None, None, checkpoint) and checkpoint.has_more is False
    assert transport.closed


def test_repeated_marker_on_empty_pages_is_not_a_complete_inventory(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = BlobSourceTransport({})
    transport.forced_next_marker = "1"
    connector, transport, _ = connect(monkeypatch, transport=transport)
    with pytest.raises(ConnectorValidationError, match="inventory failed"):
        list(connector.load_from_state())
    assert len(transport.requests) == 2 and transport.closed


def test_inventory_cancellation_queries_are_bounded_by_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = BlobSourceTransport({f"docs/{number}.txt": BlobVersion(b"text") for number in range(100)}, page_size=50)
    connector, transport, _ = connect(monkeypatch, transport=transport)
    guards: list[None] = []
    # The worker guard checks persistent SQL cancellation. Inventory polling
    # must scale with network pages rather than incur one DB round trip per key.
    connector.check_active = lambda: guards.append(None)
    with connector.session():
        assert len(list(connector.list_keys())) == 100
    assert len(guards) <= 3 and transport.closed


@pytest.mark.parametrize("name", ["../outside/secret.txt", "docs/../../outside/secret.txt", "./docs/a.txt", "docs/./a.txt"])
def test_dot_segments_cannot_escape_container_or_local_storage_prefix(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    transport = BlobSourceTransport({name: BlobVersion(b"forbidden")})
    connector, transport, _ = connect(monkeypatch, configuration(prefix=""), transport)
    with pytest.raises(ConnectorValidationError, match="inventory failed"):
        list(connector.load_from_state())
    assert transport.closed
    assert all(parse_qs(urlsplit(request.url).query).get("comp") == ["list"] for request in transport.requests)


@pytest.mark.parametrize("field,value", [("container_name", "other-container"), ("account_name", "otheraccount")])
def test_same_etag_in_another_source_scope_requires_refetch(monkeypatch: pytest.MonkeyPatch, field: str, value: str) -> None:
    connector, transport, _ = connect(monkeypatch)
    with connector.session():
        original = next(connector.list_keys()).fingerprint
    config = configuration()
    config["credentials"][field] = value
    replacement, _, _ = connect(monkeypatch, config, transport)
    with replacement.session():
        other = next(replacement.list_keys()).fingerprint
    assert original != other, "ETags are resource-scoped: editing account/container must invalidate the old source version"
