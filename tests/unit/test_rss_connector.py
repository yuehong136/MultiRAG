import importlib
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

import common


@pytest.fixture
def rss_module(monkeypatch):
    module_names = [
        "common.data_source.config",
        "common.data_source.interfaces",
        "common.data_source.models",
        "common.data_source.rss_connector",
    ]
    missing = object()
    saved_modules = {name: sys.modules.get(name, missing) for name in module_names}
    for name in module_names:
        sys.modules.pop(name, None)

    repo_root = Path(__file__).resolve().parents[2]
    data_source_pkg = ModuleType("common.data_source")
    data_source_pkg.__path__ = [str(repo_root / "common" / "data_source")]
    monkeypatch.setitem(sys.modules, "common.data_source", data_source_pkg)
    monkeypatch.setattr(common, "data_source", data_source_pkg, raising=False)

    module = importlib.import_module("common.data_source.rss_connector")
    monkeypatch.setattr(module.socket, "gethostbyname", lambda _hostname: "93.184.216.34")
    yield module

    for name, saved_module in saved_modules.items():
        if saved_module is missing:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = saved_module


class _FakeResponse:
    def __init__(self, content: bytes = b"feed", url: str = "https://example.com/feed.xml") -> None:
        self.content = content
        self.url = url

    def raise_for_status(self) -> None:
        return None


def _mock_feed(*entries, bozo=False, bozo_exception=None):
    return SimpleNamespace(
        entries=list(entries),
        bozo=bozo,
        bozo_exception=bozo_exception,
    )


def test_validate_connector_settings_rejects_invalid_feed_url(rss_module):
    connector = rss_module.RSSConnector(feed_url="ftp://example.com/feed.xml")

    with pytest.raises(ValueError, match="valid http or https URL"):
        connector.validate_connector_settings()


def test_validate_connector_settings_rejects_empty_feed(monkeypatch, rss_module):
    monkeypatch.setattr(rss_module.requests, "get", lambda *_args, **_kwargs: _FakeResponse())
    monkeypatch.setattr(rss_module.feedparser, "parse", lambda _content: _mock_feed())

    connector = rss_module.RSSConnector(feed_url="https://example.com/feed.xml")

    with pytest.raises(ValueError, match="contains no entries"):
        connector.validate_connector_settings()


def test_load_from_state_builds_documents(monkeypatch, rss_module):
    monkeypatch.setattr(rss_module.requests, "get", lambda *_args, **_kwargs: _FakeResponse())
    monkeypatch.setattr(
        rss_module.feedparser,
        "parse",
        lambda _content: _mock_feed(
            {
                "id": "entry-1",
                "link": "https://example.com/posts/1",
                "title": "Post One",
                "content": [{"value": "<p>Hello <b>world</b></p>"}],
                "author": "Alice",
                "tags": [{"term": "news"}, {"term": "product"}],
                "updated": "Tue, 02 Jan 2024 15:04:05 GMT",
            }
        ),
    )

    connector = rss_module.RSSConnector(feed_url="https://example.com/feed.xml")
    batch = next(connector.load_from_state())

    assert len(batch) == 1
    doc = batch[0]
    assert doc.source == rss_module.DocumentSource.RSS
    assert doc.semantic_identifier == "Post One"
    assert doc.extension == ".txt"
    assert doc.metadata == {
        "feed_url": "https://example.com/feed.xml",
        "link": "https://example.com/posts/1",
        "author": "Alice",
        "categories": ["news", "product"],
    }
    assert "Hello" in doc.blob.decode("utf-8")
    assert "world" in doc.blob.decode("utf-8")


def test_poll_source_filters_entries_by_timestamp(monkeypatch, rss_module):
    monkeypatch.setattr(rss_module.requests, "get", lambda *_args, **_kwargs: _FakeResponse())
    monkeypatch.setattr(
        rss_module.feedparser,
        "parse",
        lambda _content: _mock_feed(
            {
                "id": "entry-1",
                "title": "Older",
                "summary": "older summary",
                "updated": "Mon, 01 Jan 2024 00:00:00 GMT",
            },
            {
                "id": "entry-2",
                "title": "Newer",
                "summary": "new summary",
                "updated": "Tue, 02 Jan 2024 00:00:00 GMT",
            },
        ),
    )

    connector = rss_module.RSSConnector(feed_url="https://example.com/feed.xml")
    start = datetime(2024, 1, 1, tzinfo=UTC).timestamp()
    end = datetime(2024, 1, 2, tzinfo=UTC).timestamp()

    batches = list(connector.poll_source(start, end))

    assert len(batches) == 1
    assert [doc.semantic_identifier for doc in batches[0]] == ["Newer"]
