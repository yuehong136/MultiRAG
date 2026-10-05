"""RSS feed membership snapshots preserve body identities and reject parse failures."""

import hashlib
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from common.data_source.interfaces import collect_slim_document_snapshot
from common.data_source.rss_connector import RSSConnector


@pytest.mark.parametrize(
    "entry, key",
    [
        ({"id": "guid", "link": "https://feed.test/post", "title": "title"}, "guid"),
        ({"link": "https://feed.test/post", "title": "title"}, "https://feed.test/post"),
        ({"title": "title"}, "title"),
        ({}, "https://feed.test/rss"),
    ],
)
def test_body_and_snapshot_use_the_same_id(entry: dict[str, Any], key: str) -> None:
    connector = RSSConnector("https://feed.test/rss")
    document = connector._build_document(entry, datetime(2026, 1, 1, tzinfo=UTC))
    assert document.id == f"rss:{hashlib.md5(key.encode()).hexdigest()}"
    connector._cached_feed = SimpleNamespace(entries=[entry], bozo=False)
    assert [doc.id for doc in collect_slim_document_snapshot(connector)] == [document.id]


@pytest.mark.parametrize("partial", [False, True])
def test_empty_and_partial_feed_snapshots(partial: bool) -> None:
    connector = RSSConnector("https://feed.test/rss")
    connector._cached_feed = SimpleNamespace(entries=[{"id": "retained"}] if partial else [], bozo=partial)
    if partial:
        with pytest.raises(ValueError, match="partially parsed"):
            collect_slim_document_snapshot(connector)
    else:
        assert collect_slim_document_snapshot(connector) == ()
