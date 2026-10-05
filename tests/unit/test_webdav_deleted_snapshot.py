from datetime import UTC, datetime
from io import BytesIO
from typing import Any

import pytest

from common.data_source.interfaces import collect_slim_document_snapshot
from common.data_source.webdav_connector import WebDAVConnector


class Client:
    def __init__(self, tree: dict[str, Any]) -> None:
        self.tree = tree
        self.downloads: list[str] = []

    def ls(self, path: str, detail: bool = True) -> Any:
        result = self.tree[path]
        if isinstance(result, Exception):
            raise result
        return result

    def download_fileobj(self, path: str, buffer: BytesIO) -> None:
        self.downloads.append(path)
        buffer.write(b"body")


def item(name: str, **kwargs: Any) -> dict[str, Any]:
    return {"name": name, "type": "file", "content_length": "4", "modified": datetime(2025, 1, 1, tzinfo=UTC), **kwargs}


@pytest.mark.parametrize("images", [False, True])
def test_full_tree_ids_and_eligibility_match_content(images: bool) -> None:
    connector = WebDAVConnector("https://dav.test/base/", "/docs", batch_size=1)
    connector.size_threshold = 4
    connector.set_allow_images(images)
    client = Client(
        {
            "/docs": [item("docs/old.txt"), item("docs/large.txt", content_length="5"), item("docs/no.exe"), item("docs/photo.png"), item("docs/sub", type="directory")],
            "docs/sub": [item("docs/sub/old.txt", size=" 4 ")],
        }
    )
    connector.client = client  # type: ignore[assignment]
    snapshot = collect_slim_document_snapshot(connector)
    assert not client.downloads
    expected = ["docs/old.txt", *(["docs/photo.png"] if images else []), "docs/sub/old.txt"]
    assert [doc.id for doc in snapshot] == [f"webdav:https://dav.test/base:{path}" for path in expected]
    assert [doc.id for batch in connector.load_from_state() for doc in batch] == [doc.id for doc in snapshot]
    assert client.downloads == expected
    assert list(connector.poll_source(datetime(2026, 1, 1, tzinfo=UTC).timestamp(), datetime(2026, 2, 1, tzinfo=UTC).timestamp())) == []


@pytest.mark.parametrize(
    "bad",
    [PermissionError("nested denied"), None, [item("outside/file.txt")], [item("docs/sub/../escape.txt")], [item("docs/sub/missing.txt", content_length=None)], [item("docs/sub/unknown", type=None)]],
)
def test_partial_tree_is_never_published(bad: Any) -> None:
    connector = WebDAVConnector("https://dav.test", "/docs", batch_size=1)
    connector.client = Client({"/docs": [item("docs/good.txt"), item("docs/sub", type="directory")], "docs/sub": bad})  # type: ignore[assignment]
    with pytest.raises((ValueError, PermissionError)):
        collect_slim_document_snapshot(connector)


@pytest.mark.parametrize("metadata, expected", [({"size": " 12 "}, 12), ({"content_length": 0}, 0), ({"size": None, "getcontentlength": "4"}, 4)])
def test_numeric_size_metadata(metadata: dict[str, Any], expected: int) -> None:
    assert WebDAVConnector._get_size_bytes(metadata) == expected


@pytest.mark.parametrize("value", [None, True, -1, "-1", "unknown", "9" * 21])
def test_unknown_size_cannot_authorize_deletion(value: Any) -> None:
    with pytest.raises(ValueError, match="size metadata"):
        WebDAVConnector._get_size_bytes({"size": value})
