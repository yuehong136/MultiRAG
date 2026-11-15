"""Shared owned memory authorization integration fixtures and helpers."""

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import ExitStack, asynccontextmanager
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from api.apps.services import memory_revision_service
from api.db.db_models import Memory, Task, TenantLLM, UserTenant
from api.db.joint_services import memory_write_runtime
from api.db.memory_revision_models import ManagedMemoryState, MemoryEntry, MemoryEntryRevision, MemoryMutationOperation, MemoryPublicationOutbox, MemoryRevisionSource, MemorySourceState
from api.db.memory_writer_models import MemoryWriterAdmission, MemoryWriterFence
from api.db.services.tenant_llm_service import TenantLLMService
from common import settings
from core.utils.redis_conn import REDIS_CONN
from memory.services.messages import MessageService, index_name
from tests.support.agent_update_release import release_api as release_api
from tests.support.resource_journal import ResourceJournal

_SIDE_TABLES = (
    MemoryPublicationOutbox,
    MemoryRevisionSource,
    MemoryEntryRevision,
    MemoryMutationOperation,
    MemoryEntry,
    MemorySourceState,
    ManagedMemoryState,
    MemoryWriterAdmission,
    MemoryWriterFence,
)


class FixedEmbedding:
    def __init__(self, db: Session | None = None, tenant_id: str = "") -> None:
        self.db = db
        self.tenant_id = tenant_id

    def encode_queries(self, text: str) -> tuple[list[float], int]:
        return [1.0, 0.0], 0

    def encode(self, texts: list[str]) -> tuple[list[list[float]], int]:
        return [[1.0, 0.0] for _ in texts], 0

    def close(self) -> None:
        pass


