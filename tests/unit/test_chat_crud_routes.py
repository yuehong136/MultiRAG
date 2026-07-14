"""chat RESTful CRUD/tts 路由契约测试（Phase 2.5 批次 5：AsyncSession 收口）。

14 条 CRUD 路由整函数体收进单一 run_sync 回调（envelope 在回调内产生，ORM 不逸出）；
tts 按同文件 transcriptions 判例：配置与 LLMBundle 构造 run_sync + facade 剥离，
同步流式产物经 iterate_in_threadpool 逐项在线程池拉取。
list_chats/list_sessions/update_session 的 HTTP 契约测试在 test_chat_restful_api.py
（由直调形态迁移）；依赖树测试全量扫 /api/v1/chats* 前缀，覆盖现在与未来的全部路由。
"""

import sys
import threading
from types import SimpleNamespace

from fastapi.routing import APIRoute
from sqlalchemy.orm import Session

from api.db.db_models import get_db
from api.db.services.dialog_service import DialogService
from api.db.services.user_service import UserTenantService
from api.utils.api_utils import current_tenant_id
from common.constants import RetCode


class Obj(SimpleNamespace):
    def to_dict(self):
        return dict(self.__dict__)


def _record(records, s):
    records.append({"session": s, "off_loop": threading.current_thread() is not threading.main_thread()})


def _assert_sync_facade(records):
    assert records
    for r in records:
        assert isinstance(r["session"], Session), f"同步 service 收到 {type(r['session']).__name__}，应为 sqlalchemy.orm.Session"


def _route_module():
    return sys.modules["api.apps.restful_apis.chat"]


def test_chat_create_uses_sync_facade(client, monkeypatch):
    records: list[dict] = []

    def _prepare(s, tenant_id, req):
        _record(records, s)
        assert tenant_id == "tenant-unit"
        return True, {"id": "chat-new", "name": req["name"], "tenant_id": tenant_id}

    monkeypatch.setattr(_route_module(), "_prepare_create_payload", _prepare)
    monkeypatch.setattr(_route_module(), "build_chat_response", lambda s, chat: _record(records, s) or {"id": chat.id, "name": chat.name})
    monkeypatch.setattr(DialogService, "save", classmethod(lambda cls, s, **kw: _record(records, s) or Obj(**kw)))
    monkeypatch.setattr(DialogService, "get_by_id", classmethod(lambda cls, s, cid: _record(records, s) or Obj(id=cid, name="Support Bot")))

    resp = client.post("/api/v1/chats", json={"name": "Support Bot"})

    body = resp.json()
    assert body["code"] == 0
    assert body["data"] == {"id": "chat-new", "name": "Support Bot"}
    _assert_sync_facade(records)


def test_chat_get_denies_non_member(client, monkeypatch):
    records: list[dict] = []
    monkeypatch.setattr(UserTenantService, "query", classmethod(lambda cls, s, **kw: _record(records, s) or []))
    monkeypatch.setattr(DialogService, "query", classmethod(lambda cls, s, **kw: _record(records, s) or []))

    body = client.get("/api/v1/chats/chat-1").json()

    assert body["code"] == int(RetCode.AUTHENTICATION_ERROR)
    assert body["message"] == "No authorization."
    _assert_sync_facade(records)


def test_chat_delete_soft_deletes(client, monkeypatch):
    records: list[dict] = []
    monkeypatch.setattr(DialogService, "query", classmethod(lambda cls, s, **kw: _record(records, s) or [Obj(id="chat-1")]))
    updates: list[tuple] = []
    monkeypatch.setattr(DialogService, "update_by_id", classmethod(lambda cls, s, cid, values: _record(records, s) or updates.append((cid, values)) or 1))

    body = client.delete("/api/v1/chats/chat-1").json()

    assert body["code"] == 0
    assert body["data"] is True
    assert updates == [("chat-1", {"status": "0"})]  # 软删除置 INVALID
    _assert_sync_facade(records)


def test_chat_session_create_uses_prologue(client, monkeypatch):
    from api.db.services.conversation_service import ConversationService

    records: list[dict] = []
    monkeypatch.setattr(DialogService, "query", classmethod(lambda cls, s, **kw: _record(records, s) or [Obj(id="chat-1")]))
    monkeypatch.setattr(DialogService, "get_by_id", classmethod(lambda cls, s, cid: _record(records, s) or Obj(id=cid, prompt_config={"prologue": "Hi!"})))
    saved: list[dict] = []

    def _save(cls, s, **conv):
        _record(records, s)
        saved.append(conv)
        return Obj(**conv)

    monkeypatch.setattr(ConversationService, "save", classmethod(_save))
    monkeypatch.setattr(ConversationService, "get_by_id", classmethod(lambda cls, s, cid: _record(records, s) or Obj(**saved[0])))

    body = client.post("/api/v1/chats/chat-1/sessions", json={"name": " Demo "}).json()

    assert body["code"] == 0
    assert saved[0]["name"] == "Demo"
    assert saved[0]["message"] == [{"role": "assistant", "content": "Hi!"}]
    assert body["data"]["chat_id"] == "chat-1"
    _assert_sync_facade(records)


def test_chat_tts_streams_off_loop_with_stripped_bundle(client, monkeypatch):
    records: list[dict] = []
    monkeypatch.setattr(_route_module(), "get_tenant_default_model_by_type", lambda s, tid, t: _record(records, s) or {"llm_name": "tts-1"})

    stream_calls: list[dict] = []

    class _FakeBundle:
        def __init__(self, s, tenant_id, config):
            _record(records, s)
            self.db = s
            self.tenant_id = tenant_id
            self.config = config

        def tts(self, text):
            stream_calls.append({"text": text, "db": self.db, "off_loop": threading.current_thread() is not threading.main_thread()})
            yield b"chunk-" + text.encode()

    monkeypatch.setattr(_route_module(), "LLMBundle", _FakeBundle)

    resp = client.post("/api/v1/chats/tts", json={"text": "你好。世界"})

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("audio/mpeg")
    assert resp.content == b"chunk-\xe4\xbd\xa0\xe5\xa5\xbdchunk-\xe4\xb8\x96\xe7\x95\x8c"
    assert [c["text"] for c in stream_calls] == ["你好", "世界"]
    for call in stream_calls:
        assert call["db"] is None  # facade 已剥离，不随 bundle 逸出 greenlet
        assert call["off_loop"] is True  # 同步 TTS 流在线程池拉取
    _assert_sync_facade(records)


def test_all_chat_routes_have_pure_async_dependency_tree(client, route_dependency_calls):
    """全量扫 /api/v1/chats* 前缀：现在与未来的路由都不得回到同步轨。"""
    import api.apps as api_apps

    chat_routes = [(sorted(r.methods)[0], r.path) for r in client.app.routes if isinstance(r, APIRoute) and r.path.startswith("/api/v1/chats")]
    assert len(chat_routes) >= 20  # 文件当前 20 条路由全部在册

    for method, path in chat_routes:
        calls = route_dependency_calls(client.app, method, path)
        assert get_db not in calls, f"{method} {path} 依赖树含同步 get_db"
        assert current_tenant_id not in calls, f"{method} {path} 依赖树含同步 current_tenant_id"
        assert api_apps.manager not in calls, f"{method} {path} 依赖树含同步 manager"
