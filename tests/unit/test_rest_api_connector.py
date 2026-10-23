"""REST document mapping, strict pagination and outbound request boundaries."""

from datetime import UTC, datetime
from typing import Any

import pytest
import requests

from common.data_source import rest_api_connector as module
from common.data_source.exceptions import ConnectorMissingCredentialError, ConnectorValidationError
from common.data_source.rest_api_connector import RestAPIConnector


def connector(**kwargs: Any) -> RestAPIConnector:
    return RestAPIConnector(url="https://api.example.test/items", content_fields=["title", "body"], request_delay=0, **kwargs)


def feed(monkeypatch: pytest.MonkeyPatch, driver: RestAPIConnector, pages: list[Any]) -> list[dict[str, Any]]:
    calls = []
    values = iter(pages)

    def fetch(params: dict[str, Any]) -> Any:
        calls.append(dict(params))
        value = next(values)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(driver, "_fetch_page", fetch)
    return calls


def test_ui_schema_and_stable_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = RestAPIConnector.parse_storage_config(
        {
            "url": "https://api.example.test",
            "content_fields": "title, country.name, tags[*].name, missing",
            "metadata_fields": "count,country.name",
            "headers": "X-Version=1",
            "query_params": '{"q":"a=b"}',
            "request_body": "",
            "field_type_hints": {"count": "number"},
            "field_default_values": {"missing": "fallback"},
            "id_field": "id",
        }
    )
    driver = RestAPIConnector.from_parsed_config(cfg)
    item = {"id": 0, "title": "<p>Hello</p>", "country": {"name": "CN"}, "tags": [{"name": "A"}, {"name": "B"}], "count": "3"}
    doc = driver._item_to_document(item)
    assert doc.blob == b"Hello\nCN\nA, B\nfallback"
    assert doc.metadata == {"count": 3, "country.name": "CN"}
    assert doc.size_bytes == len(doc.blob) and doc.source == "rest_api"
    assert driver._item_to_document(dict(reversed(list(item.items())))).id == doc.id
    assert driver._item_to_document({**item, "count": "4"}).fingerprint != doc.fingerprint
    assert cfg.request_body == {} and cfg.query_params == {"q": "a=b"} and cfg.headers == {"X-Version": "1"}


@pytest.mark.parametrize(
    "change",
    [
        {"content_fields": []},
        {"request_body": "[]"},
        {"headers": "broken line"},
        {"method": "DELETE"},
        {"max_pages": 0},
        {"request_delay": float("nan")},
        {"batch_size": -1},
        {"url": "https://user:secret@example.test"},
        {"content_fields": ["broken["]},
        {"pagination_type": "cursor"},
    ],
)
def test_bad_config_is_explicit_and_redacted(change: dict[str, Any]) -> None:
    with pytest.raises(ConnectorValidationError, match="Invalid REST API configuration") as error:
        RestAPIConnector.parse_storage_config({"url": "https://example.test", "content_fields": ["title"], **change})
    assert "secret" not in str(error.value)


@pytest.mark.parametrize(
    ("auth_type", "values", "headers", "basic"),
    [
        ("none", {}, {}, None),
        ("bearer", {"token": "token"}, {"Authorization": "Bearer token"}, None),
        ("api_key_header", {"header_name": "X-Custom", "api_key_value": "key"}, {"X-Custom": "key"}, None),
        ("basic", {"username": "u", "password": "p"}, {}, ("u", "p")),
    ],
)
def test_authentication(auth_type: str, values: dict[str, str], headers: dict[str, str], basic: tuple[str, str] | None) -> None:
    driver = connector(auth_type=auth_type, auth_config=values)
    driver.load_credentials({})
    assert driver._auth_headers == headers and driver._basic_auth == basic
    if auth_type != "none":
        with pytest.raises(ConnectorMissingCredentialError):
            connector(auth_type=auth_type).load_credentials({})


@pytest.mark.parametrize("pagination", ["page", "offset", "cursor"])
def test_pagination_continues_empty_explicit_page(monkeypatch: pytest.MonkeyPatch, pagination: str) -> None:
    driver = connector(pagination_type=pagination, pagination_config={"page_size": 1, "page_size_param": "size", "limit_param": "limit", "next_cursor_field": "next", "has_next_page_path": "$.more"})
    calls = feed(monkeypatch, driver, [{"items": [], "next": 0, "more": True}, {"items": [{"title": "a"}], "next": None, "more": False}])
    assert [doc.blob for batch in driver.load_from_state() for doc in batch] == [b"a"]
    assert len(calls) == 2
    if pagination == "cursor":
        assert calls == [{}, {"cursor": "0"}]
    elif pagination == "page":
        assert calls == [{"page": 1, "size": 1}, {"page": 2, "size": 1}]
    else:
        assert calls == [{"offset": 0, "limit": 1}, {"offset": 1, "limit": 1}]


@pytest.mark.parametrize("case", ["cycle", "cap", "malformed", "mapping", "later-page", "missing-items"])
def test_partial_or_malformed_sync_fails(monkeypatch: pytest.MonkeyPatch, case: str) -> None:
    driver = connector(pagination_type="cursor", pagination_config={"next_cursor_field": "next"}, batch_size=1, max_pages=2)
    good = {"items": [{"title": "ok"}], "next": "a"}
    values = {
        "cycle": [good, {**good, "next": "a"}],
        "cap": [good, {**good, "next": "b"}],
        "malformed": [{"items": [1]}],
        "mapping": [{"items": [{"unexpected": "bad"}]}],
        "later-page": [good, ConnectorValidationError("page failed")],
        "missing-items": [{"error": "not an item array"}],
    }
    feed(monkeypatch, driver, values[case])
    with pytest.raises(ConnectorValidationError):
        list(driver.load_from_state())


