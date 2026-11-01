"""Size eligibility is shared by WebDAV content reads and deletion snapshots."""

import logging
from datetime import UTC, datetime
from typing import Any

import pytest

from common.data_source.interfaces import collect_slim_document_snapshot
from common.data_source.webdav_connector import WebDAVConnector
from tests.unit.test_webdav_deleted_snapshot import Client, item


def read_ids(connector: WebDAVConnector, mode: str) -> list[str]:
    if mode == "slim":
        return [doc.id for doc in collect_slim_document_snapshot(connector)]
    batches = connector.load_from_state() if mode == "load" else connector.poll_source(datetime(2024, 1, 1, tzinfo=UTC).timestamp(), datetime(2026, 1, 1, tzinfo=UTC).timestamp())
    return [doc.id for batch in batches for doc in batch]


@pytest.mark.parametrize("mode", ["load", "poll", "slim"])
@pytest.mark.parametrize("key", ["size", "content_length", "getcontentlength"])
def test_numeric_string_limits_apply_before_downloads(key: str, mode: str, caplog: pytest.LogCaptureFixture) -> None:
    connector = WebDAVConnector("https://dav.test", "/docs", batch_size=1)
    connector.size_threshold = 4
    entries = [
        {"name": "docs/equal.txt", "type": "file", key: " 4 "},
        {"name": "docs/oversized\nentry.txt", "type": "file", key: "5"},
    ]
    client = Client({"/docs": entries})
    connector.client = client  # type: ignore[assignment]
    caplog.set_level(logging.WARNING)

    assert read_ids(connector, mode) == ["webdav:https://dav.test:docs/equal.txt"]
    assert client.downloads == ([] if mode == "slim" else ["docs/equal.txt"])
    assert caplog.messages == ["oversized\\nentry.txt exceeds size threshold of 4 (size_bytes=5). Skipping."]


@pytest.mark.parametrize("mode", ["load", "poll", "slim"])
def test_zero_limit_accepts_zero_and_excludes_positive_sizes(mode: str) -> None:
    connector = WebDAVConnector("https://dav.test", "/docs")
    connector.size_threshold = 0
    client = Client({"/docs": [item("docs/zero.txt", content_length="0"), item("docs/positive.txt", content_length="1")]})
    connector.client = client  # type: ignore[assignment]

    assert read_ids(connector, mode) == ["webdav:https://dav.test:docs/zero.txt"]
    assert client.downloads == ([] if mode == "slim" else ["docs/zero.txt"])


def test_content_iterator_rechecks_size_before_downloading(monkeypatch: pytest.MonkeyPatch) -> None:
    connector = WebDAVConnector("https://dav.test", "/docs")
    connector.size_threshold = 4
    client = Client({})
    connector.client = client  # type: ignore[assignment]
    monkeypatch.setattr(connector, "_list_files_recursive", lambda *_args: [("docs/large.txt", item("docs/large.txt", size="5")), ("docs/small.txt", item("docs/small.txt", size="4"))])

    batches = list(connector.load_from_state())

    assert [doc.id for batch in batches for doc in batch] == ["webdav:https://dav.test:docs/small.txt"]
    assert batches[0][0].size_bytes == 4
    assert client.downloads == ["docs/small.txt"]


@pytest.mark.parametrize("mode", ["load", "poll", "slim"])
@pytest.mark.parametrize("size", [None, True, -1, "malformed", "", "9" * 21, "４"])
def test_unknown_size_aborts_the_complete_read_before_any_download(mode: str, size: Any) -> None:
    connector = WebDAVConnector("https://dav.test", "/docs", batch_size=1)
    client = Client({"/docs": [item("docs/good.txt"), item("docs/unknown.txt", content_length=size)]})
    connector.client = client  # type: ignore[assignment]

    with pytest.raises(ValueError, match="size metadata"):
        read_ids(connector, mode)
    assert not client.downloads


@pytest.mark.parametrize("threshold", [None, 0, 4])
def test_missing_metadata_still_fails_when_the_limit_is_disabled(threshold: int | None) -> None:
    connector = WebDAVConnector("https://dav.test", "/docs")
    connector.size_threshold = threshold
    connector.client = Client({"/docs": [{"name": "docs/unknown.txt", "type": "file"}]})  # type: ignore[assignment]
    with pytest.raises(ValueError, match="size metadata"):
        collect_slim_document_snapshot(connector)


@pytest.mark.parametrize(
    "metadata, expected",
    [
        ({"size": False, "content_length": "4"}, 4),
        ({"size": -1, "content_length": "bad", "getcontentlength": " 0 "}, 0),
        ({"size": "4", "content_length": "5"}, 4),
        ({"size": "9" * 20}, int("9" * 20)),
    ],
)
def test_valid_size_alias_fallback_and_precedence(metadata: dict[str, Any], expected: int) -> None:
    assert WebDAVConnector._get_size_bytes(metadata) == expected


@pytest.mark.parametrize(
    "path, expected",
    [
        ("https://user:synthetic-secret@dav.test/docs/large.txt?token=synthetic-token#fragment", "large.txt"),
        ("//user:synthetic-secret@dav.test/docs/large.txt?token=synthetic-token", "large.txt"),
        ("/docs/large\r\n\t.txt?token=synthetic-token", "large\\r\\n\\t.txt"),
        ("http://[invalid?token=synthetic-token", "<unknown>"),
    ],
)
def test_size_warning_redacts_urls_and_escapes_controls(path: str, expected: str, caplog: pytest.LogCaptureFixture) -> None:
    connector = WebDAVConnector("https://dav.test")
    connector.size_threshold = 0
    caplog.set_level(logging.WARNING)
    assert connector._exceeds_size_threshold(1, path)
    assert caplog.messages == [f"{expected} exceeds size threshold of 0 (size_bytes=1). Skipping."]
    assert "synthetic-secret" not in caplog.text and "synthetic-token" not in caplog.text
