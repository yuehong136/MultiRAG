"""connector RESTful API 契约测试（``/api/v1/connectors/*``）。

收编自 ``/v1/connector/*``（后者标 deprecated 留旧）。钉板：

- **租户校验**：按 id 定位的六条路由（详情/更新/删除/日志/resume/rebuild）都必须先过
  ``ConnectorService.accessible``。连接器的 ``config`` 里存着数据源 OAuth 凭证，此前
  这些路由一个校验都没有，任意登录用户拿到 id 就能读走别人的凭证；拒绝时不得落任何写。
- **新旧路径同权**：旧的 ``/v1/connector/*`` 走同一套 service，校验一并生效。
- **OAuth 回调两条路径都活**：``*_WEB_OAUTH_REDIRECT_URI`` 默认值仍指向旧路径且已注册
  在 Google/Box 后台，新增 RESTful 路径不能取代它。
- **写入面**：更新只接受 config/refresh_freq/prune_freq/timeout_secs 四个字段。
"""

import sys
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from api.apps.services import connector_api_service, connector_oauth_service
from api.db.services.connector_service import Connector2KbService, ConnectorService, SyncLogsService
from api.db.services.knowledgebase_service import KnowledgebaseService
from common.constants import RetCode, TaskStatus

CONNECTOR_ID = "conn-1"
_REST = f"/api/v1/connectors/{CONNECTOR_ID}"
_WEB = f"/v1/connector/{CONNECTOR_ID}"


def _route_module():
    return sys.modules["api.apps.restful_apis.connector"]


def _connector(**overrides):
    data = {"id": CONNECTOR_ID, "name": "drive", "source": "google_drive", "config": {"token": "secret"}}
    data.update(overrides)
    return SimpleNamespace(to_dict=lambda: dict(data), **data)


def _assert_sync_facade(sessions):
    assert sessions
    for s in sessions:
        assert isinstance(s, Session), f"同步 service 收到 {type(s).__name__}，应为 sqlalchemy.orm.Session"


@pytest.fixture(autouse=True)
def _no_settle_wait(monkeypatch):
    """写入后回读的 1 秒等待在测试里没有意义，置零。"""
    monkeypatch.setattr(connector_api_service, "WRITE_SETTLE_SECS", 0)


@pytest.fixture
def sessions():
    return []


def _stub_accessible(monkeypatch, sessions, allowed: bool):
    def _accessible(cls, s, connector_id, user_id):
        sessions.append(s)
        return allowed

    monkeypatch.setattr(ConnectorService, "accessible", classmethod(_accessible))


_DEFAULT = object()


def _stub_get(monkeypatch, sessions, connector=_DEFAULT):
    found = _connector() if connector is _DEFAULT else connector
    monkeypatch.setattr(ConnectorService, "get_by_id", classmethod(lambda cls, s, cid: sessions.append(s) or found))


# ---------------------------------------------------------------------------
# 租户校验（安全钉板）
# ---------------------------------------------------------------------------

_GUARDED_REST_CALLS = [
    ("get", _REST, None),
    ("patch", _REST, {"refresh_freq": 30}),
    ("delete", _REST, None),
    ("get", f"{_REST}/logs", None),
    ("post", f"{_REST}/resume", {"resume": True}),
    ("post", f"{_REST}/rebuild", {"kb_id": "kb-1"}),
]

_GUARDED_WEB_CALLS = [
    ("get", _WEB, None),
    ("get", f"{_WEB}/logs", None),
    ("put", f"{_WEB}/resume", {"resume": True}),
    ("put", f"{_WEB}/rebuild", {"kb_id": "kb-1"}),
    ("post", f"{_WEB}/rm", None),
    ("post", "/v1/connector/set", {"id": CONNECTOR_ID, "refresh_freq": 30}),
]


