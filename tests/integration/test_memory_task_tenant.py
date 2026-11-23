"""Persisted memory task → Redis collect → real message store; controlled models."""

import asyncio
import json
import re
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session, sessionmaker

from api.db import db_models
from api.db.db_models import Memory, Task
from api.db.joint_services import memory_message_service as mms
from api.db.joint_services import memory_write_runtime
from api.db.memory_revision_models import MEMORY_REVISION_MODELS, MemoryPublicationOutbox
from api.db.memory_writer_models import MEMORY_WRITER_MODELS, MemoryWriterAdmission
from api.db.services.tenant_llm_service import TenantLLMService
from common import settings
from common.bootstrap import ensure_initialized
from core.svr import task_executor as worker
from core.utils.redis_conn import REDIS_CONN
from memory.revisions.domain import RevisionConflict
from memory.services.messages import MessageService, index_name
from tests.support.registered_model import register_model
from tests.support.resource_journal import ResourceJournal


@pytest.fixture
def memory_resources(bootstrapped_engine: sa.Engine, bootstrapped_async_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    ensure_initialized()
    journal = ResourceJournal("memory-task-tenant")
    tenant, foreign_tenant, mid, foreign_mid = (uuid4().hex for _ in range(4))
    queue = f"memory-task-{uuid4().hex}"
    counter = queue + ":coordinated-ids"
    redis_keys = [queue, f"{queue}:memory", counter, f"memory_{mid}", f"memory_{foreign_mid}"]
    journal.event(
        "reserve-memory-resources",
        database=bootstrapped_engine.url.database,
        tenants=[tenant, foreign_tenant],
        memory_ids=[mid, foreign_mid],
        indexes=[index_name(tenant), index_name(foreign_tenant)],
        redis_keys=redis_keys,
        consumer_group=worker.SVR_CONSUMER_GROUP_NAME,
    )
    env: dict[str, Any] = {
        "tenant": tenant,
        "foreign_tenant": foreign_tenant,
        "mid": mid,
        "foreign_mid": foreign_mid,
        "queue": queue,
        "counter": counter,
        "calls": [],
        "extracts": [],
        "references": [],
        "journal": journal,
    }
    sessions = sessionmaker(bootstrapped_engine, expire_on_commit=False)
    monkeypatch.setattr(db_models, "SessionLocal", sessions)
    asynchronous = create_async_engine(bootstrapped_engine.url, poolclass=sa.pool.NullPool)
    factory = async_sessionmaker(asynchronous, expire_on_commit=False)

    @asynccontextmanager
    async def runtime_db() -> AsyncIterator[AsyncSession]:
        async with factory() as db:
            yield db

    monkeypatch.setattr(memory_write_runtime, "async_db_connection", runtime_db)
    monkeypatch.setattr(memory_write_runtime, "_COUNTER_KEY", counter)
    monkeypatch.setattr(settings, "get_svr_queue_name", lambda priority: queue)
    monkeypatch.setattr(settings, "get_svr_queue_names", lambda: [queue])
    monkeypatch.setattr(worker, "UNACKED_ITERATOR", iter([]))

    class Embedding:
        def __init__(self, owner: str) -> None:
            self.owner = owner

        def encode(self, texts: list[str]) -> tuple[list[list[float]], int]:
            env["calls"].append(self.owner)
            if env.pop("fail_embedding_once", False):
                raise RuntimeError("controlled failure before extracted storage")
            return [[1.0, 0.0] for _ in texts], 0

        def close(self) -> None:
            pass

    class Chat:
        def __init__(self, owner: str) -> None:
            self.owner = owner

        async def async_chat(self, system: str, history: list[dict[str, Any]], gen_conf: dict[str, Any]) -> tuple[str, int]:
            env["extracts"].append(self.owner)
            env["references"].append(history[-1]["content"])
            return json.dumps(env.get("model_response", {"semantic": [{"content": "controlled", "valid_at": "2026-11-08 00:00:00", "invalid_at": None}]})), 0

        def close(self) -> None:
            pass

    def construct(cls: type[TenantLLMService], config: dict[str, Any], lang: str = "Chinese", **kwargs: Any) -> Any:
        assert config["tenant_id"] in {tenant, foreign_tenant} and re.fullmatch(r"[0-9a-f]{32}", config["id"])
        assert config["mdl_type"] in {"embedding", "chat"}
        return Embedding(config["tenant_id"]) if config["mdl_type"] == "embedding" else Chat(config["tenant_id"])

    monkeypatch.setattr(TenantLLMService, "model_instance_from_config", classmethod(construct))
    try:
        with Session(bootstrapped_engine) as db:
            for owner, memory_id in [(tenant, mid), (foreign_tenant, foreign_mid)]:
                identifiers = {}
                for kind in ("embedding", "chat"):
                    journal.event("reserve-model-row", tenant_id=owner, model_name="controlled-" + kind, model_type=kind)
                    model = register_model(db, tenant_id=owner, provider_name="controlled", model_name="controlled-" + kind, model_types=(kind,))
                    identifiers[kind] = model.id
                db.add(
                    Memory(
                        id=memory_id,
                        tenant_id=owner,
                        name="scratch",
                        memory_type=3,
                        llm_id=identifiers["chat"],
                        embd_id=identifiers["embedding"],
                        tenant_llm_id=identifiers["chat"],
                        tenant_embd_id=identifiers["embedding"],
                    )
                )
            db.commit()
        yield env
    finally:
        for owner, memory_id in [(tenant, mid), (foreign_tenant, foreign_mid)]:
            MessageService.delete_index(owner, memory_id)
            assert not settings.msgStoreConn.index_exists_strict(index_name(owner), memory_id)
        REDIS_CONN.REDIS.delete(*redis_keys)
        assert not REDIS_CONN.REDIS.exists(*redis_keys)
        with Session(bootstrapped_engine) as db:
            for model in (*MEMORY_WRITER_MODELS, *reversed(MEMORY_REVISION_MODELS)):
                db.execute(sa.delete(model).where(model.memory_id.in_([mid, foreign_mid])))
            db.execute(sa.delete(Task).where(Task.doc_id.in_([mid, foreign_mid])))
            db.execute(sa.delete(Memory).where(Memory.id.in_([mid, foreign_mid])))
            for owner in (tenant, foreign_tenant):
                TenantLLMService.delete_by_tenant_id(db, owner)
            db.commit()
        with Session(bootstrapped_engine) as reader:
            remaining = {
                model.__tablename__: reader.scalar(sa.select(sa.func.count()).select_from(model).where(model.memory_id.in_([mid, foreign_mid])))
                for model in (*MEMORY_WRITER_MODELS, *MEMORY_REVISION_MODELS)
            }
            assert not any(remaining.values()), remaining
            assert reader.scalar(sa.select(sa.func.count()).select_from(Memory).where(Memory.id.in_([mid, foreign_mid]))) == 0
            assert reader.scalar(sa.select(sa.func.count()).select_from(Task).where(Task.doc_id.in_([mid, foreign_mid]))) == 0
        asyncio.run(asynchronous.dispose())
        journal.event("memory-cleanup-readback", redis_keys_absent=True, memory_indexes_absent=True, sql_rows_absent=True, ledger_rows=remaining)


async def test_enqueue_collect_execute_and_reject_forgery(
    bootstrapped_engine: sa.Engine, bootstrapped_async_engine: AsyncEngine, memory_resources: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    env = memory_resources
    extracts = env["extracts"]
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

    @contextmanager
    def connection() -> Iterator[Session]:
        with Session(bootstrapped_engine) as db:
            yield db

    monkeypatch.setattr(worker, "db_connection", connection)
    await worker.handle_task()
    with Session(bootstrapped_engine) as reader:
        assert reader.get(Task, queued["id"]).progress == 1
        assert reader.get(Task, queued["id"]).process_duration >= 0
    assert extracts == [env["tenant"]] and env["calls"] == [env["tenant"], env["tenant"]]
    stored = MessageService.get_recent_page(env["tenant"], env["mid"], "agent", "session", 10)["messages"]
    assert len(stored) == 2 and {row["user_id"] for row in stored} == {"subject"}
    before_calls = list(env["calls"])
    before_counter = REDIS_CONN.REDIS.get(env["counter"])
    before_size = REDIS_CONN.REDIS.get(f"memory_{env['mid']}")
    for change in [{"tenant_id": None}, {"tenant_id": "foreign"}, {"memory_id": env["foreign_mid"]}, {"memory_id": "missing"}, {"source_id": queued["source_id"] + 1}]:
        poisoned = queued | change
        assert REDIS_CONN.queue_product(env["queue"], poisoned)
        with Session(bootstrapped_engine) as db:
            assert await worker.collect(db) == (None, None)
            with pytest.raises(RevisionConflict, match="extraction_task_binding_mismatch"):
                await mms.handle_save_to_memory_task(db, poisoned)
            db.commit()
        assert env["calls"] == before_calls and extracts == [env["tenant"]]
        assert REDIS_CONN.REDIS.get(env["counter"]) == before_counter
        assert REDIS_CONN.REDIS.get(f"memory_{env['mid']}") == before_size
        assert len(MessageService.get_recent_page(env["tenant"], env["mid"], "agent", "session", 10)["messages"]) == 2
        assert not MessageService.has_index(env["foreign_tenant"], env["foreign_mid"])
    with Session(bootstrapped_engine) as reader:
        persisted = reader.get(Task, queued["id"])
        assert persisted.progress == 1
    assert REDIS_CONN.REDIS.xpending(env["queue"], worker.SVR_CONSUMER_GROUP_NAME)["pending"] == 0
    env["journal"].event("memory-chain-readback", tenant_matches_db=True, raw_and_extracted_count=2, denied_variants=5, denied_model_and_storage_effects=0, queue_pending=0)


async def test_extraction_retry_uses_persisted_raw_reference(
    bootstrapped_engine: sa.Engine, bootstrapped_async_engine: AsyncEngine, memory_resources: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    env = memory_resources
    env["model_response"] = {"episodic": [{"content": "controlled meeting", "valid_at": "2026-11-02 15:00:00", "invalid_at": "2026-11-02 16:00:00"}]}
    with Session(bootstrapped_engine) as db:
        db.get(Memory, env["mid"]).memory_type = 5
        db.commit()
    monkeypatch.setattr(memory_write_runtime, "current_timestamp", lambda: 1793498400000)
    async with AsyncSession(bootstrapped_async_engine, expire_on_commit=False) as db:
        assert await mms.queue_save_to_memory_task(
            db, [env["mid"]], {"user_id": "subject", "agent_id": "agent", "session_id": "session", "user_input": "明天15:00到16:00开会", "agent_response": "已记录"}
        ) == (True, "All add to task.")
        await db.commit()
    queued = json.loads(REDIS_CONN.REDIS.xrange(env["queue"])[0][1]["message"])
    raw = MessageService.get_by_message_id(env["mid"], queued["source_id"], env["tenant"])
    assert raw["message_type"] == "raw" and raw["valid_at"]
    queued["message_dict"].update(user_input="altered queue text", conversation_time="2099-01-01 00:00:00")
    env["fail_embedding_once"] = True
    monkeypatch.setattr(memory_write_runtime, "current_timestamp", lambda: 1893498400000)
    with Session(bootstrapped_engine) as db, pytest.raises(memory_write_runtime.MemoryTaskPending) as pending:
        await mms.save_extracted_to_memory_only(db, env["mid"], queued["message_dict"], queued["source_id"], task_id=queued["id"])
    assert pending.value.code == "writer_io_unconfirmed" and not pending.value.acknowledge
    assert len(MessageService.get_recent_messages([env["tenant"]], [env["mid"]], "agent", "session", 10)) == 1
    assert env["extracts"] == [env["tenant"]]
    assert len(env["references"]) == 1 and raw["content"] in env["references"][0]
    assert raw["valid_at"][:10] in env["references"][0] and "2099-01-01" not in env["references"][0] and "altered queue text" not in env["references"][0]
    monkeypatch.setattr(memory_write_runtime, "current_timestamp", lambda: 1993498400000)
    with Session(bootstrapped_engine) as db:
        assert (await mms.save_extracted_to_memory_only(db, env["mid"], queued["message_dict"], queued["source_id"], task_id=queued["id"]))[0]
    stored = MessageService.get_recent_messages([env["tenant"]], [env["mid"]], "agent", "session", 10)
    extracted = next(message for message in stored if message["message_type"] == "episodic")
    assert len(stored) == 2 and extracted["source_id"] == queued["source_id"]
    assert extracted["invalid_at"] == "2026-11-02 16:00:00" and env["extracts"] == [env["tenant"], env["tenant"]]
    assert env["references"] == [env["references"][0], env["references"][0]]
    with Session(bootstrapped_engine) as db:
        assert (await mms.save_extracted_to_memory_only(db, env["mid"], queued["message_dict"], queued["source_id"])) == (False, "Coordinated extraction requires a persisted task.")
    with Session(bootstrapped_engine) as db, pytest.raises(RevisionConflict, match="extraction_task_binding_mismatch"):
        await mms.save_extracted_to_memory_only(db, env["mid"], queued["message_dict"], extracted["message_id"], task_id=queued["id"])
    assert len(MessageService.get_recent_messages([env["tenant"]], [env["mid"]], "agent", "session", 10)) == 2
    env["journal"].event("memory-extraction-retry-readback", persisted_source_time=raw["valid_at"], paid_extractions=len(env["extracts"]), invalid_task_binding_effects=0)


async def test_memory_redelivery_preserves_committed_and_uncertain_storage_results(
    bootstrapped_engine: sa.Engine, bootstrapped_async_engine: AsyncEngine, memory_resources: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    env = memory_resources
    extracts = env["extracts"]

    @contextmanager
    def connection() -> Iterator[Session]:
        with Session(bootstrapped_engine) as db:
            yield db

    monkeypatch.setattr(worker, "db_connection", connection)
    async with AsyncSession(bootstrapped_async_engine, expire_on_commit=False) as db:
        assert (
            await mms.queue_save_to_memory_task(db, [env["mid"]], {"user_id": "subject", "agent_id": "agent", "session_id": "session", "user_input": "controlled", "agent_response": "controlled"})
        )[0]
        await db.commit()
    queued = json.loads(REDIS_CONN.REDIS.xrange(env["queue"])[0][1]["message"])
    await worker.handle_task()
    with Session(bootstrapped_engine) as reader:
        assert reader.get(Task, queued["id"]).progress == 1
    before_calls = list(env["calls"])
    before_counter = REDIS_CONN.REDIS.get(env["counter"])

    # A completed task delivered again must not call the model or write memory.
    assert REDIS_CONN.queue_product(env["queue"], queued)
    await worker.handle_task()
    assert env["calls"] == before_calls and extracts == [env["tenant"]]
    assert REDIS_CONN.REDIS.get(env["counter"]) == before_counter

    # A durable ready admission recovers SQL progress without a second model call.
    with Session(bootstrapped_engine) as db:
        db.get(Task, queued["id"]).progress = 0
        db.commit()
    assert REDIS_CONN.queue_product(env["queue"], queued)
    await worker.handle_task()
    with Session(bootstrapped_engine) as reader:
        assert reader.get(Task, queued["id"]).progress == 1
    assert env["calls"] == before_calls and extracts == [env["tenant"]]

    # Reproduce the historical failure: message storage committed, SQL progress
    # rolled back. Preserve those results and require reconciliation, not replay.
    with Session(bootstrapped_engine) as db:
        admission = db.scalar(sa.select(MemoryWriterAdmission).where(MemoryWriterAdmission.task_id == queued["id"]))
        assert admission is not None and admission.publication_state == "ready"
        env["journal"].event("reserve-owned-legacy-proof-loss", memory_id=env["mid"], task_id=queued["id"], operation_id=admission.operation_id)
        db.execute(sa.delete(MemoryPublicationOutbox).where(MemoryPublicationOutbox.memory_id == env["mid"], MemoryPublicationOutbox.operation_id == admission.operation_id))
        db.delete(admission)
        db.get(Task, queued["id"]).progress = 0
        db.commit()
    assert REDIS_CONN.queue_product(env["queue"], queued)
    await worker.handle_task()
    with Session(bootstrapped_engine) as reader:
        task = reader.get(Task, queued["id"])
        assert task.progress == -1
        assert "reconciliation" in task.progress_msg
    assert len(MessageService.get_recent_page(env["tenant"], env["mid"], "agent", "session", 10)["messages"]) == 2
    assert env["calls"] == before_calls and extracts == [env["tenant"]]
    assert REDIS_CONN.REDIS.get(env["counter"]) == before_counter
    assert REDIS_CONN.REDIS.xpending(env["queue"], worker.SVR_CONSUMER_GROUP_NAME)["pending"] == 0
    env["journal"].event("memory-redelivery-readback", completed_replay_model_calls=0, uncertain_replay_model_calls=0, derived_writes=1, uncertain_task_requires_reconciliation=True, queue_pending=0)


def test_configuration_equivalence_and_occupied_change_sql(bootstrapped_engine: sa.Engine, memory_resources: dict[str, Any]) -> None:
    from api.apps.services import memory_api_service as api
    from api.db.db_models import TenantModel
    from common.exceptions import ArgumentException

    env = memory_resources
    model_ids: list[str] = []
    try:
        with Session(bootstrapped_engine) as db:
            for owner, name in ((env["tenant"], "canonical"), (env["tenant"], "changed"), (env["foreign_tenant"], "foreign")):
                record = register_model(db, tenant_id=owner, provider_name="controlled", model_name=name, model_types=("embedding",))
                model_ids.append(record.id)
            env["journal"].event("reserve-model-rows", ids=model_ids, database=str(bootstrapped_engine.url.database))
            memory = db.get(Memory, env["mid"])
            memory.embd_id = model_ids[0]
            memory.tenant_embd_id = model_ids[0]
            db.commit()
        env["journal"].event("reserve-physical-occupancy", memory_id=env["mid"], message_id=1000)
        assert MessageService.create_index(env["tenant"], env["mid"], 2)
        assert (
            MessageService.insert_message(
                [
                    {
                        "message_id": 1000,
                        "message_type": "raw",
                        "source_id": 0,
                        "memory_id": env["mid"],
                        "user_id": "subject",
                        "agent_id": "agent",
                        "session_id": "session",
                        "content": "real occupied memory",
                        "content_embed": [1.0, 0.0],
                        "valid_at": "2026-11-15 00:00:00",
                        "invalid_at": None,
                        "forget_at": None,
                        "status": True,
                    }
                ],
                env["tenant"],
                env["mid"],
            )
            == []
        )
        assert mms.get_memory_size_cache(env["tenant"], env["mid"]) > 0
        with Session(bootstrapped_engine) as reader:
            before = reader.get(Memory, env["mid"]).to_dict()
        for update in [{"embd_id": model_ids[0]}, {"memory_type": ["semantic", " RAW ", "raw"]}, {"embd_id": model_ids[0], "memory_type": ["raw", "semantic"]}]:
            with Session(bootstrapped_engine) as db:
                assert api.update_memory(db, env["mid"], update, user_id=env["tenant"])[0]
                db.commit()
            with Session(bootstrapped_engine) as reader:
                assert reader.get(Memory, env["mid"]).to_dict() == before
        for update in [{"embd_id": model_ids[1]}, {"memory_type": ["raw"]}, {"embd_id": model_ids[2]}]:
            with Session(bootstrapped_engine) as db, pytest.raises(ArgumentException):
                api.update_memory(db, env["mid"], update, user_id=env["tenant"])
            with Session(bootstrapped_engine) as reader:
                assert reader.get(Memory, env["mid"]).to_dict() == before
        MessageService.delete_index(env["tenant"], env["mid"])
        assert not settings.msgStoreConn.index_exists_strict(index_name(env["tenant"]), env["mid"])
        assert mms.get_memory_size_cache(env["tenant"], env["mid"]) == 0
        with Session(bootstrapped_engine) as db:
            assert api.update_memory(db, env["mid"], {"embd_id": model_ids[1], "description": "changed"}, user_id=env["tenant"])[0]
            db.commit()
        with Session(bootstrapped_engine) as reader:
            memory = reader.get(Memory, env["mid"])
            assert memory.tenant_embd_id == model_ids[1] and memory.description == "changed"
        env["journal"].event("memory-config-readback", no_op_cases=3, occupied_or_foreign_rejections=3, empty_memory_change_persisted=True)
    finally:
        with Session(bootstrapped_engine) as db:
            db.execute(sa.delete(TenantModel).where(TenantModel.id.in_(model_ids)))
            db.commit()
        with Session(bootstrapped_engine) as reader:
            assert reader.scalar(sa.select(sa.func.count()).select_from(TenantModel).where(TenantModel.id.in_(model_ids))) == 0
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
