"""file RESTful API 契约测试（Phase 2.5 批次 3：AsyncSession 收口）。

路由按 IO 面分形态，测试锁各自的类型契约：
- 纯 DB（create_folder/list/root/parent/ancestors/convert）：路由层 run_sync——桩断言
  收到同步 facade（``sqlalchemy.orm.Session``）且在事件循环线程上；
- 混轨（upload/delete/move，存储与 DB 交错在共享 helper 内）：service ``*_async``
  包装——桩断言在工作线程收到 ``db_connection`` 自开的同步 Session；
- download：DB 面 run_sync（回调内取纯字段，ORM 不逸出），存储读取 to_thread。
upload_info 已于 11.11 转换（tests 见 test_upload_info_routes.py）。
convert 的 HTTP 契约测试由 test_file2document_convert_parity.py 的 sys.modules
伪造直调版迁移而来（路由转 async 后旧形态失效，按 AGENTS「坏了才按契约式重写」）。
"""

import sys
import threading
from types import SimpleNamespace

from sqlalchemy.orm import Session

from api.apps import deps
from api.apps.services import file_api_service
from api.db.db_models import get_db
from api.db.services.file2document_service import File2DocumentService
from api.db.services.file_service import FileService
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.utils.api_utils import async_current_tenant_id, current_tenant_id
from common.constants import RetCode


def _record(records, s):
    records.append({"session": s, "off_loop": threading.current_thread() is not threading.main_thread()})


def _assert_sync_facade(records, *, off_loop=None):
    assert records
    for r in records:
        assert isinstance(r["session"], Session), f"同步 service 收到 {type(r['session']).__name__}，应为 sqlalchemy.orm.Session"
        if off_loop is not None:
            assert r["off_loop"] is off_loop


def _route_module():
    """app 绑定的是 register_page spec-load 的模块实例（无 `_api` 后缀）。"""
    return sys.modules["api.apps.restful_apis.file"]


# ---------------------------------------------------------------------------
# 纯 DB 形态（路由层 run_sync）
# ---------------------------------------------------------------------------


def test_file_create_folder_json_branch(client, monkeypatch):
    records: list[dict] = []

    def _create_folder(s, tenant_id, name, pf_id, file_type):
        _record(records, s)
        assert (tenant_id, name, pf_id, file_type) == ("tenant-unit", "docs", None, "folder")
        return True, {"id": "f-1", "name": "docs"}

    monkeypatch.setattr(file_api_service, "create_folder", _create_folder)

    resp = client.post("/api/v1/files", json={"name": "docs", "type": "folder"})

    body = resp.json()
    assert body["code"] == 0
    assert body["data"] == {"id": "f-1", "name": "docs"}
    _assert_sync_facade(records)


def test_file_list_envelope(client, monkeypatch):
    records: list[dict] = []
    payload = {"total": 1, "files": [{"id": "f-1"}], "parent_folder": {"id": "root"}}

    def _list(s, tenant_id, args):
        _record(records, s)
        assert args["page"] == 1 and args["page_size"] == 15
        return True, payload

    monkeypatch.setattr(file_api_service, "list_files", _list)

    body = client.get("/api/v1/files").json()

    assert body["code"] == 0
    assert body["data"] == payload
    _assert_sync_facade(records)


def test_file_root_folder(client, monkeypatch):
    records: list[dict] = []
    monkeypatch.setattr(FileService, "get_root_folder", classmethod(lambda cls, s, tid: _record(records, s) or {"id": "root", "name": "/"}))

    body = client.get("/api/v1/files/root").json()

    assert body["code"] == 0
    assert body["data"] == {"root_folder": {"id": "root", "name": "/"}}
    _assert_sync_facade(records)


def test_file_parent_and_ancestors(client, monkeypatch):
    records: list[dict] = []
    monkeypatch.setattr(file_api_service, "get_parent_folder", lambda s, fid: _record(records, s) or (True, {"parent_folder": {"id": "p-1"}}))
    monkeypatch.setattr(file_api_service, "get_all_parent_folders", lambda s, fid: _record(records, s) or (True, {"parent_folders": [{"id": "p-1"}]}))

    parent = client.get("/api/v1/files/f-1/parent").json()
    ancestors = client.get("/api/v1/files/f-1/ancestors").json()

    assert parent["code"] == 0 and parent["data"] == {"parent_folder": {"id": "p-1"}}
    assert ancestors["code"] == 0 and ancestors["data"] == {"parent_folders": [{"id": "p-1"}]}
    _assert_sync_facade(records)