@pytest.mark.parametrize(("method", "path", "body"), _GUARDED_REST_CALLS + _GUARDED_WEB_CALLS)
def test_by_id_routes_reject_foreign_connectors(client, monkeypatch, sessions, method, path, body):
    """别人的连接器一律拒绝，且不得触达任何读写。"""
    _stub_accessible(monkeypatch, sessions, False)
    touched: list[str] = []
    for name in ("get_by_id", "update_by_id", "delete_by_id", "resume", "rebuild", "list"):
        monkeypatch.setattr(ConnectorService, name, classmethod(lambda cls, *a, _n=name, **kw: touched.append(_n)))
    monkeypatch.setattr(SyncLogsService, "list_sync_tasks", classmethod(lambda cls, *a, **kw: touched.append("logs")))

    resp = client.request(method.upper(), path, json=body)

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"retcode": RetCode.AUTHENTICATION_ERROR, "retmsg": "No authorization.", "data": False}, resp.text
    assert touched == [], f"拒绝路径上不应触达 service：{touched}"
    _assert_sync_facade(sessions)


# ---------------------------------------------------------------------------
# CRUD 契约
# ---------------------------------------------------------------------------


def test_list_is_scoped_to_the_caller(client, monkeypatch, sessions):
    seen: list[str] = []

    def _list(cls, s, tenant_id):
        sessions.append(s)
        seen.append(tenant_id)
        return [{"id": CONNECTOR_ID}]

    monkeypatch.setattr(ConnectorService, "list", classmethod(_list))

    resp = client.get("/api/v1/connectors")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["retcode"] == RetCode.SUCCESS, body
    assert body["data"] == [{"id": CONNECTOR_ID}], body
    assert seen == ["user-unit"], seen
    _assert_sync_facade(sessions)


def test_create_persists_owner_and_rereads(client, monkeypatch, sessions):
    inserted: list[dict] = []
    monkeypatch.setattr(ConnectorService, "insert", classmethod(lambda cls, s, **kw: sessions.append(s) or inserted.append(kw)))
    _stub_get(monkeypatch, sessions)

    resp = client.post("/api/v1/connectors", json={"name": "drive", "source": "google_drive", "config": {"token": "x"}})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["retcode"] == RetCode.SUCCESS, body
    assert body["data"]["id"] == CONNECTOR_ID, body
    assert inserted[0]["tenant_id"] == "user-unit", inserted
    assert inserted[0]["status"] == TaskStatus.SCHEDULE, inserted
    _assert_sync_facade(sessions)


def test_create_requires_name_and_source(client):
    assert client.post("/api/v1/connectors", json={"source": "google_drive"}).status_code == 422
    assert client.post("/api/v1/connectors", json={"name": "drive"}).status_code == 422


def test_update_only_writes_the_four_schedule_fields(client, monkeypatch, sessions):
    updates: list[dict] = []
    _stub_accessible(monkeypatch, sessions, True)
    _stub_get(monkeypatch, sessions)
    monkeypatch.setattr(ConnectorService, "update_by_id", classmethod(lambda cls, s, cid, payload: sessions.append(s) or updates.append(payload)))

    resp = client.patch(_REST, json={"refresh_freq": 30, "timeout_secs": 90, "config": {"token": "y"}})

    assert resp.status_code == 200, resp.text
    assert resp.json()["retcode"] == RetCode.SUCCESS, resp.text
    assert updates == [{"config": {"token": "y"}, "refresh_freq": 30, "timeout_secs": 90}], updates
    _assert_sync_facade(sessions)


def test_update_reports_missing_connector(client, monkeypatch, sessions):
    _stub_accessible(monkeypatch, sessions, True)
    _stub_get(monkeypatch, sessions, connector=None)

    resp = client.patch(_REST, json={"refresh_freq": 30})

    body = resp.json()
    assert body["retcode"] == RetCode.DATA_ERROR, body
    assert body["retmsg"] == "Can't find this Connector!", body


def test_delete_cancels_tasks_before_removing(client, monkeypatch, sessions):
    calls: list[str] = []
    _stub_accessible(monkeypatch, sessions, True)
    monkeypatch.setattr(ConnectorService, "resume", classmethod(lambda cls, s, cid, status: sessions.append(s) or calls.append(f"resume:{status}")))
    monkeypatch.setattr(ConnectorService, "delete_by_id", classmethod(lambda cls, s, cid: sessions.append(s) or calls.append("delete")))

    resp = client.delete(_REST)

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"retcode": RetCode.SUCCESS, "retmsg": "success", "data": True}
    assert calls == [f"resume:{TaskStatus.CANCEL}", "delete"], calls
    _assert_sync_facade(sessions)


