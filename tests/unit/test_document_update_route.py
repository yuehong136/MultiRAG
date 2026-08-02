"""document RESTful 更新路由契约测试（Phase 2.5 批次 1：AsyncSession 收口）。

PATCH /api/v1/datasets/{id}/documents/{id} 走真实 ``api.apps.app`` 的 HTTP 契约式；
整个更新体运行在单一 run_sync 回调内——service 桩保留真实类型契约（记录并断言
收到 ``sqlalchemy.orm.Session``），各分支 envelope 在回调内产生后跨 greenlet 返回。
service 层纯逻辑测试见 test_document_update_api_parity.py，此处只锁路由行为。
"""

from types import SimpleNamespace

from sqlalchemy.orm import Session

from api.apps.services import document_api_service
from api.db.db_models import get_async_db, get_db
from api.db.services.document_service import DocumentService
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.utils.api_utils import async_current_tenant_id, current_tenant_id
from common.constants import RetCode

_PATH = "/api/v1/datasets/kb1/documents/doc1"


def _stub_happy_chain(monkeypatch, sessions):
    """铺满主链路桩：kb 存在、有权限、文档属于数据集、字段校验通过。"""
    kb = SimpleNamespace(id="kb1", tenant_id="tenant-unit")
    doc = SimpleNamespace(id="doc1", name="old.pdf", kb_id="kb1")
    monkeypatch.setattr(KnowledgebaseService, "get_by_id", classmethod(lambda cls, s, kid: sessions.append(s) or kb))
    monkeypatch.setattr(document_api_service, "can_update_dataset", lambda s, tid, k: sessions.append(s) or True)
    monkeypatch.setattr(DocumentService, "query", classmethod(lambda cls, s, **kw: sessions.append(s) or [doc]))
    monkeypatch.setattr(document_api_service, "validate_document_update_fields", lambda s, r, d, rq: sessions.append(s) or (None, None))
    monkeypatch.setattr(DocumentService, "get_by_id", classmethod(lambda cls, s, did: sessions.append(s) or doc))
    monkeypatch.setattr(document_api_service, "map_doc_keys", lambda s, d: {"id": d.id, "chunk_method": "naive"})
    return doc


def _assert_sync_facade(sessions):
    assert sessions
    for s in sessions:
        assert isinstance(s, Session), f"同步 service 收到 {type(s).__name__}，应为 sqlalchemy.orm.Session"


def test_update_document_renames_and_returns_mapped_doc(client, monkeypatch):
    sessions: list[object] = []
    _stub_happy_chain(monkeypatch, sessions)
    renamed: list[tuple[str, str]] = []
    monkeypatch.setattr(document_api_service, "update_document_name_only", lambda s, did, name: sessions.append(s) or renamed.append((did, name)) or None)

    resp = client.patch(_PATH, json={"name": "new.pdf"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    assert body["data"] == {"id": "doc1", "chunk_method": "naive"}
    assert renamed == [("doc1", "new.pdf")]
    _assert_sync_facade(sessions)


def test_update_document_missing_dataset(client, monkeypatch):
    sessions: list[object] = []
    monkeypatch.setattr(KnowledgebaseService, "get_by_id", classmethod(lambda cls, s, kid: sessions.append(s) or None))

    resp = client.patch(_PATH, json={"name": "new.pdf"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == int(RetCode.DATA_ERROR)
    assert body["message"] == "Can't find this dataset!"
    _assert_sync_facade(sessions)


def test_update_document_denies_without_permission(client, monkeypatch):
    sessions: list[object] = []
    kb = SimpleNamespace(id="kb1", tenant_id="tenant-unit")
    monkeypatch.setattr(KnowledgebaseService, "get_by_id", classmethod(lambda cls, s, kid: sessions.append(s) or kb))
    monkeypatch.setattr(document_api_service, "can_update_dataset", lambda s, tid, k: sessions.append(s) or False)

    resp = client.patch(_PATH, json={"name": "new.pdf"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == int(RetCode.AUTHENTICATION_ERROR)
    assert body["message"] == "No authorization."
    _assert_sync_facade(sessions)


def test_update_document_helper_error_response_passes_through(client, monkeypatch):
    """service helper 返回的错误 envelope（回调内产生的 Response）原样透出。"""
    sessions: list[object] = []
    _stub_happy_chain(monkeypatch, sessions)
    from api.utils.api_utils import get_error_data_result

    monkeypatch.setattr(document_api_service, "update_document_name_only", lambda s, did, name: get_error_data_result(retmsg="Database error (Document rename)!"))

    resp = client.patch(_PATH, json={"name": "new.pdf"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == int(RetCode.DATA_ERROR)
    assert body["message"] == "Database error (Document rename)!"
    _assert_sync_facade(sessions)


def test_update_document_put_alias_is_removed(client):
    resp = client.put(_PATH, json={"name": "new.pdf"})

    assert resp.status_code == 405


def test_update_document_route_has_pure_async_dependency_tree(client, route_dependency_calls):
    import api.apps as api_apps

    calls = route_dependency_calls(client.app, "PATCH", "/api/v1/datasets/{dataset_id}/documents/{document_id}")

    assert get_db not in calls
    assert current_tenant_id not in calls
    assert api_apps.manager not in calls
    assert async_current_tenant_id in calls
    assert get_async_db in calls