# ---------------------------------------------------------------------------
# 混轨形态（service *_async 包装：工作线程 + db_connection 自开短会话）
# ---------------------------------------------------------------------------


def test_file_upload_multipart_runs_off_loop(client, monkeypatch):
    records: list[dict] = []

    def _upload(s, tenant_id, pf_id, file_contents):
        _record(records, s)
        assert (tenant_id, pf_id) == ("tenant-unit", "pf-1")
        assert file_contents == [(b"hello", "a.txt")]
        return True, [{"id": "f-1", "name": "a.txt"}]

    monkeypatch.setattr(file_api_service, "upload_file", _upload)

    resp = client.post("/api/v1/files", files=[("file", ("a.txt", b"hello"))], data={"parent_id": "pf-1"})

    body = resp.json()
    assert body["code"] == 0
    assert body["data"] == [{"id": "f-1", "name": "a.txt"}]
    _assert_sync_facade(records, off_loop=True)  # 混轨块必须在工作线程执行


def test_file_upload_requires_file_part(client):
    resp = client.post("/api/v1/files", files=[("other", ("a.txt", b"x"))])

    body = resp.json()
    assert body["code"] == int(RetCode.ARGUMENT_ERROR)
    assert body["message"] == "No file part!"


def test_file_delete_runs_off_loop(client, monkeypatch):
    records: list[dict] = []

    def _delete(s, uid, file_ids):
        _record(records, s)
        assert (uid, file_ids) == ("tenant-unit", ["f-1", "f-2"])
        return True, True

    monkeypatch.setattr(file_api_service, "delete_files", _delete)

    body = client.request("DELETE", "/api/v1/files", json={"ids": ["f-1", "f-2"]}).json()

    assert body["code"] == 0
    assert body["data"] is True
    _assert_sync_facade(records, off_loop=True)


def test_file_move_runs_off_loop(client, monkeypatch):
    records: list[dict] = []

    def _move(s, uid, src_file_ids, dest_file_id, new_name):
        _record(records, s)
        assert (src_file_ids, dest_file_id, new_name) == (["f-1"], "d-1", "renamed.txt")
        return True, True

    monkeypatch.setattr(file_api_service, "move_files", _move)

    body = client.post("/api/v1/files/move", json={"src_file_ids": ["f-1"], "dest_file_id": "d-1", "new_name": "renamed.txt"}).json()

    assert body["code"] == 0
    assert body["data"] is True
    _assert_sync_facade(records, off_loop=True)


# ---------------------------------------------------------------------------
# download（DB run_sync + 存储 to_thread）
# ---------------------------------------------------------------------------


class _FakeStorage:
    def __init__(self, blobs):
        self.blobs = blobs
        self.calls: list[dict] = []

    def get(self, bucket, name):
        self.calls.append({"bucket": bucket, "name": name, "off_loop": threading.current_thread() is not threading.main_thread()})
        return self.blobs.get((bucket, name))


def test_file_download_streams_blob_off_loop(client, monkeypatch):
    records: list[dict] = []
    monkeypatch.setattr(
        file_api_service,
        "get_file_content",
        lambda s, uid, fid: _record(records, s) or (True, SimpleNamespace(parent_id="pf-1", location="a.txt", name="a.txt", type="doc")),
    )
    storage = _FakeStorage({("pf-1", "a.txt"): b"hello"})
    client.app.dependency_overrides[deps.get_storage] = lambda: storage

    resp = client.get("/api/v1/files/f-1")

    assert resp.status_code == 200
    assert resp.content == b"hello"
    assert resp.headers["content-disposition"] == "attachment; filename=a.txt"
    assert storage.calls and all(c["off_loop"] for c in storage.calls)  # 存储读取必须在工作线程
    _assert_sync_facade(records)


def test_file_download_falls_back_to_document_address(client, monkeypatch):
    records: list[dict] = []
    monkeypatch.setattr(
        file_api_service,
        "get_file_content",
        lambda s, uid, fid: _record(records, s) or (True, SimpleNamespace(parent_id="pf-1", location="gone.txt", name="gone.txt", type="doc")),
    )
    monkeypatch.setattr(File2DocumentService, "get_storage_address", classmethod(lambda cls, s, file_id: _record(records, s) or ("kb-bucket", "doc-loc")))
    storage = _FakeStorage({("kb-bucket", "doc-loc"): b"fallback"})
    client.app.dependency_overrides[deps.get_storage] = lambda: storage

    resp = client.get("/api/v1/files/f-1")

    assert resp.content == b"fallback"
    assert [(c["bucket"], c["name"]) for c in storage.calls] == [("pf-1", "gone.txt"), ("kb-bucket", "doc-loc")]
    _assert_sync_facade(records)