def test_resume_maps_the_flag_to_task_status(client, monkeypatch, sessions):
    statuses: list[str] = []
    _stub_accessible(monkeypatch, sessions, True)
    monkeypatch.setattr(ConnectorService, "resume", classmethod(lambda cls, s, cid, status: sessions.append(s) or statuses.append(status)))

    assert client.post(f"{_REST}/resume", json={"resume": True}).status_code == 200
    assert client.post(f"{_REST}/resume", json={"resume": False}).status_code == 200
    assert statuses == [TaskStatus.SCHEDULE, TaskStatus.CANCEL], statuses


def test_rebuild_surfaces_service_errors(client, monkeypatch, sessions):
    _stub_accessible(monkeypatch, sessions, True)
    monkeypatch.setattr(ConnectorService, "rebuild", classmethod(lambda cls, s, cid, kb_id, tenant_id: sessions.append(s) or "boom"))

    resp = client.post(f"{_REST}/rebuild", json={"kb_id": "kb-1"})

    body = resp.json()
    assert body["retcode"] == RetCode.SERVER_ERROR, body
    assert body["retmsg"] == "boom", body


def test_logs_are_paginated(client, monkeypatch, sessions):
    seen: list[tuple] = []

    def _list_sync_tasks(cls, s, connector_id, page, page_size):
        sessions.append(s)
        seen.append((connector_id, page, page_size))
        return [{"id": "log-1"}], 1

    _stub_accessible(monkeypatch, sessions, True)
    monkeypatch.setattr(SyncLogsService, "list_sync_tasks", classmethod(_list_sync_tasks))

    resp = client.get(f"{_REST}/logs", params={"page": 2, "page_size": 5})

    assert resp.status_code == 200, resp.text
    assert resp.json()["data"] == {"total": 1, "logs": [{"id": "log-1"}]}, resp.text
    assert seen == [(CONNECTOR_ID, 2, 5)], seen


# ---------------------------------------------------------------------------
# 路由形态
# ---------------------------------------------------------------------------


def test_link_route_is_gone_on_both_surfaces(client):
    """上游 #11075 就地把 /link 换成了 /rebuild，我方当时漏删；关联知识库由数据集更新端点承接。"""
    assert client.post(f"{_REST}/link", json={"kb_ids": ["kb-1"]}).status_code in (404, 405)
    assert client.post(f"{_WEB}/link", json={"kb_ids": ["kb-1"]}).status_code in (404, 405)
    assert not hasattr(Connector2KbService, "link_kb")


def test_oauth_callback_answers_on_both_paths(client, monkeypatch):
    """默认 redirect_uri 仍指向 /v1/connector/...，新旧路径必须都能收回调。"""
    monkeypatch.setattr(connector_oauth_service, "handle_google_callback", lambda *a, **kw: ("flow-1", False, "Missing OAuth state parameter."))

    for path in ("/api/v1/connectors/google-drive/oauth/web/callback", "/v1/connector/google-drive/oauth/web/callback"):
        resp = client.get(path, params={"state": "flow-1"})
        assert resp.status_code == 200, path
        assert "Missing OAuth state parameter." in resp.text, path


def test_oauth_start_rejects_unknown_source(client, monkeypatch):
    monkeypatch.setattr(connector_oauth_service, "REDIS_CONN", SimpleNamespace(set_obj=lambda *a, **kw: None))

    resp = client.post("/api/v1/connectors/google/oauth/web/start", params={"source": "dropbox"}, json={"credentials": "{}"})

    body = resp.json()
    assert body["retcode"] == RetCode.ARGUMENT_ERROR, body
    assert body["retmsg"] == "Invalid Google OAuth type.", body


# ---------------------------------------------------------------------------
# 连接器 ↔ 知识库关联（承接前端 LinkDataSource 的逐条勾选交互）
# ---------------------------------------------------------------------------

_DATASET_LINK = f"/api/v1/datasets/kb-1/connectors/{CONNECTOR_ID}"


