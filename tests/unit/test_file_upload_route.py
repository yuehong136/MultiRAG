"""file_app.upload AsyncSession 化后的契约测试(§11 Phase 1)。

断言只锁 HTTP 契约;FileService/DocumentService monkeypatch 真实类,
存储走假件(记录 put 调用)。
"""

import sys
import types

import pytest

from api.db.services.document_service import DocumentService
from api.db.services.file_service import FileService


def _file_module():
    return sys.modules["api.apps.file"]


class _FakeStorage:
    def __init__(self):
        self.puts: list[tuple] = []

    def obj_exist(self, folder_id, location):
        return False

    def put(self, folder_id, location, blob):
        self.puts.append((folder_id, location, len(blob)))


@pytest.fixture
def upload_stubs(monkeypatch):
    storage = _FakeStorage()
    folder = types.SimpleNamespace(id="folder-1", name="root")
    inserted = types.SimpleNamespace(id="file-1", parent_id="folder-1", tenant_id="user-unit", created_by="user-unit", name="a.txt", location="a.txt", size=4, type="doc")

    monkeypatch.setattr(FileService, "get_root_folder", classmethod(lambda cls, s, uid: {"id": "root-1"}))
    monkeypatch.setattr(FileService, "get_by_id", classmethod(lambda cls, s, fid: folder))
    monkeypatch.setattr(FileService, "get_id_list_by_id", classmethod(lambda cls, s, pid, names, depth, ids: ["root-1", "leaf"]))
    monkeypatch.setattr(FileService, "create_folder", classmethod(lambda cls, s, f, fid, names, n: folder))
    monkeypatch.setattr(FileService, "insert", classmethod(lambda cls, s, data: inserted))
    monkeypatch.setattr(DocumentService, "get_doc_count", classmethod(lambda cls, s, uid: 0))
    monkeypatch.setattr(_file_module(), "duplicate_name", lambda query, db, name, parent_id: name)
    monkeypatch.setattr(_file_module().settings, "STORAGE_IMPL", storage, raising=False)
    return storage


def test_upload_stores_blob_and_returns_file_dict(client, upload_stubs):
    resp = client.post(
        "/v1/file/upload",
        params={"parent_id": ""},
        files=[("files", ("a.txt", b"data", "text/plain"))],
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["retcode"] == 0
    assert body["data"]["id"] == "file-1"
    assert body["data"]["name"] == "a.txt"
    assert upload_stubs.puts == [("folder-1", "a.txt", 4)]


def test_upload_rejects_missing_folder(client, upload_stubs, monkeypatch):
    monkeypatch.setattr(FileService, "get_by_id", classmethod(lambda cls, s, fid: None))

    resp = client.post(
        "/v1/file/upload",
        params={"parent_id": "nope"},
        files=[("files", ("a.txt", b"data", "text/plain"))],
    )

    assert resp.status_code == 200
    assert resp.json()["retcode"] != 0