def test_file_download_denied(client, monkeypatch):
    monkeypatch.setattr(file_api_service, "get_file_content", lambda s, uid, fid: (False, "No authorization."))

    body = client.get("/api/v1/files/f-1").json()

    assert body["code"] == int(RetCode.DATA_ERROR)
    assert body["message"] == "No authorization."


# ---------------------------------------------------------------------------
# convert 与 attachment 下载（收编自 sdk/files.py 的专有端点）
# ---------------------------------------------------------------------------


def test_sdk_file_convert_schedules_background_work(client, monkeypatch):
    records: list[dict] = []
    folder = SimpleNamespace(id="folder-1", type="folder")
    monkeypatch.setattr(FileService, "get_by_ids", classmethod(lambda cls, s, ids: _record(records, s) or [folder]))
    monkeypatch.setattr(FileService, "get_all_innermost_file_ids", classmethod(lambda cls, s, fid, acc: _record(records, s) or ["inner-1"]))
    monkeypatch.setattr(KnowledgebaseService, "get_by_id", classmethod(lambda cls, s, kid: _record(records, s) or SimpleNamespace(id=kid)))
    scheduled: list[tuple] = []
    monkeypatch.setattr(_route_module(), "convert_files_with_new_session", lambda *args: scheduled.append(args))

    resp = client.post("/api/v1/file/convert", json={"kb_ids": ["kb-1"], "file_ids": ["folder-1"]})

    body = resp.json()
    assert body["retcode"] == 0
    assert body["data"] is True
    assert scheduled == [(["inner-1"], ["kb-1"], "tenant-unit")]  # TestClient 同步执行 background task
    _assert_sync_facade(records)


def test_sdk_file_convert_rejects_missing_file(client, monkeypatch):
    monkeypatch.setattr(FileService, "get_by_ids", classmethod(lambda cls, s, ids: []))
    scheduled: list[tuple] = []
    monkeypatch.setattr(_route_module(), "convert_files_with_new_session", lambda *args: scheduled.append(args))

    body = client.post("/api/v1/file/convert", json={"kb_ids": ["kb-1"], "file_ids": ["missing"]}).json()

    assert body["retmsg"] == "File not found!"
    assert scheduled == []


def test_file_download_attachment_off_loop(client):
    storage = _FakeStorage({("tenant-unit", "att-1"): b"# md"})
    client.app.dependency_overrides[deps.get_storage] = lambda: storage

    resp = client.get("/api/v1/file/download/att-1?ext=md")

    assert resp.status_code == 200
    assert resp.content == b"# md"
    assert storage.calls[0]["off_loop"] is True


# ---------------------------------------------------------------------------
# 依赖树（11 条路由全部纯异步轨）
# ---------------------------------------------------------------------------


def test_file_routes_have_pure_async_dependency_tree(client, route_dependency_calls):
    import api.apps as api_apps

    for method, path in (
        ("POST", "/api/v1/files"),
        ("GET", "/api/v1/files"),
        ("POST", "/api/v1/files/upload_info"),
        ("GET", "/api/v1/files/root"),
        ("DELETE", "/api/v1/files"),
        ("POST", "/api/v1/files/move"),
        ("GET", "/api/v1/files/{file_id}"),
        ("GET", "/api/v1/files/{file_id}/parent"),
        ("GET", "/api/v1/files/{file_id}/ancestors"),
        ("POST", "/api/v1/file/convert"),
        ("GET", "/api/v1/file/download/{attachment_id}"),
    ):
        calls = route_dependency_calls(client.app, method, path)
        assert get_db not in calls, f"{method} {path} 依赖树含同步 get_db"
        assert current_tenant_id not in calls, f"{method} {path} 依赖树含同步 current_tenant_id"
        assert api_apps.manager not in calls, f"{method} {path} 依赖树含同步 manager"
        assert async_current_tenant_id in calls, f"{method} {path} 缺异步鉴权依赖"