def _stub_kb_accessible(monkeypatch, sessions, allowed: bool):
    monkeypatch.setattr(KnowledgebaseService, "accessible", classmethod(lambda cls, s, kb_id, user_id: sessions.append(s) or allowed))


def test_list_dataset_connectors(client, monkeypatch, sessions):
    _stub_kb_accessible(monkeypatch, sessions, True)
    monkeypatch.setattr(
        Connector2KbService,
        "list_connectors",
        classmethod(lambda cls, s, kb_id: sessions.append(s) or [{"id": CONNECTOR_ID, "auto_parse": "1"}]),
    )

    resp = client.get("/api/v1/datasets/kb-1/connectors")

    assert resp.status_code == 200, resp.text
    assert resp.json()["data"] == [{"id": CONNECTOR_ID, "auto_parse": "1"}], resp.text
    _assert_sync_facade(sessions)


def test_link_maps_auto_parse_to_the_column_flag(client, monkeypatch, sessions):
    linked: list[tuple] = []
    _stub_kb_accessible(monkeypatch, sessions, True)
    _stub_accessible(monkeypatch, sessions, True)
    monkeypatch.setattr(
        Connector2KbService,
        "link_connector",
        classmethod(lambda cls, s, kb_id, conn_id, auto_parse="1": sessions.append(s) or linked.append((kb_id, conn_id, auto_parse))),
    )

    assert client.put(_DATASET_LINK, json={"auto_parse": True}).status_code == 200
    assert client.put(_DATASET_LINK, json={"auto_parse": False}).status_code == 200
    assert client.put(_DATASET_LINK, json={}).status_code == 200  # 默认开启

    assert linked == [("kb-1", CONNECTOR_ID, "1"), ("kb-1", CONNECTOR_ID, "0"), ("kb-1", CONNECTOR_ID, "1")], linked
    _assert_sync_facade(sessions)


def test_unlink_removes_the_pair(client, monkeypatch, sessions):
    unlinked: list[tuple] = []
    _stub_kb_accessible(monkeypatch, sessions, True)
    monkeypatch.setattr(
        Connector2KbService,
        "unlink_connector",
        classmethod(lambda cls, s, kb_id, conn_id: sessions.append(s) or unlinked.append((kb_id, conn_id))),
    )

    resp = client.delete(_DATASET_LINK)

    assert resp.status_code == 200, resp.text
    assert resp.json()["data"] is True, resp.text
    assert unlinked == [("kb-1", CONNECTOR_ID)], unlinked


def test_dataset_link_routes_require_dataset_access(client, monkeypatch, sessions):
    """够不到知识库就一律拒绝，且不落写。"""
    touched: list[str] = []
    _stub_kb_accessible(monkeypatch, sessions, False)
    _stub_accessible(monkeypatch, sessions, True)
    for name in ("list_connectors", "link_connector", "unlink_connector"):
        monkeypatch.setattr(Connector2KbService, name, classmethod(lambda cls, *a, _n=name, **kw: touched.append(_n)))

    assert client.get("/api/v1/datasets/kb-1/connectors").json()["retcode"] == RetCode.AUTHENTICATION_ERROR
    assert client.put(_DATASET_LINK, json={"auto_parse": True}).json()["retcode"] == RetCode.AUTHENTICATION_ERROR
    assert client.delete(_DATASET_LINK).json()["retcode"] == RetCode.AUTHENTICATION_ERROR
    assert touched == [], touched


def test_link_also_requires_access_to_the_connector(client, monkeypatch, sessions):
    """能改知识库不等于能挂别人的连接器（其 config 里带凭证）。"""
    touched: list[str] = []
    _stub_kb_accessible(monkeypatch, sessions, True)
    _stub_accessible(monkeypatch, sessions, False)
    monkeypatch.setattr(Connector2KbService, "link_connector", classmethod(lambda cls, *a, **kw: touched.append("link")))

    resp = client.put(_DATASET_LINK, json={"auto_parse": True})

    assert resp.json()["retcode"] == RetCode.AUTHENTICATION_ERROR, resp.text
    assert touched == [], touched
