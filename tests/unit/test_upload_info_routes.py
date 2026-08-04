"""FileService.upload_info 与其三条路由契约（restful_apis AsyncSession 收口）。

服务层：健康检查走 run_sync，PDF 修复 + 存储写入在工作线程执行；
路由层：file_api 的 code/data、canvas/document 的 retcode 两套形状钉板。
"""

import threading
import types

import pytest

from api.db.services.canvas_service import UserCanvasService
from api.db.services.document_service import DocumentService
from api.db.services.file_service import FileService

# ---------------------------------------------------------------------------
# 服务层（文件分支：run_sync 健康检查 + 工作线程 structured）
# ---------------------------------------------------------------------------


async def test_upload_info_file_branch_bridges_db_and_thread(async_db, monkeypatch):
    seen: dict[str, object] = {}

    def _check_doc_health(s, user_id, filename):
        seen["health"] = (user_id, filename)
        return True

    def _put_blob(user_id, location, blob):
        seen["off_loop"] = threading.current_thread() is not threading.main_thread()
        seen["blob"] = blob

    monkeypatch.setattr(DocumentService, "check_doc_health", classmethod(lambda cls, s, user_id, filename: _check_doc_health(s, user_id, filename)))
    monkeypatch.setattr(FileService, "put_blob", staticmethod(_put_blob))

    fake_file = types.SimpleNamespace(read=lambda: b"hello", filename="a.txt", content_type="text/plain")
    result = await FileService.upload_info(async_db, "user-unit", fake_file, None)

    assert seen["health"] == ("user-unit", "a.txt")
    assert seen["off_loop"] is True  # 存储写入必须在工作线程执行
    assert seen["blob"] == b"hello"
    assert result["name"] == "a.txt"
    assert result["created_by"] == "user-unit"
    assert result["mime_type"] == "text/plain"


async def test_upload_info_rejects_invalid_file_object(async_db):
    with pytest.raises(ValueError, match="Invalid file object"):
        await FileService.upload_info(async_db, "user-unit", types.SimpleNamespace(filename="x"), None)


# ---------------------------------------------------------------------------
# 路由层（FileService.upload_info 打桩，锁三套响应形状）
# ---------------------------------------------------------------------------


@pytest.fixture
def upload_info_stub(monkeypatch):
    calls: list[tuple] = []

    async def _fake(db, user_id, file, url=None):
        calls.append((user_id, getattr(file, "filename", None), url))
        return {"id": "loc1", "name": getattr(file, "filename", None) or "from-url"}

    monkeypatch.setattr(FileService, "upload_info", staticmethod(_fake))
    return calls


def test_file_api_upload_info_shape(client, upload_info_stub):
    resp = client.post("/api/v1/files/upload_info", files={"files": ("a.txt", b"data", "text/plain")})

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    assert body["data"] == {"id": "loc1", "name": "a.txt"}
    assert upload_info_stub == [("tenant-unit", "a.txt", None)]


def test_file_api_upload_info_rejects_file_plus_url(client, upload_info_stub):
    resp = client.post("/api/v1/files/upload_info?url=http://x", files={"files": ("a.txt", b"data", "text/plain")})

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] != 0
    assert "not both" in body["message"]
    assert upload_info_stub == []


def test_canvas_upload_shape(client, upload_info_stub, monkeypatch):
    monkeypatch.setattr(UserCanvasService, "get_by_canvas_id", classmethod(lambda cls, s, cid: (True, {"user_id": "owner-1"})))

    resp = client.post("/api/v1/agents/c1/upload", files={"file": ("b.txt", b"data", "text/plain")})

    assert resp.status_code == 200
    body = resp.json()
    assert body["retcode"] == 0
    assert body["data"] == {"id": "loc1", "name": "b.txt"}
    assert upload_info_stub == [("owner-1", "b.txt", None)]


def test_canvas_upload_missing_canvas_shape(client, upload_info_stub, monkeypatch):
    monkeypatch.setattr(UserCanvasService, "get_by_canvas_id", classmethod(lambda cls, s, cid: (False, None)))

    resp = client.post("/api/v1/agents/missing/upload", files={"file": ("b.txt", b"data", "text/plain")})

    assert resp.status_code == 200
    body = resp.json()
    assert body["retcode"] != 0
    assert body["retmsg"] == "canvas not found."
    assert upload_info_stub == []


def test_document_upload_info_shape(client, upload_info_stub):
    resp = client.post("/v1/document/upload_info", files={"file": ("c.txt", b"data", "text/plain")})

    assert resp.status_code == 200
    body = resp.json()
    assert body["retcode"] == 0
    assert body["data"] == {"id": "loc1", "name": "c.txt"}
    assert upload_info_stub == [("user-unit", "c.txt", None)]  # Principal.id 注入 user_id 位
