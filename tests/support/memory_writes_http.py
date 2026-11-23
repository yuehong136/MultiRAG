"""Shared owned memory writes http integration fixtures and helpers."""

import asyncio
import hashlib
import json
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
from api.db.db_models import Memory, Task, TenantModel, UserTenant
from api.db.joint_services import memory_write_runtime
from api.db.memory_revision_models import ManagedMemoryState, MemoryEntry, MemoryEntryRevision, MemoryMutationOperation, MemoryPublicationOutbox, MemoryRevisionSource, MemorySourceState
from api.db.memory_writer_models import MemoryWriterAdmission, MemoryWriterFence
from api.db.model_contract import parse_model_id
from api.db.services.tenant_llm_service import TenantLLMService
from common import settings
from common.constants import MemoryType
from core.utils.redis_conn import REDIS_CONN
from memory.revisions.storage import ManagedMessageStore
from memory.services.messages import MessageService, index_name
from tests.support.agent_update_release import release_api as release_api
from tests.support.registered_model import register_model
from tests.support.resource_journal import ResourceJournal

_TABLES = (MemoryWriterAdmission, MemoryWriterFence, MemoryPublicationOutbox, MemoryRevisionSource, MemoryEntryRevision, MemoryMutationOperation, MemoryEntry, MemorySourceState, ManagedMemoryState)


class WriteEmbedding:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def encode(self, texts: list[str]) -> tuple[list[list[float]], int]:
        self.calls.extend(texts)
        return [[1.0, 0.0] for _ in texts], 0

    def close(self) -> None:
        pass


class WriteChat:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    async def async_chat(self, system: str, history: list[dict[str, Any]], gen_conf: dict[str, Any]) -> tuple[str, int]:
        self.calls.append(history[-1]["content"])
        return json.dumps({"semantic": [{"content": "A controlled derived memory.", "valid_at": "2026-11-15 09:00:00", "invalid_at": None}]}), 0

    def close(self) -> None:
        pass


