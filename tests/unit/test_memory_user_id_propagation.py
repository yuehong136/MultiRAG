import asyncio
from types import SimpleNamespace

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from agent.component import message as message_module
from agent.component.message import Message
from agent.tools import retrieval as retrieval_module
from agent.tools.retrieval import Retrieval
from api.db.joint_services import memory_message_service as memory_message_service_module
from common.constants import MemoryType


class _DummyDBContext:
    def __enter__(self):
        return "db"

    def __exit__(self, exc_type, exc, tb):
        return False


class _DummyAsyncDBContext:
    """async_db_connection() 的替身：给出未绑定 AsyncSession（过 beartype，不连库）。"""

    async def __aenter__(self):
        return AsyncSession()

    async def __aexit__(self, exc_type, exc, tb):
        return False


def test_query_message_passes_user_id_filter_when_provided(monkeypatch) -> None:
    fake_memory = SimpleNamespace(tenant_id="tenant-1", tenant_embd_id=None, embd_id="embd-1")
    fake_db = Session()
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        memory_message_service_module.MemoryService,
        "get_by_ids",
        classmethod(lambda cls, _db, _memory_ids: [fake_memory]),
    )
    monkeypatch.setattr(
        memory_message_service_module,
        "get_model_config_by_type_and_name",
        lambda *_args, **_kwargs: {"model": "embd-1"},
    )
    monkeypatch.setattr(memory_message_service_module, "LLMBundle", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(memory_message_service_module, "get_vector", lambda *_args, **_kwargs: "dense")
    monkeypatch.setattr(
        memory_message_service_module.MsgTextQuery,
        "question",
        lambda self, _question, min_match=0.2: ("text", min_match),
    )

    def fake_search_message(cls, memory_ids, condition_dict, uid_list, match_expressions, top_n):
        captured["memory_ids"] = memory_ids
        captured["condition_dict"] = dict(condition_dict)
        captured["uid_list"] = uid_list
        captured["match_expressions"] = match_expressions
        captured["top_n"] = top_n
        return [{"message_id": 1}]

    monkeypatch.setattr(
        memory_message_service_module.MessageService,
        "search_message",
        classmethod(fake_search_message),
    )

    result = memory_message_service_module.query_message(
        db=fake_db,
        filter_dict={"memory_id": ["mem-1"], "user_id": "user-1"},
        params={
            "query": "hello",
            "similarity_threshold": 0.3,
            "keywords_similarity_weight": 0.7,
            "top_n": 3,
        },
    )

    assert result == [{"message_id": 1}]
    assert captured["memory_ids"] == ["mem-1"]
    assert captured["condition_dict"] == {"memory_id": ["mem-1"], "user_id": "user-1"}
    assert captured["uid_list"] == ["tenant-1"]
    assert captured["top_n"] == 3


def test_save_to_memory_persists_user_id_for_raw_and_extracted_messages(monkeypatch) -> None:
    fake_db = Session()
    fake_memory = SimpleNamespace(
        id="mem-1",
        tenant_id="tenant-1",
        llm_id="llm-1",
        tenant_llm_id=None,
        memory_type=MemoryType.SEMANTIC.value,
        temperature=0.1,
    )
    captured: dict[str, object] = {}
    ids = iter([101, 102])

    monkeypatch.setattr(
        memory_message_service_module.MemoryService,
        "get_by_memory_id",
        classmethod(lambda cls, _db, _memory_id: fake_memory),
    )

    async def fake_extract_by_llm(*_args, **_kwargs):
        return [
            {
                "message_type": "semantic",
                "content": "summary",
                "valid_at": "2026-01-01 00:00:00",
                "invalid_at": None,
            }
        ]

    monkeypatch.setattr(memory_message_service_module, "extract_by_llm", fake_extract_by_llm)
    monkeypatch.setattr(
        memory_message_service_module.REDIS_CONN,
        "generate_auto_increment_id",
        lambda namespace="memory": next(ids),
    )

    async def fake_embed_and_save(_db, _memory, message_list, task_id=None):
        captured["message_list"] = message_list
        captured["task_id"] = task_id
        return True, "ok"

    monkeypatch.setattr(memory_message_service_module, "embed_and_save", fake_embed_and_save)

    ok, msg = asyncio.run(
        memory_message_service_module.save_to_memory(
            db=fake_db,
            memory_id="mem-1",
            message_dict={
                "user_id": "user-1",
                "agent_id": "agent-1",
                "session_id": "session-1",
                "user_input": "hi",
                "agent_response": "hello",
            },
        )
    )

    assert ok is True
    assert msg == "ok"
    assert [message["user_id"] for message in captured["message_list"]] == ["user-1", "user-1"]


def test_queue_save_to_memory_task_persists_user_id_on_raw_message(monkeypatch) -> None:
    from sqlalchemy.ext.asyncio import AsyncSession

    fake_db = AsyncSession()
    fake_memory = SimpleNamespace(id="mem-1", tenant_id="tenant-1")
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        memory_message_service_module.MemoryService,
        "get_by_memory_id",
        classmethod(lambda cls, _db, _memory_id: fake_memory),
    )
    monkeypatch.setattr(
        memory_message_service_module.REDIS_CONN,
        "generate_auto_increment_id",
        lambda namespace="memory": 201,
    )
    monkeypatch.setattr(
        memory_message_service_module.REDIS_CONN,
        "queue_product",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(memory_message_service_module, "bulk_insert_into_db", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(memory_message_service_module, "_embedding_bundle_for", lambda _s, _m: SimpleNamespace(db="facade"))

    def fake_embed_and_save_messages(_embedding_model, _memory, message_list, _report):
        captured["message_list"] = message_list
        return True, "ok"

    monkeypatch.setattr(memory_message_service_module, "_embed_and_save_messages", fake_embed_and_save_messages)

    ok, msg = asyncio.run(
        memory_message_service_module.queue_save_to_memory_task(
            db=fake_db,
            memory_ids=["mem-1"],
            message_dict={
                "user_id": "user-1",
                "agent_id": "agent-1",
                "session_id": "session-1",
                "user_input": "hi",
                "agent_response": "hello",
            },
        )
    )

    assert ok is True
    assert msg == "All add to task."
    assert [message["user_id"] for message in captured["message_list"]] == ["user-1"]


def test_message_resolves_variable_user_id_before_saving_to_memory(monkeypatch) -> None:
    captured: dict[str, object] = {}
    canvas = SimpleNamespace(
        _id="agent-1",
        task_id="session-1",
        get_sys_query=lambda: "hi",
        get_variable_value=lambda ref: {"{sys.user_id}": "user-1", "sys.user_id": "user-1"}[ref],
    )
    param = SimpleNamespace(memory_ids=["mem-1"], user_id="{sys.user_id}")
    component = object.__new__(Message)
    component._canvas = canvas
    component._param = param

    async def fake_queue_save_to_memory_task(db, memory_ids, message_dict):
        captured["db"] = db
        captured["memory_ids"] = memory_ids
        captured["message_dict"] = message_dict
        return True, "ok"

    monkeypatch.setattr(message_module, "async_db_connection", lambda: _DummyAsyncDBContext())
    monkeypatch.setattr(message_module, "queue_save_to_memory_task", fake_queue_save_to_memory_task)

    ok, msg = asyncio.run(component._save_to_memory("hello"))

    assert ok is True
    assert msg == "ok"
    # 契约守门：queue_save_to_memory_task 收 AsyncSession——传同步 Session 会被 beartype 拒，
    # 且 Agent 的 Message 组件是它除 memory_api 外的唯一调用方（回归实锤，勿弱化此断言）
    assert isinstance(captured["db"], AsyncSession)
    assert captured["memory_ids"] == ["mem-1"]
    assert captured["message_dict"]["user_id"] == "user-1"


def test_retrieval_resolves_variable_user_id_before_querying_memory(monkeypatch) -> None:
    captured: dict[str, object] = {}
    canvas = SimpleNamespace(
        get_component_name=lambda _cpn_id: "",
        get_variable_value=lambda ref: {"{sys.user_id}": "user-1", "sys.user_id": "user-1"}[ref],
    )
    param = SimpleNamespace(
        memory_ids=["mem-1"],
        user_id="{sys.user_id}",
        similarity_threshold=0.3,
        keywords_similarity_weight=0.7,
        top_n=3,
        empty_response="",
        outputs={},
    )
    tool = object.__new__(Retrieval)
    tool._canvas = canvas
    tool._param = param

    fake_memory = SimpleNamespace(embd_id="embd-1")
    monkeypatch.setattr(retrieval_module, "db_connection", lambda: _DummyDBContext())
    monkeypatch.setattr(
        retrieval_module.MemoryService,
        "get_by_ids",
        classmethod(lambda cls, _db, _memory_ids: [fake_memory]),
    )
    monkeypatch.setattr(retrieval_module, "memory_prompt", lambda message_list, _limit: ["memory text"])

    def fake_query_message(db, filter_dict, params):
        captured["db"] = db
        captured["filter_dict"] = dict(filter_dict)
        captured["params"] = dict(params)
        return [{"content": "memory"}]

    monkeypatch.setattr(retrieval_module.memory_message_service, "query_message", fake_query_message)

    result = asyncio.run(tool._retrieve_memory("hello"))

    assert result == "memory text"
    assert tool.output("formalized_content") == "memory text"
    assert captured["db"] == "db"
    assert captured["filter_dict"] == {"memory_id": ["mem-1"], "user_id": "user-1"}
    assert captured["params"]["top_n"] == 3
