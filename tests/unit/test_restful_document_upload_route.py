"""document RESTful 上传路由契约测试。

POST /api/v1/datasets/{dataset_id}/documents 是 web 会话与 API token 的统一上传
入口（原 /v1/document/upload 标 deprecated、原 sdk 同路径版已收编于此）。
走真实 ``api.apps.app`` 的 HTTP 契约式：service 桩记录并断言收到
``sqlalchemy.orm.Session``；文件读取在事件循环内、DB/存储在单一 run_sync 回调内。
键名映射的纯逻辑钉板（含未知 run 值原样透出的 fallback）也收在本文件。
"""

import sys
from types import SimpleNamespace

from sqlalchemy.orm import Session

from api.apps.services import document_api_service
from api.db.db_models import get_async_db, get_db
from api.db.services.file_service import FileService
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.utils.api_utils import async_current_tenant_id, current_tenant_id
from common.constants import RetCode

_PATH = "/api/v1/datasets/kb1/documents"

_RAW_DOC = {"id": "doc1", "name": "a.pdf", "kb_id": "kb1", "chunk_num": 0, "token_num": 0, "parser_id": "naive"}


def _route_module():
    """app 加载器以剥掉 ``_api`` 后缀的模块名注册路由模块；直接 import
    ``api.apps.restful_apis.document_api`` 会得到第二个实例，打桩不生效。"""
    return sys.modules["api.apps.restful_apis.document"]


def _stub_happy_chain(monkeypatch, sessions, upload_calls=None):
    """铺满主链路桩：kb 存在、有团队权限、上传成功返回一个原始文档 dict。"""
    kb = SimpleNamespace(id="kb1", tenant_id="tenant-unit")
    monkeypatch.setattr(KnowledgebaseService, "get_by_id", classmethod(lambda cls, s, kid: sessions.append(s) or kb))
    monkeypatch.setattr(_route_module(), "check_kb_team_permission", lambda s, k, tid: sessions.append(s) or True)

    def _upload(cls, s, k, file_contents, user_id, parent_path=None, **kw):
        sessions.append(s)
        if upload_calls is not None:
            upload_calls.append({"file_contents": file_contents, "user_id": user_id, "parent_path": parent_path})
        return [], [(dict(_RAW_DOC), b"blob")]

    monkeypatch.setattr(FileService, "upload_document", classmethod(_upload))
    return kb


def _assert_sync_facade(sessions):
    assert sessions
    for s in sessions:
        assert isinstance(s, Session), f"同步 service 收到 {type(s).__name__}，应为 sqlalchemy.orm.Session"


def test_upload_returns_mapped_doc_by_default(client, monkeypatch):
    sessions: list[object] = []
    upload_calls: list[dict] = []
    _stub_happy_chain(monkeypatch, sessions, upload_calls)

    resp = client.post(_PATH, files=[("file", ("a.pdf", b"content", "application/pdf"))])

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    assert body["data"] == [{"id": "doc1", "name": "a.pdf", "dataset_id": "kb1", "chunk_count": 0, "token_count": 0, "chunk_method": "naive", "run": "UNSTART"}]
    assert upload_calls[0]["file_contents"] == [(b"content", "a.pdf")]
    assert upload_calls[0]["user_id"] == "tenant-unit"
    _assert_sync_facade(sessions)


def test_upload_return_raw_files_skips_mapping(client, monkeypatch):
    sessions: list[object] = []
    _stub_happy_chain(monkeypatch, sessions)

    resp = client.post(f"{_PATH}?return_raw_files=true", files=[("file", ("a.pdf", b"content", "application/pdf"))])

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    assert body["data"] == [_RAW_DOC]
    _assert_sync_facade(sessions)


def test_upload_accepts_legacy_files_field_and_merges(client, monkeypatch):
    """multipart 字段 ``files`` 兼容既有 SDK 消费方，与 ``file`` 按序合并。"""
    sessions: list[object] = []
    upload_calls: list[dict] = []
    _stub_happy_chain(monkeypatch, sessions, upload_calls)

    resp = client.post(
        _PATH,
        files=[
            ("file", ("a.pdf", b"aa", "application/pdf")),
            ("files", ("b.pdf", b"bb", "application/pdf")),
        ],
    )

    assert resp.status_code == 200
    assert resp.json()["code"] == 0
    assert upload_calls[0]["file_contents"] == [(b"aa", "a.pdf"), (b"bb", "b.pdf")]
    _assert_sync_facade(sessions)