@pytest.fixture
def writes_api(release_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    env = release_api
    owner, member, outsider = env["owners"]
    memories = {key: uuid4().hex for key in ("personal", "team", "foreign")}
    tenants = {"personal": member, "team": owner, "foreign": outsider}
    queue = "memory-writes-http-" + uuid4().hex
    counter = queue + ":ids"
    journal = ResourceJournal("memory-writes-http")
    journal.event(
        "reserve-memory-scope",
        database=env["engine"].url.database,
        memories=memories,
        tenants=tenants,
        indexes={key: index_name(tenant) + "_" + memories[key] for key, tenant in tenants.items()},
        queue=queue,
        counter=counter,
    )
    embeddings: list[str] = []
    chats: list[str] = []
    model_ids: list[str] = []
    created_indexes: set[str] = set()
    monkeypatch.setattr(memory_write_runtime, "_COUNTER_KEY", counter)
    monkeypatch.setattr(memory_revision_service, "_COUNTER_KEY", counter)
    monkeypatch.setattr(settings, "get_svr_queue_name", lambda priority: queue)

    def model(cls: type[TenantLLMService], config: dict[str, Any], lang: str = "Chinese", **kwargs: Any) -> WriteEmbedding | WriteChat:
        assert config["tenant_id"] in tenants.values() and parse_model_id(config["id"]) is not None
        if config["mdl_type"] == "embedding":
            return WriteEmbedding(embeddings)
        assert config["mdl_type"] == "chat"
        return WriteChat(chats)

    monkeypatch.setattr(TenantLLMService, "model_instance_from_config", classmethod(model))
    with ExitStack() as cleanup:
        journal.event("reserve-runtime-async-engine", database=env["engine"].url.database, pool="NullPool")
        asynchronous = create_async_engine(env["engine"].url, poolclass=NullPool)
        cleanup.callback(lambda: asyncio.run(asynchronous.dispose()))
        factory = async_sessionmaker(asynchronous, expire_on_commit=False)

        def reserve_insert(conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool) -> None:
            compiled = getattr(context, "compiled", None)
            if compiled is None or not getattr(context, "isinsert", False):
                return
            table = compiled.statement.table
            for values in context.compiled_parameters:
                mid = values.get("memory_id") or values.get("doc_id")
                if mid in memories.values():
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
            for key, mid in memories.items():
                if mid in created_indexes:
                    asyncio.run(ManagedMessageStore(tenants[key], mid).drop())
                assert not settings.msgStoreConn.index_exists_strict(index_name(tenants[key]), mid)
            redis = REDIS_CONN.REDIS
            keys = [queue, counter, *memories.values(), *("memory_" + mid for mid in memories.values())]
            redis.delete(*keys)
            assert not redis.exists(*keys)
            with Session(env["engine"]) as db:
                for table in _TABLES:
                    db.execute(sa.delete(table).where(table.memory_id.in_(list(memories.values()))))
                db.execute(sa.delete(Task).where(Task.doc_id.in_(list(memories.values()))))
                db.execute(sa.delete(Memory).where(Memory.id.in_(list(memories.values()))))
                db.execute(sa.delete(TenantModel).where(TenantModel.id.in_(model_ids)))
                db.commit()
            with Session(env["engine"]) as reader:
                remaining = {table.__tablename__: reader.scalar(sa.select(sa.func.count()).select_from(table).where(table.memory_id.in_(list(memories.values())))) for table in _TABLES}
                remaining["memories"] = reader.scalar(sa.select(sa.func.count()).select_from(Memory).where(Memory.id.in_(list(memories.values()))))
                remaining["tasks"] = reader.scalar(sa.select(sa.func.count()).select_from(Task).where(Task.doc_id.in_(list(memories.values()))))
                assert not any(remaining.values()), remaining
            journal.event("memory-writes-cleanup-readback", indexes_absent=True, redis_keys=keys, redis_absent=True, sql_remaining=remaining)

        cleanup.callback(remove)
        with Session(env["engine"]) as db:
            db.add(UserTenant(id=uuid4().hex, user_id=member, tenant_id=owner, role="normal", status="1", invited_by=owner))
            for key, mid in memories.items():
                models: dict[str, str] = {}
                for kind in ("embedding", "chat"):
                    record = register_model(db, tenant_id=tenants[key], provider_name="controlled", model_name="auth-" + kind, model_types=(kind,), api_key="unused-owned-test-value")
                    models[kind] = record.id
                    model_ids.append(record.id)
                db.add(
                    Memory(
                        id=mid,
                        tenant_id=tenants[key],
                        name=key,
                        permissions="team" if key == "team" else "me",
                        memory_type=MemoryType.RAW.value,
                        embd_id=models["embedding"],
                        tenant_embd_id=models["embedding"],
                        llm_id=models["chat"],
                        tenant_llm_id=models["chat"],
                    )
                )
            db.commit()
        REDIS_CONN.REDIS.set(counter, str(2**53 + 100))
        for key, mid in memories.items():
            assert not settings.msgStoreConn.index_exists_strict(index_name(tenants[key]), mid)
            created_indexes.add(mid)
            assert MessageService.create_index(tenants[key], mid, 2)
            assert (
                MessageService.insert_message(
                    [
                        {
                            "message_id": 900,
                            "message_type": "raw",
                            "source_id": 0,
                            "memory_id": mid,
                            "user_id": "existing-subject",
                            "agent_id": "existing-agent",
                            "session_id": "existing-session",
                            "valid_at": "2026-11-15 08:00:00",
                            "invalid_at": None,
                            "forget_at": None,
                            "status": True,
                            "content": "Owned raw " + key,
                            "content_embed": [1.0, 0.0],
                        }
                    ],
                    tenants[key],
                    mid,
                )
                == []
            )
        yield dict(env, memories=memories, tenants=tenants, queue=queue, counter=counter, memory_id=memories["personal"], journal=journal, embedding_calls=embeddings, chat_calls=chats)


def api_request(env: dict[str, Any], method: str, path: str, *, actor: int = 1, token: str | None = None, **kwargs: Any) -> Any:
    return env["client"].request(method, env["base"] + "/api/v1" + path, headers={"Authorization": "Bearer " + (token if token is not None else env["jwts"][actor])}, timeout=60, **kwargs)


def result(response: Any, *, status: int = 200) -> dict[str, Any]:
    assert response.status_code == status, response.text
    value = response.json()
    assert value["retcode"] == 0, value
    return value["data"]


def manage_bank(env: dict[str, Any]) -> None:
    digest = hashlib.sha256(b"Owned raw personal").hexdigest()
    value = result(api_request(env, "POST", f"/memories/{env['memory_id']}/sources/900/adoptions", json={"operation_id": "register-owned-source", "expected_content_sha256": digest}))
    assert value["index_sync_status"] == "ready"