@pytest.fixture
def memory_api(release_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    env = release_api
    owner, member, outsider = env["owners"]
    memories = {key: uuid4().hex for key in ["private", "team", "personal", "foreign"]}
    tenants = {"private": owner, "team": owner, "personal": member, "foreign": outsider}
    queue = f"memory-auth-{uuid4().hex}"
    counter = queue + ":coordinated-ids"
    journal = ResourceJournal("memory-authorization-http")
    journal.event("reserve-memory-resources", database=env["engine"].url.database, memories=memories, tenants=tenants, queue=queue, counter=counter)
    redis = REDIS_CONN.REDIS
    increment = REDIS_CONN.generate_auto_increment_id
    monkeypatch.setattr(settings, "get_svr_queue_name", lambda priority: queue)
    monkeypatch.setattr(REDIS_CONN, "generate_auto_increment_id", lambda **kwargs: increment(key_prefix=queue, **kwargs))
    monkeypatch.setattr(memory_write_runtime, "_COUNTER_KEY", counter)
    monkeypatch.setattr(memory_revision_service, "_COUNTER_KEY", counter)

    def model(cls: type[TenantLLMService], config: dict[str, Any], lang: str = "Chinese", **kwargs: Any) -> FixedEmbedding:
        assert config["mdl_type"] == "embedding" and config["tenant_id"] in tenants.values()
        assert type(config["id"]) is int and config["id"] > 0
        return FixedEmbedding(tenant_id=config["tenant_id"])

    monkeypatch.setattr(TenantLLMService, "model_instance_from_config", classmethod(model))
    physical: set[tuple[str, str]] = set()
    create_index = MessageService.create_index

    def register_index(cls: type[MessageService], tenant_id: str, memory_id: str, dimension: int) -> bool:
        if (tenant_id, memory_id) not in physical:
            assert not settings.msgStoreConn.index_exists_strict(index_name(tenant_id), memory_id)
            journal.event("reserve-memory-index", tenant_id=tenant_id, memory_id=memory_id, index=index_name(tenant_id), collection=f"{index_name(tenant_id)}_{memory_id}")
            physical.add((tenant_id, memory_id))
        return create_index(tenant_id, memory_id, dimension)

    monkeypatch.setattr(MessageService, "create_index", classmethod(register_index))
    created: list[str] = []
    model_ids: dict[str, int] = {}
    with ExitStack() as cleanup:
        journal.event("reserve-runtime-async-engine", database=env["engine"].url.database, pool="NullPool")
        asynchronous = create_async_engine(env["engine"].url, poolclass=NullPool)
        cleanup.callback(lambda: asyncio.run(asynchronous.dispose()))
        factory = async_sessionmaker(asynchronous, expire_on_commit=False)

        def reserve_insert(conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool) -> None:
            if getattr(context, "compiled", None) is None or not getattr(context, "isinsert", False):
                return
            table = context.compiled.statement.table
            for values in context.compiled_parameters:
                mid = values.get("memory_id") or values.get("doc_id")
                if mid in [*memories.values(), *created]:
                    journal.event(
                        "reserve-runtime-sql-row",
                        database=env["engine"].url.database,
                        table=table.name,
                        memory_id=mid,
                        identity={column.name: values.get(column.name) for column in table.primary_key.columns},
                    )

        sa.event.listen(asynchronous.sync_engine, "before_cursor_execute", reserve_insert)
        cleanup.callback(sa.event.remove, asynchronous.sync_engine, "before_cursor_execute", reserve_insert)

        @asynccontextmanager
        async def runtime_db() -> AsyncIterator[AsyncSession]:
            async with factory() as db:
                yield db

        monkeypatch.setattr(memory_write_runtime, "async_db_connection", runtime_db)

        def remove() -> None:
            mids = [*memories.values(), *created, *(mid for _, mid in physical)]
            with Session(env["engine"]) as db:
                for table in _SIDE_TABLES:
                    db.execute(sa.delete(table).where(table.memory_id.in_(mids)))
                db.execute(sa.delete(Task).where(Task.doc_id.in_(mids)))
                db.execute(sa.delete(Memory).where(Memory.id.in_(mids)))
                db.execute(sa.delete(TenantLLM).where(TenantLLM.id.in_(list(model_ids.values()))))
                db.commit()
            with Session(env["engine"]) as reader:
                remaining = {table.__tablename__: reader.scalar(sa.select(sa.func.count()).select_from(table).where(table.memory_id.in_(mids))) for table in _SIDE_TABLES}
                assert not any(remaining.values()), remaining
                assert reader.scalar(sa.select(sa.func.count()).select_from(Memory).where(Memory.id.in_(mids))) == 0
                assert reader.scalar(sa.select(sa.func.count()).select_from(Task).where(Task.doc_id.in_(mids))) == 0
            journal.event("memory-auth-sql-cleanup-readback", memory_ids=mids, remaining=remaining)
            for tenant_id, mid in physical:
                if settings.msgStoreConn.index_exists_strict(index_name(tenant_id), mid):
                    settings.msgStoreConn.delete_idx(index_name(tenant_id), mid)
                assert not settings.msgStoreConn.index_exists_strict(index_name(tenant_id), mid)
                journal.event("memory-auth-index-cleanup-readback", tenant_id=tenant_id, memory_id=mid, absent=True)
            for mid in mids:
                redis.delete(mid, f"memory_{mid}")
                assert not redis.exists(mid, f"memory_{mid}")
            redis.delete(queue, f"{queue}:memory", counter)
            assert not redis.exists(queue, f"{queue}:memory", counter)
            journal.event("memory-auth-redis-cleanup-readback", keys=[queue, f"{queue}:memory", counter], absent=True)

        cleanup.callback(remove)
        with Session(env["engine"]) as db:
            db.add(UserTenant(id=uuid4().hex, user_id=member, tenant_id=owner, role="normal", status="1", invited_by=owner))
            for key, mid in memories.items():
                db.add(Memory(id=mid, tenant_id=tenants[key], name=key, permissions="team" if key == "team" else "me", memory_type=1, embd_id="auth-embedding", llm_id="auth-chat"))
            for label, tenant in [("owner", owner), ("member", member), ("foreign", outsider)]:
                for kind in ["chat", "embedding"]:
                    model = TenantLLM(tenant_id=tenant, llm_factory="controlled", llm_name=f"auth-{kind}", mdl_type=kind, api_key="unused-test-value", max_tokens=8192)
                    db.add(model)
                    db.flush()
                    model_ids[f"{label}-{kind}"] = model.id
            db.commit()
        for key, mid in memories.items():
            assert MessageService.create_index(tenants[key], mid, 2)
            assert (
                MessageService.insert_message(
                    [
                        {
                            "message_id": 900,
                            "message_type": "raw",
                            "source_id": 0,
                            "memory_id": mid,
                            "user_id": "subject",
                            "agent_id": "a",
                            "session_id": "s",
                            "valid_at": "2026-10-07 00:00:00",
                            "invalid_at": None,
                            "forget_at": None,
                            "status": True,
                            "content": key,
                            "content_embed": [1.0, 0.0],
                        }
                    ],
                    tenants[key],
                    mid,
                )
                == []
            )
        yield dict(env, memories=memories, tenants=tenants, queue=queue, counter=counter, journal=journal, created=created, model_ids=model_ids)


def insert_read_messages(env: dict[str, Any], rows: list[dict[str, Any]], *, key: str = "personal") -> None:
    """Write only this fixture's owned index; teardown independently deletes it."""
    memory_id = env["memories"][key]
    messages = [
        {
            "message_id": 910,
            "message_type": "semantic",
            "source_id": 900,
            "memory_id": memory_id,
            "user_id": "subject",
            "agent_id": "a",
            "session_id": "s",
            "valid_at": "2026-10-08 00:00:00",
            "invalid_at": None,
            "forget_at": None,
            "status": True,
            "content": "A decision with an inspectable source.",
            "content_embed": [1.0, 0.0],
            **row,
        }
        for row in rows
    ]
    assert MessageService.insert_message(messages, env["tenants"][key], memory_id) == []
    for message in messages:
        stored = MessageService.get_by_message_id(memory_id, message["message_id"], env["tenants"][key])
        assert stored and stored["content"] == message["content"]
