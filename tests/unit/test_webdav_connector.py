import importlib
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import pytest

import common


@pytest.fixture
def webdav_module(monkeypatch):
    module_names = [
        "common.data_source.config",
        "common.data_source.exceptions",
        "common.data_source.interfaces",
        "common.data_source.models",
        "common.data_source.utils",
        "common.data_source.webdav_connector",
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

    module = importlib.import_module("common.data_source.webdav_connector")
    yield module

    for name, saved_module in saved_modules.items():
        if saved_module is missing:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = saved_module


class FakeWebDAVClient:
    def __init__(self, items):
        self.items = items
        self.downloaded_paths: list[str] = []

    def ls(self, path, detail=True):
        assert detail is True
        return self.items.get(path, [])

    def download_fileobj(self, path, buffer):
        self.downloaded_paths.append(path)
        buffer.write(f"content for {path}".encode())


def _item(name: str, modified: datetime, size: int = 100, item_type: str = "file"):
    return {
        "name": name,
        "modified": modified,
        "size": size,
        "type": item_type,
    }


def test_list_files_recursive_filters_unsupported_extensions(webdav_module):
    modified = datetime(2026, 1, 1, tzinfo=UTC)
    connector = webdav_module.WebDAVConnector(base_url="https://example.com", remote_path="/docs")
    connector.client = FakeWebDAVClient(
        {
            "/docs": [
                _item("/docs/report.pdf", modified),
                _item("/docs/run.exe", modified),
                _item("/docs/photo.png", modified),
            ]
        }
    )

    files = connector._list_files_recursive(
        "/docs",
        datetime(2025, 1, 1, tzinfo=UTC),
        datetime(2027, 1, 1, tzinfo=UTC),
    )

    assert [path for path, _info in files] == ["/docs/report.pdf"]

    connector.set_allow_images(True)

    files = connector._list_files_recursive(
        "/docs",
        datetime(2025, 1, 1, tzinfo=UTC),
        datetime(2027, 1, 1, tzinfo=UTC),
    )

    assert [path for path, _info in files] == ["/docs/report.pdf", "/docs/photo.png"]


def test_yield_webdav_documents_defensively_skips_unsupported_files(webdav_module, monkeypatch):
    modified = datetime(2026, 1, 1, tzinfo=UTC)
    connector = webdav_module.WebDAVConnector(base_url="https://example.com", remote_path="/docs")
    client = FakeWebDAVClient({})
    connector.client = client
    monkeypatch.setattr(
        connector,
        "_list_files_recursive",
        lambda *_args: [
            ("/docs/report.pdf", _item("/docs/report.pdf", modified)),
            ("/docs/run.exe", _item("/docs/run.exe", modified)),
        ],
    )

    batches = list(
        connector._yield_webdav_documents(
            datetime(2025, 1, 1, tzinfo=UTC),
            datetime(2027, 1, 1, tzinfo=UTC),
        )
    )

    assert [[doc.semantic_identifier for doc in batch] for batch in batches] == [["report.pdf"]]
    assert client.downloaded_paths == ["/docs/report.pdf"]
