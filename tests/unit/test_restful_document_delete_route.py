"""document RESTful 删除路由契约测试。

DELETE /api/v1/datasets/{dataset_id}/documents 是 web 会话与 API token 的统一删除
入口（原 /v1/document/rm 标 deprecated、原 sdk 同路径版已收编于此）。

三条钉板值得单独说明：
- **归属校验**：只能删属于本数据集的文档，防止拿任一自有数据集越权删他人文档；
- **去重**：重复 ID 只删一次，不让第二次 "Document not found" 把整批判失败；
- **ids/delete_all 互斥**：两者都不给或都给都拒绝，不再静默返回成功。
"""

import sys
from types import SimpleNamespace

from sqlalchemy.orm import Session

from api.db.db_models import get_async_db, get_db
from api.db.services.document_service import DocumentService
from api.db.services.file_service import FileService
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.utils.api_utils import async_current_tenant_id, current_tenant_id
from common.constants import RetCode

_PATH = "/api/v1/datasets/kb1/documents"


def _route_module():
    """app 加载器以剥掉 ``_api`` 后缀的模块名注册路由模块；直接 import
    ``api.apps.restful_apis.document_api`` 会得到第二个实例，打桩不生效。"""
    return sys.modules["api.apps.restful_apis.document"]


def _stub_chain(monkeypatch, sessions, *, accessible=True, doc_ids=("doc1", "doc2"), errors=""):
    """铺满主链路桩：数据集可访问、数据集内含 doc_ids、删除返回 errors。"""
    delete_calls: list[dict] = []

    monkeypatch.setattr(KnowledgebaseService, "accessible", classmethod(lambda cls, s, kb_id, user_id: sessions.append(s) or accessible))
    monkeypatch.setattr(DocumentService, "query", classmethod(lambda cls, s, **kw: sessions.append(s) or [SimpleNamespace(id=d) for d in doc_ids]))

    def _delete_docs(cls, s, ids, tenant_id):
        sessions.append(s)
        delete_calls.append({"ids": list(ids), "tenant_id": tenant_id})
        return errors

    monkeypatch.setattr(FileService, "delete_docs", classmethod(_delete_docs))
    return delete_calls


def _assert_sync_facade(sessions):
    assert sessions
    for s in sessions:
        assert isinstance(s, Session), f"同步 service 收到 {type(s).__name__}，应为 sqlalchemy.orm.Session"


def test_delete_by_ids(client, monkeypatch):
    sessions: list[object] = []
    delete_calls = _stub_chain(monkeypatch, sessions)

    resp = client.request("DELETE", _PATH, json={"ids": ["doc1"]})

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    assert body["data"] == {"deleted": 1}
    assert delete_calls[0]["ids"] == ["doc1"]
    assert delete_calls[0]["tenant_id"] == "tenant-unit"
    _assert_sync_facade(sessions)


def test_delete_all_removes_every_document_in_dataset(client, monkeypatch):
    sessions: list[object] = []
    delete_calls = _stub_chain(monkeypatch, sessions)

    resp = client.request("DELETE", _PATH, json={"delete_all": True})

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    assert body["data"] == {"deleted": 2}
    assert set(delete_calls[0]["ids"]) == {"doc1", "doc2"}
    _assert_sync_facade(sessions)


def test_delete_rejects_documents_of_another_dataset(client, monkeypatch):
    """归属校验：数据集里没有的文档 ID 一律拒绝，且不触达删除逻辑。"""
    sessions: list[object] = []
    delete_calls = _stub_chain(monkeypatch, sessions)

    resp = client.request("DELETE", _PATH, json={"ids": ["doc1", "someone-else"]})

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == int(RetCode.DATA_ERROR)
    assert "do not belong to dataset kb1" in body["message"]
    assert "someone-else" in body["message"]
    assert delete_calls == []
    _assert_sync_facade(sessions)


def test_delete_deduplicates_ids(client, monkeypatch):
    """重复 ID 只下发一次，避免第二次删除报 not found 把整批判失败。"""
    sessions: list[object] = []
    delete_calls = _stub_chain(monkeypatch, sessions)

    resp = client.request("DELETE", _PATH, json={"ids": ["doc1", "doc1", "doc2"]})

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    assert body["data"] == {"deleted": 2}
    assert sorted(delete_calls[0]["ids"]) == ["doc1", "doc2"]
    _assert_sync_facade(sessions)


def test_delete_requires_ids_or_delete_all(client, monkeypatch):
    sessions: list[object] = []
    delete_calls = _stub_chain(monkeypatch, sessions)

    resp = client.request("DELETE", _PATH, json={})

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == int(RetCode.DATA_ERROR)
    assert "should either provide doc ids or set delete_all(true)" in body["message"]
    assert delete_calls == []


def test_delete_rejects_ids_together_with_delete_all(client, monkeypatch):
    sessions: list[object] = []
    delete_calls = _stub_chain(monkeypatch, sessions)

    resp = client.request("DELETE", _PATH, json={"ids": ["doc1"], "delete_all": True})

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == int(RetCode.DATA_ERROR)
    assert "should not provide both doc ids and delete_all(true)" in body["message"]
    assert delete_calls == []


def test_delete_denies_inaccessible_dataset(client, monkeypatch):
    sessions: list[object] = []
    delete_calls = _stub_chain(monkeypatch, sessions, accessible=False)

    resp = client.request("DELETE", _PATH, json={"ids": ["doc1"]})

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == int(RetCode.DATA_ERROR)
    assert body["message"] == "You don't own the dataset kb1."
    assert delete_calls == []
    _assert_sync_facade(sessions)


def test_delete_surfaces_service_errors(client, monkeypatch):
    sessions: list[object] = []
    _stub_chain(monkeypatch, sessions, errors="Database error (Document removal)!")

    resp = client.request("DELETE", _PATH, json={"ids": ["doc1"]})

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == int(RetCode.DATA_ERROR)
    assert body["message"] == "Database error (Document removal)!"
    _assert_sync_facade(sessions)


def test_delete_route_has_pure_async_dependency_tree(client, route_dependency_calls):
    import api.apps as api_apps

    calls = route_dependency_calls(client.app, "DELETE", "/api/v1/datasets/{dataset_id}/documents")

    assert get_db not in calls
    assert current_tenant_id not in calls
    assert api_apps.manager not in calls
    assert async_current_tenant_id in calls
    assert get_async_db in calls
