"""Owned real REST, SQL, Milvus and Redis contracts with controlled models only."""

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
from api.db.db_models import Memory, Task, TenantLLM, UserTenant
from api.db.joint_services import memory_write_runtime
from api.db.memory_revision_models import ManagedMemoryState, MemoryEntry, MemoryEntryRevision, MemoryMutationOperation, MemoryPublicationOutbox, MemoryRevisionSource, MemorySourceState
from api.db.memory_writer_models import MemoryWriterAdmission, MemoryWriterFence
from api.db.services.tenant_llm_service import TenantLLMService
from common import settings
from common.constants import MemoryType
from core.utils.redis_conn import REDIS_CONN
from memory.revisions.storage import ManagedMessageStore
from memory.services.messages import MessageService, index_name
from tests.support.agent_update_release import release_api as release_api
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
    model_ids: list[int] = []
    created_indexes: set[str] = set()
    monkeypatch.setattr(memory_write_runtime, "_COUNTER_KEY", counter)
    monkeypatch.setattr(memory_revision_service, "_COUNTER_KEY", counter)
    monkeypatch.setattr(settings, "get_svr_queue_name", lambda priority: queue)

    def model(cls: type[TenantLLMService], config: dict[str, Any], lang: str = "Chinese", **kwargs: Any) -> WriteEmbedding | WriteChat:
        assert config["tenant_id"] in tenants.values() and type(config["id"]) is int
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
                db.execute(sa.delete(TenantLLM).where(TenantLLM.id.in_(model_ids)))
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
                models: dict[str, int] = {}
                for kind in ("embedding", "chat"):
                    record = TenantLLM(tenant_id=tenants[key], llm_factory="controlled", llm_name="auth-" + kind, mdl_type=kind, api_key="unused-owned-test-value")
                    db.add(record)
                    db.flush()
                    models[kind] = record.id
                    model_ids.append(record.id)
                db.add(
                    Memory(
                        id=mid,
                        tenant_id=tenants[key],
                        name=key,
                        permissions="team" if key == "team" else "me",
                        memory_type=MemoryType.RAW.value,
                        embd_id="auth-embedding",
                        tenant_embd_id=models["embedding"],
                        llm_id="auth-chat",
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


def command(operation_id: str = "append-owned") -> dict[str, str]:
    return {
        "operation_id": operation_id,
        "user_input": "Controlled input",
        "agent_response": "Controlled answer",
        "agent_id": "owned-agent",
        "session_id": "owned-session",
        "user_id": "forged-subject",
    }


@pytest.mark.parametrize("managed", [False, True])
def test_raw_append_replay_identity_and_jwt_attribution(writes_api: dict[str, Any], managed: bool) -> None:
    env, mid = writes_api, writes_api["memory_id"]
    if managed:
        manage_bank(env)
    payload = command()
    created = result(api_request(env, "POST", f"/memories/{mid}/messages", json=payload))
    assert created["kind"] == "append_raw" and created["queue_state"] == "not_required" and created["extraction_task_id"] is None
    source_id = created["raw_source_id"]
    assert isinstance(source_id, str) and int(source_id) > 2**53 and created["message_ids"] == [source_id]
    before = REDIS_CONN.REDIS.get(env["counter"])
    assert result(api_request(env, "POST", f"/memories/{mid}/messages", json=payload))["message_ids"] == [source_id]
    assert REDIS_CONN.REDIS.get(env["counter"]) == before
    assert api_request(env, "POST", f"/memories/{mid}/messages", json={**payload, "user_input": "Different input"}).status_code == 409
    raw = MessageService.get_by_message_id(mid, int(source_id), env["tenants"]["personal"])
    assert raw and raw["user_id"] == env["owners"][1] and raw["session_id"] == payload["session_id"]
    with Session(env["engine"]) as db:
        admission = db.get(MemoryWriterAdmission, (mid, payload["operation_id"]))
        assert admission and admission.source_message_id == int(source_id) and admission.tenant_id == env["tenants"]["personal"]
        assert db.scalar(sa.select(sa.func.count()).select_from(Task).where(Task.doc_id == mid)) == 0
    assert result(api_request(env, "GET", f"/memories/{mid}/operations/{payload['operation_id']}"))["raw_source_id"] == source_id
    assert result(api_request(env, "GET", f"/memories/{mid}/capabilities"))["revisions"]["managed"] is managed
    assert len(env["embedding_calls"]) == 1 and not env["chat_calls"] and REDIS_CONN.REDIS.xlen(env["queue"]) == 0


def test_denied_append_is_effect_free_and_sdk_subject_is_explicit(writes_api: dict[str, Any]) -> None:
    env, mid = writes_api, writes_api["memory_id"]
    before = REDIS_CONN.REDIS.get(env["counter"])
    assert api_request(env, "POST", f"/memories/{mid}/messages", actor=2, json=command()).status_code == 404
    assert api_request(env, "POST", f"/memories/{mid}/messages", json={**command(), "tenant_id": env["tenants"]["foreign"]}).status_code == 422
    assert REDIS_CONN.REDIS.get(env["counter"]) == before and not env["embedding_calls"]
    with Session(env["engine"]) as db:
        assert db.scalar(sa.select(sa.func.count()).select_from(MemoryWriterAdmission).where(MemoryWriterAdmission.memory_id == mid)) == 0
    payload = {**command("append-sdk"), "user_id": "  explicit external subject  "}
    accepted = result(api_request(env, "POST", f"/memories/{mid}/messages", token=env["keys"][1], json=payload))
    raw = MessageService.get_by_message_id(mid, int(accepted["raw_source_id"]), env["tenants"]["personal"])
    assert raw and raw["user_id"] == payload["user_id"]


def test_append_creates_a_missing_owned_index(writes_api: dict[str, Any]) -> None:
    env, mid = writes_api, writes_api["memory_id"]
    tenant = env["tenants"]["personal"]
    env["journal"].event("drop-owned-index-for-recreation", index=index_name(tenant), memory_id=mid)
    asyncio.run(ManagedMessageStore(tenant, mid).drop())
    assert not settings.msgStoreConn.index_exists_strict(index_name(tenant), mid)
    created = result(api_request(env, "POST", f"/memories/{mid}/messages", json=command("append-empty-index")))
    assert created["index_sync_status"] == "ready" and created["queue_state"] == "not_required"
    assert settings.msgStoreConn.index_exists_strict(index_name(tenant), mid)
    raw = MessageService.get_by_message_id(mid, int(created["raw_source_id"]), tenant)
    assert raw and raw["message_type"] == "raw" and raw["content_embed"] == [1.0, 0.0]


def test_unknown_queue_recovers_then_real_task_extracts_once(writes_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env, mid = writes_api, writes_api["memory_id"]
    with Session(env["engine"]) as db:
        db.execute(sa.update(Memory).where(Memory.id == mid).values(memory_type=MemoryType.RAW.value | MemoryType.SEMANTIC.value))
        db.commit()
    submit = REDIS_CONN.queue_product
    monkeypatch.setattr(REDIS_CONN, "queue_product", lambda *args, **kwargs: False)
    pending = result(api_request(env, "POST", f"/memories/{mid}/messages", json=command("append-queue")), status=202)
    assert pending["index_sync_status"] == "ready" and pending["queue_state"] == "unknown"
    task_id = pending["extraction_task_id"]
    with Session(env["engine"]) as reader:
        task = reader.get(Task, task_id)
        assert task and task.doc_id == mid and task.digest == pending["raw_source_id"] and task.progress == 0
    assert REDIS_CONN.REDIS.xlen(env["queue"]) == 0
    assert result(api_request(env, "GET", f"/memories/{mid}/operations/append-queue"))["queue_state"] == "unknown"
    assert REDIS_CONN.REDIS.xlen(env["queue"]) == 0 and not env["chat_calls"]
    monkeypatch.setattr(REDIS_CONN, "queue_product", submit)
    ready = result(api_request(env, "POST", f"/memories/{mid}/operations/append-queue/reconciliations", json={}))
    assert ready["queue_state"] == "submitted" and ready["extraction_task_id"] == task_id and ready["message_ids"] == pending["message_ids"]
    queued = REDIS_CONN.REDIS.xrange(env["queue"])
    assert len(queued) == 1
    encoded = queued[0][1].get("message", queued[0][1].get(b"message"))
    payload = json.loads(encoded)
    assert payload["id"] == task_id and payload["source_id"] == int(pending["raw_source_id"]) and payload["tenant_id"] == env["tenants"]["personal"]
    assert asyncio.run(memory_write_runtime.handle_coordinated_task(payload))
    with Session(env["engine"]) as reader:
        assert reader.get(Task, task_id).progress == 1
    inventory = asyncio.run(ManagedMessageStore(env["tenants"]["personal"], mid).inventory())
    derived = [row for row in inventory if row["message_type"] == "semantic" and row["source_id"] == payload["source_id"]]
    assert len(derived) == 1 and derived[0]["content"] == "A controlled derived memory."
    assert len(env["chat_calls"]) == 1
    count = len(env["embedding_calls"])
    assert asyncio.run(memory_write_runtime.handle_coordinated_task(payload))
    assert len(env["chat_calls"]) == 1 and len(env["embedding_calls"]) == count
    assert asyncio.run(ManagedMessageStore(env["tenants"]["personal"], mid).inventory()) == inventory


def test_unmanaged_deletion_keeps_only_authorized_deletion_receipt(writes_api: dict[str, Any]) -> None:
    env, mid = writes_api, writes_api["memory_id"]
    result(api_request(env, "POST", f"/memories/{mid}/messages", json=command()))
    with Session(env["engine"]) as reader:
        assert reader.get(ManagedMemoryState, mid) is None
    deleted = result(api_request(env, "POST", f"/memories/{mid}/deletions", json={"operation_id": "delete-unmanaged"}))
    assert deleted["lifecycle"] == "deleted" and deleted["tracked_cleanup_confirmed"] is True
    assert deleted["physical_cleanup_complete"] is False and deleted["untraceable_copies"] is True
    with Session(env["engine"]) as reader:
        assert reader.get(Memory, mid) is None and reader.get(ManagedMemoryState, mid).state == "deleted"
    assert not settings.msgStoreConn.index_exists_strict(index_name(env["tenants"]["personal"]), mid)
    assert result(api_request(env, "GET", f"/memories/{mid}/operations/delete-unmanaged"))["lifecycle"] == "deleted"
    assert api_request(env, "GET", f"/memories/{mid}/operations/delete-unmanaged", actor=2).status_code == 404
    assert api_request(env, "GET", f"/memories/{mid}/operations/append-owned").status_code == 404
    before = REDIS_CONN.REDIS.get(env["counter"])
    assert api_request(env, "POST", f"/memories/{mid}/messages", json=command("late-append")).status_code == 404
    assert REDIS_CONN.REDIS.get(env["counter"]) == before
