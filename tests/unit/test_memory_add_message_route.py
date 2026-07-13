"""memory add_message 链与路由契约（restful_apis AsyncSession 收口）。

queue_save_to_memory_task：DB 面 run_sync、Redis/embedding/doc-store 面工作线程、
LLMBundle facade 剥离（断言经变异验证）；embed_and_save 同步孪生的进度回调保真；
路由层 retcode 形状钉板。
"""

import threading
import types

import pytest

from api.apps.services import memory_api_service
from api.db.joint_services import memory_message_service as mms
from api.db.services.llm_service import LLMBundle


class _FakeBundle(LLMBundle):
    """继承真类过 beartype 返回校验；__init__ 不上桥、不触库。"""

    instances: list["_FakeBundle"] = []

    def __init__(self, db, tenant_id, model_config, **kwargs):
        self.db = db
        self.tenant_id = tenant_id
        type(self).instances.append(self)


def _fake_memory(**kw):
    from api.db.db_models import Memory

    defaults = {
        "id": "mem1",
        "tenant_id": "tenant-unit",
        "tenant_embd_id": 7,
        "embd_id": "emb",
        "memory_size": 1000,
        "forgetting_policy": "FIFO",
    }
    defaults.update(kw)
    return Memory(**defaults)  # 声明式构造不触库，detached 实例过 beartype


@pytest.fixture
def queue_stubs(monkeypatch):
    _FakeBundle.instances = []
    seen: dict[str, object] = {}

    from api.db.services.memory_service import MemoryService

    monkeypatch.setattr(MemoryService, "get_by_memory_id", classmethod(lambda cls, s, mid: _fake_memory()))
    monkeypatch.setattr(mms, "get_model_config_by_id", lambda s, mid: {"llm_name": "emb-m"})
    monkeypatch.setattr(mms, "LLMBundle", _FakeBundle)
    monkeypatch.setattr(mms, "REDIS_CONN", types.SimpleNamespace(generate_auto_increment_id=lambda namespace: 42, queue_product=lambda name, message: True))
    monkeypatch.setattr(mms, "settings", types.SimpleNamespace(get_svr_queue_name=lambda priority: "q0"))
    monkeypatch.setattr(mms, "bulk_insert_into_db", lambda s, model, rows, replace_on_conflict: seen.setdefault("task_rows", rows))

    def _fake_embed_and_save_messages(embedding_model, memory, message_list, report):
        seen["bundle_db_at_use"] = embedding_model.db
        seen["off_loop"] = threading.current_thread() is not threading.main_thread()
        seen["contents"] = [m["content"] for m in message_list]
        return True, "Message saved successfully."

    monkeypatch.setattr(mms, "_embed_and_save_messages", _fake_embed_and_save_messages)
    return seen


async def test_queue_save_strips_facade_and_offloads(async_db, queue_stubs):
    ok, msg = await mms.queue_save_to_memory_task(async_db, ["mem1"], {"agent_id": "a", "session_id": "s", "user_input": "hi", "agent_response": "yo"})

    assert (ok, msg) == (True, "All add to task.")
    # facade 剥离：run_sync 构造的 bundle 进入工作线程前必须 db=None（变异验证锚点）
    assert queue_stubs["bundle_db_at_use"] is None
    assert queue_stubs["off_loop"] is True
    assert queue_stubs["task_rows"][0]["doc_id"] == "mem1"
    assert queue_stubs["contents"] == ["User Input: hi\nAgent Response: yo"]


async def test_queue_save_reports_missing_memory(async_db, monkeypatch):
    from api.db.services.memory_service import MemoryService

    monkeypatch.setattr(MemoryService, "get_by_memory_id", classmethod(lambda cls, s, mid: None))

    ok, msg = await mms.queue_save_to_memory_task(async_db, ["ghost"], {"agent_id": "a", "session_id": "s"})

    assert ok is False
    assert "['ghost'] not found" in msg


async def test_queue_save_collects_embed_failures(async_db, queue_stubs, monkeypatch):
    monkeypatch.setattr(mms, "_embed_and_save_messages", lambda embedding_model, memory, message_list, report: (False, "boom"))

    ok, msg = await mms.queue_save_to_memory_task(async_db, ["mem1"], {"agent_id": "a", "session_id": "s"})

    assert ok is False
    assert "mem1" in msg and "boom" in msg


async def test_embed_and_save_sync_twin_reports_progress(db, monkeypatch):
    """task_executor 走的同步孪生：进度回调注入 TaskService.update_progress 保真。"""
    _FakeBundle.instances = []
    progress: list[float] = []

    from memory.services.messages import MessageService

    monkeypatch.setattr(mms, "get_model_config_by_id", lambda s, mid: {"llm_name": "emb-m"})
    monkeypatch.setattr(mms, "LLMBundle", _FakeBundle)
    monkeypatch.setattr(mms.TaskService, "update_progress", classmethod(lambda cls, s, tid, info: progress.append(info["progress"])))
    monkeypatch.setattr(_FakeBundle, "encode", lambda self, texts: ([[0.1]] * len(texts), 3), raising=False)
    monkeypatch.setattr(MessageService, "has_index", staticmethod(lambda tid, mid: True))
    monkeypatch.setattr(MessageService, "calculate_message_size", staticmethod(lambda m: 1))
    monkeypatch.setattr(MessageService, "insert_message", staticmethod(lambda msgs, tid, mid: []))
    monkeypatch.setattr(mms, "get_memory_size_cache", lambda tid, mid: 0)
    monkeypatch.setattr(mms, "increase_memory_size_cache", lambda mid, size: None)

    ok, msg = await mms.embed_and_save(db, _fake_memory(), [{"content": "c"}], task_id="t1")

    assert (ok, msg) == (True, "Message saved successfully.")
    assert progress == [0.65, 0.85, 0.95]


# ---------------------------------------------------------------------------
# 路由层（service 打桩，锁 retcode 形状）
# ---------------------------------------------------------------------------


_PAYLOAD = {"memory_id": "mem1", "agent_id": "a", "session_id": "s", "user_input": "hi", "agent_response": "yo"}


def test_add_message_route_success_shape(client, monkeypatch):
    async def _fake(db, memory_ids, message_dict):
        assert memory_ids == ["mem1"]
        return True, "All add to task."

    monkeypatch.setattr(memory_api_service, "add_message", _fake)

    resp = client.post("/api/v1/messages", json=_PAYLOAD)

    assert resp.status_code == 200
    body = resp.json()
    assert body["retcode"] == 0
    assert body["retmsg"] == "All add to task."


def test_add_message_route_failure_shape(client, monkeypatch):
    async def _fake(db, memory_ids, message_dict):
        return False, "Memory mem1 failed. Detail: boom"

    monkeypatch.setattr(memory_api_service, "add_message", _fake)

    resp = client.post("/api/v1/messages", json=_PAYLOAD)

    assert resp.status_code == 200
    body = resp.json()
    assert body["retcode"] != 0
    assert body["retmsg"].startswith("Some messages failed to add.")
