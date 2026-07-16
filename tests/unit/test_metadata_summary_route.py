"""document RESTful 元数据汇总路由契约测试。

GET /api/v1/datasets/{id}/metadata/summary 走真实 ``api.apps.app`` 的 HTTP 契约式；
锁三件事：doc_ids 逗号分隔 query 参数的解析、权限拒绝 envelope、
路由依赖树纯 async（AsyncSession + async_current_tenant_id）。
service 桩记录并断言收到 ``sqlalchemy.orm.Session``（run_sync 同步门面）。
"""

from sqlalchemy.orm import Session

from api.db.db_models import get_async_db, get_db
from api.db.services.doc_metadata_service import DocMetadataService
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.utils.api_utils import async_current_tenant_id, current_tenant_id
from common.constants import RetCode

_PATH = "/api/v1/datasets/kb1/metadata/summary"

_SUMMARY = {"author": [["alice", 3], ["bob", 1]]}


def _stub_happy_chain(monkeypatch, sessions, seen_doc_ids):
    monkeypatch.setattr(KnowledgebaseService, "accessible", classmethod(lambda cls, s, kb_id, user_id: sessions.append(s) or True))
    monkeypatch.setattr(
        DocMetadataService,
        "get_metadata_summary",
        classmethod(lambda cls, s, kb_id, doc_ids=None: sessions.append(s) or seen_doc_ids.append(doc_ids) or _SUMMARY),
    )


def _assert_sync_facade(sessions):
    assert sessions
    for s in sessions:
        assert isinstance(s, Session), f"同步 service 收到 {type(s).__name__}，应为 sqlalchemy.orm.Session"


def test_metadata_summary_returns_summary_without_doc_ids(client, monkeypatch):
    sessions: list[object] = []
    seen_doc_ids: list[object] = []
    _stub_happy_chain(monkeypatch, sessions, seen_doc_ids)

    resp = client.get(_PATH)

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    assert body["data"] == {"summary": _SUMMARY}
    assert seen_doc_ids == [None]
    _assert_sync_facade(sessions)


def test_metadata_summary_parses_comma_separated_doc_ids(client, monkeypatch):
    sessions: list[object] = []
    seen_doc_ids: list[object] = []
    _stub_happy_chain(monkeypatch, sessions, seen_doc_ids)

    resp = client.get(_PATH, params={"doc_ids": "d1,d2"})

    assert resp.status_code == 200
    assert resp.json()["code"] == 0
    assert seen_doc_ids == [["d1", "d2"]]
    _assert_sync_facade(sessions)


def test_metadata_summary_denies_inaccessible_dataset(client, monkeypatch):
    sessions: list[object] = []
    monkeypatch.setattr(KnowledgebaseService, "accessible", classmethod(lambda cls, s, kb_id, user_id: sessions.append(s) or False))

    resp = client.get(_PATH)

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == int(RetCode.DATA_ERROR)
    assert body["message"] == "You don't own the dataset kb1."
    _assert_sync_facade(sessions)


def test_metadata_summary_route_has_pure_async_dependency_tree(client, route_dependency_calls):
    import api.apps as api_apps

    calls = route_dependency_calls(client.app, "GET", "/api/v1/datasets/{dataset_id}/metadata/summary")

    assert get_db not in calls
    assert current_tenant_id not in calls
    assert api_apps.manager not in calls
    assert async_current_tenant_id in calls
    assert get_async_db in calls
