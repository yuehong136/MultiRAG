"""Persisted memory task → Redis collect → real message store; controlled models."""

import json
from collections.abc import Iterator
from datetime import datetime
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.orm import Session

from api.db.db_models import Memory, Task
from api.db.joint_services import memory_message_service as mms
from api.db.services.llm_service import LLMBundle
from common import settings
from common.bootstrap import ensure_initialized
from core.svr import task_executor as worker
from core.utils.redis_conn import REDIS_CONN
from memory.services.messages import MessageService, index_name
from tests.support.resource_journal import ResourceJournal


class FixedEmbedding(LLMBundle):
    def __init__(self, db: Session, tenant_id: str, calls: list[str]) -> None:
        self.db = db
        self.tenant_id = tenant_id
        self.calls = calls

    def encode(self, texts: list[str]) -> tuple[list[list[float]], int]:
        self.calls.append(self.tenant_id)
        return [[1.0, 0.0] for _ in texts], 0


@pytest.fixture
def memory_resources(bootstrapped_engine: sa.Engine, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    ensure_initialized()
    journal = ResourceJournal("memory-task-tenant")
    tenant, foreign_tenant, mid, foreign_mid = (uuid4().hex for _ in range(4))
    queue = f"memory-task-{uuid4().hex}"
    redis_keys = [queue, f"{queue}:memory", f"memory_{mid}", f"memory_{foreign_mid}"]
    journal.event(
        "reserve-memory-resources",
        tenants=[tenant, foreign_tenant],
        memory_ids=[mid, foreign_mid],
        indexes=[index_name(tenant), index_name(foreign_tenant)],
        redis_keys=redis_keys,
        consumer_group=worker.SVR_CONSUMER_GROUP_NAME,
    )
    calls: list[str] = []
    increment = REDIS_CONN.generate_auto_increment_id
    monkeypatch.setattr(REDIS_CONN, "generate_auto_increment_id", lambda **kw: increment(key_prefix=queue, **kw))
    monkeypatch.setattr(settings, "get_svr_queue_name", lambda priority: queue)
    monkeypatch.setattr(settings, "get_svr_queue_names", lambda: [queue])
    monkeypatch.setattr(worker, "UNACKED_ITERATOR", iter([]))
    monkeypatch.setattr(mms, "_embedding_bundle_for", lambda db, memory: FixedEmbedding(db, memory.tenant_id, calls))
    try:
        with Session(bootstrapped_engine) as db:
            db.add_all(
                [
                    Memory(id=mid, tenant_id=tenant, name="scratch", memory_type=3, llm_id="controlled", embd_id="controlled"),
                    Memory(id=foreign_mid, tenant_id=foreign_tenant, name="scratch-foreign", memory_type=3, llm_id="controlled", embd_id="controlled"),
                ]
            )
            db.commit()
        yield {"tenant": tenant, "foreign_tenant": foreign_tenant, "mid": mid, "foreign_mid": foreign_mid, "queue": queue, "calls": calls, "journal": journal}
    finally:
        for owner, memory_id in [(tenant, mid), (foreign_tenant, foreign_mid)]:
            MessageService.delete_index(owner, memory_id)
            assert not MessageService.has_index(owner, memory_id)
        REDIS_CONN.REDIS.delete(*redis_keys)
        assert not REDIS_CONN.REDIS.exists(*redis_keys)
        with Session(bootstrapped_engine) as db:
            db.execute(sa.delete(Task).where(Task.doc_id.in_([mid, foreign_mid])))
            db.execute(sa.delete(Memory).where(Memory.id.in_([mid, foreign_mid])))
            db.commit()
        with Session(bootstrapped_engine) as reader:
            assert reader.scalar(sa.select(sa.func.count()).select_from(Memory).where(Memory.id.in_([mid, foreign_mid]))) == 0
            assert reader.scalar(sa.select(sa.func.count()).select_from(Task).where(Task.doc_id.in_([mid, foreign_mid]))) == 0
        journal.event("memory-cleanup-readback", redis_keys_absent=True, memory_indexes_absent=True, sql_rows_absent=True)


async def test_enqueue_collect_execute_and_reject_forgery(
    bootstrapped_engine: sa.Engine, bootstrapped_async_engine: AsyncEngine, memory_resources: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    env = memory_resources
    extracts: list[str] = []

    async def extract(db: Session, tenant_id: str, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        extracts.append(tenant_id)
        return [{"message_type": "semantic", "content": "controlled", "valid_at": "2026-10-30 00:00:00", "invalid_at": None}]

    monkeypatch.setattr(mms, "extract_by_llm", extract)
    async with AsyncSession(bootstrapped_async_engine, expire_on_commit=False) as db:
        assert await mms.queue_save_to_memory_task(
            db, [env["mid"]], {"user_id": "subject", "tenant_id": "forged", "agent_id": "agent", "session_id": "session", "user_input": "controlled", "agent_response": "controlled"}
        ) == (True, "All add to task.")
        await db.commit()
    messages = REDIS_CONN.REDIS.xrange(env["queue"])
    assert len(messages) == 1
    queued = json.loads(messages[0][1]["message"])
    assert queued["tenant_id"] == env["tenant"]
    with Session(bootstrapped_engine) as reader:
        persisted = reader.get(Task, queued["id"])
        assert isinstance(persisted.begin_at, datetime)
        assert persisted.doc_id == env["mid"] and persisted.digest == str(queued["source_id"])
    raw = MessageService.get_by_message_id(env["mid"], queued["source_id"], env["tenant"])
    assert raw["user_id"] == "subject"
    with Session(bootstrapped_engine) as db:
        redis_msg, task = await worker.collect(db)
        assert task["tenant_id"] == env["tenant"]
        assert (await mms.handle_save_to_memory_task(db, task))[0] is True
        db.commit()
        redis_msg.ack()
    with Session(bootstrapped_engine) as reader:
        assert reader.get(Task, queued["id"]).progress == 1
        assert reader.get(Task, queued["id"]).process_duration >= 0
    stored = MessageService.get_recent_messages([env["tenant"]], [env["mid"]], "agent", "session", 10)
    assert len(stored) == 2 and {row["user_id"] for row in stored} == {"subject"}
    assert extracts == [env["tenant"]] and env["calls"] == [env["tenant"], env["tenant"]]
    before_calls = list(env["calls"])
    before_counter = REDIS_CONN.REDIS.get(f"{env['queue']}:memory")
    before_size = REDIS_CONN.REDIS.get(f"memory_{env['mid']}")
    for change in [{"tenant_id": None}, {"tenant_id": "foreign"}, {"memory_id": env["foreign_mid"]}, {"memory_id": "missing"}, {"source_id": queued["source_id"] + 1}]:
        poisoned = queued | change
        assert REDIS_CONN.queue_product(env["queue"], poisoned)
        with Session(bootstrapped_engine) as db:
            assert await worker.collect(db) == (None, None)
            assert (await mms.handle_save_to_memory_task(db, poisoned))[0] is False
            db.commit()
        assert env["calls"] == before_calls and extracts == [env["tenant"]]
        assert REDIS_CONN.REDIS.get(f"{env['queue']}:memory") == before_counter
        assert REDIS_CONN.REDIS.get(f"memory_{env['mid']}") == before_size
        assert len(MessageService.get_recent_messages([env["tenant"]], [env["mid"]], "agent", "session", 10)) == 2
        assert not MessageService.has_index(env["foreign_tenant"], env["foreign_mid"])
    with Session(bootstrapped_engine) as reader:
        persisted = reader.get(Task, queued["id"])
        assert persisted.progress == 1
    assert REDIS_CONN.REDIS.xpending(env["queue"], worker.SVR_CONSUMER_GROUP_NAME)["pending"] == 0
    env["journal"].event("memory-chain-readback", tenant_matches_db=True, raw_and_extracted_count=2, denied_variants=5, denied_model_and_storage_effects=0, queue_pending=0)


def test_configuration_equivalence_and_occupied_change_sql(bootstrapped_engine: sa.Engine, memory_resources: dict[str, Any]) -> None:
    from api.apps.services import memory_api_service as api
    from api.db.db_models import TenantLLM
    from common.exceptions import ArgumentException

    env = memory_resources
    model_ids = [7001, 7002, 7003]
    env["journal"].event("reserve-model-rows", ids=model_ids, database=str(bootstrapped_engine.url.database))
    try:
        with Session(bootstrapped_engine) as db:
            db.add_all(
                [
                    TenantLLM(id=model_ids[0], tenant_id=env["tenant"], llm_name="canonical", llm_factory="controlled", mdl_type="embedding", api_key=""),
                    TenantLLM(id=model_ids[1], tenant_id=env["tenant"], llm_name="changed", llm_factory="controlled", mdl_type="embedding", api_key=""),
                    TenantLLM(id=model_ids[2], tenant_id=env["foreign_tenant"], llm_name="foreign", llm_factory="controlled", mdl_type="embedding", api_key=""),
                ]
            )
            memory = db.get(Memory, env["mid"])
            memory.embd_id = "canonical"
            memory.tenant_embd_id = model_ids[0]
            db.commit()
        REDIS_CONN.REDIS.set(f"memory_{env['mid']}", 100)
        with Session(bootstrapped_engine) as reader:
            before = reader.get(Memory, env["mid"]).to_dict()
        for update in [{"embd_id": " 7001 "}, {"memory_type": ["semantic", " RAW ", "raw"]}, {"embd_id": "canonical@controlled", "memory_type": ["raw", "semantic"]}]:
            with Session(bootstrapped_engine) as db:
                assert api.update_memory(db, env["mid"], update, user_id=env["tenant"])[0]
                db.commit()
            with Session(bootstrapped_engine) as reader:
                assert reader.get(Memory, env["mid"]).to_dict() == before
        for update in [{"embd_id": "7002"}, {"memory_type": ["raw"]}, {"embd_id": "7003"}]:
            with Session(bootstrapped_engine) as db, pytest.raises(ArgumentException):
                api.update_memory(db, env["mid"], update, user_id=env["tenant"])
            with Session(bootstrapped_engine) as reader:
                assert reader.get(Memory, env["mid"]).to_dict() == before
        REDIS_CONN.REDIS.set(f"memory_{env['mid']}", 0)
        with Session(bootstrapped_engine) as db:
            assert api.update_memory(db, env["mid"], {"embd_id": "7002", "description": "changed"}, user_id=env["tenant"])[0]
            db.commit()
        with Session(bootstrapped_engine) as reader:
            memory = reader.get(Memory, env["mid"])
            assert memory.tenant_embd_id == 7002 and memory.description == "changed"
        env["journal"].event("memory-config-readback", no_op_cases=3, occupied_or_foreign_rejections=3, empty_memory_change_persisted=True)
    finally:
        with Session(bootstrapped_engine) as db:
            db.execute(sa.delete(TenantLLM).where(TenantLLM.id.in_(model_ids)))
            db.commit()
        with Session(bootstrapped_engine) as reader:
            assert reader.scalar(sa.select(sa.func.count()).select_from(TenantLLM).where(TenantLLM.id.in_(model_ids))) == 0
        env["journal"].event("model-rows-cleanup-readback", ids=model_ids, absent=True)


@pytest.mark.parametrize("begin_at", [None, datetime(2026, 10, 29)])
def test_progress_duration_with_optional_begin_at(bootstrapped_engine: sa.Engine, memory_resources: dict[str, Any], begin_at: datetime | None) -> None:
    from api.db.services.task_service import TaskService

    env = memory_resources
    task_id = uuid4().hex
    env["journal"].event("reserve-task-row", task_id=task_id, memory_id=env["mid"])
    with Session(bootstrapped_engine) as db:
        db.add(Task(id=task_id, doc_id=env["mid"], task_type="memory", progress=0, begin_at=begin_at, process_duration=0))
        db.commit()
    with Session(bootstrapped_engine) as db:
        TaskService.update_progress(db, task_id, {"progress": 0.5})
        db.commit()
    with Session(bootstrapped_engine) as reader:
        task = reader.get(Task, task_id)
        assert task.progress == 0.5
        assert task.process_duration > 0 if begin_at is not None else task.process_duration == 0
    env["journal"].event("task-duration-readback", task_id=task_id, begin_at_present=begin_at is not None, progress=0.5)