def test_upload_forwards_parent_path(client, monkeypatch):
    sessions: list[object] = []
    upload_calls: list[dict] = []
    _stub_happy_chain(monkeypatch, sessions, upload_calls)

    resp = client.post(_PATH, files=[("file", ("a.pdf", b"content", "application/pdf"))], data={"parent_path": "sub/folder"})

    assert resp.status_code == 200
    assert resp.json()["code"] == 0
    assert upload_calls[0]["parent_path"] == "sub/folder"
    _assert_sync_facade(sessions)


def test_upload_without_file_part(client):
    resp = client.post(_PATH)

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == int(RetCode.ARGUMENT_ERROR)
    assert body["message"] == "No file part!"


def test_upload_rejects_too_many_files(client, monkeypatch):
    monkeypatch.setattr(_route_module(), "MAXIMUM_OF_UPLOADING_FILES", 1)

    resp = client.post(
        _PATH,
        files=[
            ("file", ("a.pdf", b"aa", "application/pdf")),
            ("file", ("b.pdf", b"bb", "application/pdf")),
        ],
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == int(RetCode.ARGUMENT_ERROR)
    assert "exceeds the maximum number" in body["message"]


def test_upload_rejects_overlong_filename(client):
    resp = client.post(_PATH, files=[("file", (f"{'x' * 256}.pdf", b"content", "application/pdf"))])

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == int(RetCode.ARGUMENT_ERROR)
    assert "bytes or less" in body["message"]


def test_upload_missing_dataset(client, monkeypatch):
    sessions: list[object] = []
    monkeypatch.setattr(KnowledgebaseService, "get_by_id", classmethod(lambda cls, s, kid: sessions.append(s) or None))

    resp = client.post(_PATH, files=[("file", ("a.pdf", b"content", "application/pdf"))])

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == int(RetCode.DATA_ERROR)
    assert body["message"] == "Can't find the dataset with ID kb1!"
    _assert_sync_facade(sessions)


def test_upload_denies_without_team_permission(client, monkeypatch):
    sessions: list[object] = []
    kb = SimpleNamespace(id="kb1", tenant_id="other-tenant")
    monkeypatch.setattr(KnowledgebaseService, "get_by_id", classmethod(lambda cls, s, kid: sessions.append(s) or kb))
    monkeypatch.setattr(_route_module(), "check_kb_team_permission", lambda s, k, tid: sessions.append(s) or False)

    resp = client.post(_PATH, files=[("file", ("a.pdf", b"content", "application/pdf"))])

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == int(RetCode.AUTHENTICATION_ERROR)
    assert body["message"] == "No authorization."
    _assert_sync_facade(sessions)


def test_upload_surfaces_service_errors(client, monkeypatch):
    sessions: list[object] = []
    kb = SimpleNamespace(id="kb1", tenant_id="tenant-unit")
    monkeypatch.setattr(KnowledgebaseService, "get_by_id", classmethod(lambda cls, s, kid: sessions.append(s) or kb))
    monkeypatch.setattr(_route_module(), "check_kb_team_permission", lambda s, k, tid: sessions.append(s) or True)
    monkeypatch.setattr(FileService, "upload_document", classmethod(lambda cls, s, k, fc, uid, parent_path=None, **kw: sessions.append(s) or (["boom"], [])))

    resp = client.post(_PATH, files=[("file", ("a.pdf", b"content", "application/pdf"))])

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == int(RetCode.SERVER_ERROR)
    assert body["message"] == "boom"
    _assert_sync_facade(sessions)


def test_upload_route_has_pure_async_dependency_tree(client, route_dependency_calls):
    import api.apps as api_apps

    calls = route_dependency_calls(client.app, "POST", "/api/v1/datasets/{dataset_id}/documents")

    assert get_db not in calls
    assert current_tenant_id not in calls
    assert api_apps.manager not in calls
    assert async_current_tenant_id in calls
    assert get_async_db in calls


# ---------------------------------------------------------------------------
# 键名映射纯逻辑钉板
# ---------------------------------------------------------------------------


def test_map_doc_keys_with_run_status_maps_keys_and_run():
    mapped = document_api_service.map_doc_keys_with_run_status(dict(_RAW_DOC), "0")

    assert mapped == {"id": "doc1", "name": "a.pdf", "dataset_id": "kb1", "chunk_count": 0, "token_count": 0, "chunk_method": "naive", "run": "UNSTART"}


def test_process_run_mapping_keeps_unknown_run_value():
    """未知 run 值原样透出（不强制归 UNSTART），避免丢失状态信息。"""
    assert document_api_service._process_run_mapping({"run": "9"}, "9") == {"run": "9"}
    assert document_api_service._process_run_mapping({}, "3") == {"run": "DONE"}