def test_jsonpath_empty_and_timestamp_window(monkeypatch: pytest.MonkeyPatch) -> None:
    driver = connector(items_path="$.payload[*]", id_field="id", poll_timestamp_field="updated", content_template="{title}: {body}")
    assert driver._extract_items({"payload": []}) == []
    feed(monkeypatch, driver, [{"payload": [{"id": 1, "title": "old", "body": "a", "updated": "2026-01-01"}, {"id": 2, "title": "current", "body": "b", "updated": "2026-01-02T08:00:00+08:00"}]}])
    docs = [doc for batch in driver.poll_source(datetime(2026, 1, 2, tzinfo=UTC).timestamp(), datetime(2026, 1, 3, tzinfo=UTC).timestamp()) for doc in batch]
    assert len(docs) == 1 and docs[0].blob == b"current: b" and docs[0].doc_updated_at == datetime(2026, 1, 2, tzinfo=UTC)
    with pytest.raises(ConnectorValidationError):
        driver._item_to_document({"id": 1, "title": "bad", "updated": "broken"})


class FakeResponse:
    def __init__(self, status: int, location: str = "") -> None:
        self.status_code, self.headers = status, {"Location": location}
        self.closed = False

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: Any) -> None:
        self.closed = True

    def iter_content(self, size: int) -> list[bytes]:
        return [b'[{"title":"ok"}]']


def network(monkeypatch: pytest.MonkeyPatch, responses: list[FakeResponse]) -> tuple[list[Any], list[Any]]:
    requests_seen, guards = [], []
    values = iter(responses)

    def safe(url: str) -> tuple[str, str]:
        guards.append(url)
        if "private" in url:
            raise ValueError("blocked")
        return "example.test", "8.8.8.8"

    def request(self: requests.Session, method: str, url: str, **kwargs: Any) -> FakeResponse:
        assert self.trust_env is False
        requests_seen.append((method, url, kwargs))
        return next(values)

    monkeypatch.setattr(module, "assert_url_is_safe", safe)
    monkeypatch.setattr(requests.Session, "request", request)
    return requests_seen, guards


def test_redirect_ssrf_and_cross_origin_strip(monkeypatch: pytest.MonkeyPatch) -> None:
    first, final = FakeResponse(302, "https://other.example.test/next"), FakeResponse(200)
    calls, guards = network(monkeypatch, [first, final])
    driver = connector(headers={"X-Secret": "secret"}, query_params={"api_key": "key"}, auth_type="bearer", auth_config={"token": "token"})
    driver.load_credentials({})
    driver._fetch_page({})
    assert calls[1][2]["headers"] == {} and calls[1][2]["params"] == {} and calls[1][2]["auth"] is None
    assert len(guards) == 2 and first.closed and final.closed
    calls, guards = network(monkeypatch, [FakeResponse(302, "http://private/secret")])
    with pytest.raises(ConnectorValidationError):
        driver._fetch_page({})
    assert len(calls) == 1 and len(guards) == 2


@pytest.mark.parametrize("status", [401, 403, 404, 429, 503])
def test_status_retries_are_bounded_and_safe(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    calls, _ = network(monkeypatch, [FakeResponse(status) for _ in range(3)])
    monkeypatch.setattr(module.time, "sleep", lambda value: None)
    with pytest.raises((ConnectorValidationError, ConnectorMissingCredentialError)) as error:
        connector()._fetch_page({})
    assert len(calls) == (3 if status in {429, 503} else 1)
    assert "example.test" not in str(error.value)


def test_cursor_final_flag_does_not_require_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    driver = connector(pagination_type="cursor", pagination_config={"initial_cursor": "", "next_cursor_field": "next", "has_next_page_field": "more"})
    calls = feed(monkeypatch, driver, [{"items": [], "next": 0, "more": True}, {"items": [{"title": "last"}], "more": False}])
    assert [doc.blob for batch in driver.load_from_state() for doc in batch] == [b"last"]
    assert calls == [{}, {"cursor": "0"}]


def test_malformed_redirect_redacts_vendor_value(monkeypatch: pytest.MonkeyPatch) -> None:
    response = FakeResponse(302, "https://other.example.test:vendor-secret/path")
    calls, _ = network(monkeypatch, [response])
    with pytest.raises(ConnectorValidationError, match="redirect URL is malformed") as error:
        connector()._fetch_page({})
    assert "vendor-secret" not in str(error.value) and len(calls) == 1 and response.closed


def test_idna_redirect_is_canonicalized_before_guard_and_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    import socket
    from urllib.parse import urlsplit

    driver = connector()
    guarded = []
    connected = []
    responses = iter([FakeResponse(302, "https://bücher.example/items"), FakeResponse(200)])

    def safe(url: str) -> tuple[str, str]:
        host = urlsplit(url).hostname
        assert host is not None and host.isascii()
        guarded.append(host)
        return host, "8.8.8.8"

    def request(self: requests.Session, method: str, url: str, **kwargs: Any) -> FakeResponse:
        host = urlsplit(url).hostname
        assert host is not None
        # Any unpinned second DNS resolution would hit a fake/reserved domain.
        assert socket.getaddrinfo(host, 443)[0][4][0] == "8.8.8.8"
        connected.append(host)
        return next(responses)

    monkeypatch.setattr(module, "assert_url_is_safe", safe)
    monkeypatch.setattr(requests.Session, "request", request)
    driver._fetch_page({})
    assert guarded == connected == ["api.example.test", "xn--bcher-kva.example"]
