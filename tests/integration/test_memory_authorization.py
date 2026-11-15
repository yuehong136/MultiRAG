"""Authenticated registered Memory routes, scratch SQL, real message store/Redis.

Only the external embedding model is controlled; no authorization/storage stubs.
"""

import asyncio
import threading
from collections.abc import AsyncIterator, Iterator
from contextlib import ExitStack, asynccontextmanager
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from api.apps.services import memory_context_service, memory_revision_service
from api.db.db_models import Memory, Task, TenantLLM, UserTenant
from api.db.joint_services import memory_write_runtime
from api.db.memory_revision_models import ManagedMemoryState, MemoryEntry, MemoryEntryRevision, MemoryMutationOperation, MemoryPublicationOutbox, MemoryRevisionSource, MemorySourceState
from api.db.memory_writer_models import MemoryWriterAdmission, MemoryWriterFence
from api.db.services.llm_service import LLMBundle
from api.db.services.tenant_llm_service import TenantLLMService
from common import settings
from common.constants import RetCode
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


class FixedContextEmbedding:
    """Control only the provider; model authorization and bundle lifecycle stay real."""

    def encode_queries(self, text: str) -> tuple[list[float], int]:
        return [1.0, 0.0], 0


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


def request(env: dict[str, Any], method: str, path: str, *, actor: int = 1, **kwargs: Any) -> Any:
    response = env["client"].request(method, env["base"] + "/api/v1" + path, headers={"Authorization": f"Bearer {env['jwts'][actor]}"}, timeout=30, **kwargs)
    assert response.status_code == 200, response.status_code
    return response.json()


def adopt_source_for_management(env: dict[str, Any], memory_id: str, source_id: int = 900) -> None:
    source = request(env, "GET", f"/memories/{memory_id}/sources/{source_id}/state")["data"]
    registered = request(env, "POST", f"/memories/{memory_id}/sources/{source_id}/adoptions", json={"operation_id": f"manage-source-{source_id}", "expected_content_sha256": source["content_sha256"]})
    assert registered["retcode"] == 0


def snapshot(env: dict[str, Any]) -> Any:
    with Session(env["engine"]) as db:
        rows = [m.to_dict() for m in db.scalars(sa.select(Memory).where(Memory.id.in_(list(env["memories"].values()))).order_by(Memory.id))]
        tasks = [t.to_dict() for t in db.scalars(sa.select(Task).where(Task.doc_id.in_(list(env["memories"].values()))))]
    messages = {key: MessageService.get_by_message_id(mid, 900, env["tenants"][key]) for key, mid in env["memories"].items()}
    redis = REDIS_CONN.REDIS
    return rows, tasks, messages, redis.xrange(env["queue"]), redis.get(f"{env['queue']}:memory"), {mid: redis.get(mid) for mid in env["memories"].values()}


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


def fixed_context_embedding(monkeypatch: pytest.MonkeyPatch) -> None:
    def model(cls: type[TenantLLMService], config: dict[str, Any], lang: str = "Chinese", **kwargs: Any) -> FixedContextEmbedding:
        assert config["mdl_type"] == "embedding" and config["tenant_id"]
        assert type(config["id"]) is int and config["id"] > 0
        return FixedContextEmbedding()

    monkeypatch.setattr(TenantLLMService, "model_instance_from_config", classmethod(model))


def test_optional_reads_enforce_real_auth_and_have_no_write_effects(memory_api: dict[str, Any]) -> None:
    env = memory_api
    before = snapshot(env)
    for key in ("private", "foreign"):
        mid = env["memories"][key]
        for method, path, body in [
            ("GET", f"/memories/{mid}/capabilities", None),
            ("GET", f"/memories/{mid}/messages", None),
            ("GET", f"/memories/{mid}/sources/900", None),
            ("POST", f"/memories/{mid}/context", {"query": "private data"}),
        ]:
            response = env["client"].request(method, env["base"] + "/api/v1" + path, headers={"Authorization": f"Bearer {env['jwts'][1]}"}, **({"json": body} if body else {}), timeout=30)
            assert response.status_code == 404 and response.json()["data"] is False, response.text
        assert request(env, "GET", f"/messages/search?memory_id={mid}&query=private&include_sources=true")["data"] == []
    assert snapshot(env) == before


def test_recent_page_exact_filters_ties_and_change_detection(memory_api: dict[str, Any]) -> None:
    env, mid = memory_api, memory_api["memories"]["personal"]
    insert_read_messages(env, [{"message_id": 910}, {"message_id": 911}, {"message_id": 912, "user_id": "other"}, {"message_id": 913, "status": False}])
    path = f"/memories/{mid}/messages?message_type=semantic&user_id=subject&status=true&limit=1"
    first = request(env, "GET", path)["data"]
    assert [int(message["message_id"]) for message in first["messages"]] == [911]
    second = request(env, "GET", path, params={"cursor": first["next_cursor"]})["data"]
    assert [int(message["message_id"]) for message in second["messages"]] == [910]
    assert second["next_cursor"] is None
    disabled = request(env, "GET", f"/messages?memory_id={mid}&message_type=semantic&user_id=subject&status=false")["data"]
    assert [int(message["message_id"]) for message in disabled] == [913]
    insert_read_messages(env, [{"message_id": 914}])
    changed = env["client"].get(env["base"] + "/api/v1" + path, params={"cursor": first["next_cursor"]}, headers={"Authorization": f"Bearer {env['jwts'][1]}"}, timeout=30)
    capabilities = request(env, "GET", f"/memories/{mid}/capabilities")["data"]["recent_messages"]
    if capabilities["pagination"] == "bounded_scan_cursor":
        assert changed.status_code == 409 and changed.json()["retcode"] == 409
    else:
        assert capabilities["pagination"] == "live_seek_cursor"
        assert changed.status_code == 200
        assert [int(message["message_id"]) for message in changed.json()["data"]["messages"]] == [910]
    assert request(env, "PUT", f"/messages/{mid}:911", json={"status": False})["retcode"] == 0
    stale_boundary = env["client"].get(env["base"] + "/api/v1" + path, params={"cursor": first["next_cursor"]}, headers={"Authorization": f"Bearer {env['jwts'][1]}"}, timeout=30)
    assert stale_boundary.status_code == 409


def test_source_excerpt_hash_scope_and_membership_readback(memory_api: dict[str, Any]) -> None:
    env, mid = memory_api, memory_api["memories"]["team"]
    path = f"/memories/{mid}/sources/900"
    first = request(env, "GET", path + "?limit=2")["data"]
    assert first["state"] == "available" and first["excerpt"] == "te"
    second = request(env, "GET", path, params={"offset": first["next_offset"], "content_sha256": first["content_sha256"]})["data"]
    assert first["excerpt"] + second["excerpt"] == "team"
    assert MessageService.update_message({"message_id": 900}, {"content": "changed source"}, env["tenants"]["team"], mid)
    changed = env["client"].get(env["base"] + "/api/v1" + path, params={"content_sha256": first["content_sha256"]}, headers={"Authorization": f"Bearer {env['jwts'][1]}"}, timeout=30)
    assert changed.status_code == 409
    with Session(env["engine"]) as db:
        db.execute(sa.update(UserTenant).where(UserTenant.user_id == env["owners"][1], UserTenant.tenant_id == env["owners"][0]).values(status="0"))
        db.commit()
    denied = env["client"].get(env["base"] + "/api/v1" + path, headers={"Authorization": f"Bearer {env['jwts'][1]}"}, timeout=30)
    assert denied.status_code == 404 and "changed source" not in denied.text


def test_query_context_source_state_budget_and_read_only_storage(memory_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    from common.token_utils import num_tokens_from_string
    from memory.services.context import serialize_query_context

    env, mid = memory_api, memory_api["memories"]["personal"]
    fixed_context_embedding(monkeypatch)
    insert_read_messages(env, [{}, {"message_id": 911, "source_id": 9999, "content": "Unsupported statement"}, {"message_id": 912, "user_id": "other", "content": "Wrong subject"}])
    before = snapshot(env)
    body = {"query": "decision", "budget_tokens": 1024, "candidate_limit": 20, "user_id": "subject"}
    data = request(env, "POST", f"/memories/{mid}/context", json=body)["data"]
    assert data["source_snapshot_only"] is True
    assert num_tokens_from_string(serialize_query_context(data)) <= data["usage"]["estimated_tokens"] <= 1024
    selected = {int(item["message_id"]): item for item in data["items"]}
    assert 910 in selected and 911 not in selected and 912 not in selected
    assert selected[910]["source"]["state"] == "available"
    assert selected[910]["source"]["verification"] == "provenance_only"
    assert selected[910]["role_status"] == "legacy_role_unknown"
    assert snapshot(env) == before
    adopt_source_for_management(env, mid)
    assert request(env, "PUT", f"/messages/{mid}:900", json={"status": False})["retcode"] == 0
    after_disable = request(env, "POST", f"/memories/{mid}/context", json=body)["data"]
    assert after_disable["items"] == []
    # The durable source gate excludes its children before candidate top_n.
    # Resolver counts describe the remaining candidates, not all excluded rows.
    with Session(env["engine"]) as db:
        assert db.get(MemorySourceState, (mid, 900)).state == "inactive"


def test_query_context_exact_empty_scope_precedes_candidate_limit(memory_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env, mid = memory_api, memory_api["memories"]["personal"]
    fixed_context_embedding(monkeypatch)
    insert_read_messages(
        env,
        [
            {"message_id": 901, "message_type": "raw", "source_id": 0, "user_id": "", "agent_id": "", "session_id": "", "content": "Unattributed receipt"},
            {"message_id": 911, "source_id": 901, "user_id": "", "agent_id": "", "session_id": "", "content": "Unattributed decision"},
            {"message_id": 912, "content": "Another subject's competing decision"},
        ],
    )
    data = request(
        env,
        "POST",
        f"/memories/{mid}/context",
        json={"query": "decision", "candidate_limit": 1, "max_items": 1, "user_id": "", "agent_id": "", "session_id": "", "budget_tokens": 1024},
    )["data"]
    assert len(data["items"]) == 1
    assert all(data["items"][0][field] == "" for field in ("user_id", "agent_id", "session_id"))


def test_query_context_returns_sql_connection_before_embedding(memory_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env, mid = memory_api, memory_api["memories"]["personal"]
    fixed_context_embedding(monkeypatch)
    sessions: list[AsyncSession] = []
    build = memory_context_service.query_context
    calls: list[str] = []
    loop_threads: list[int] = []
    factory = TenantLLMService.model_instance_from_config
    get_vector = memory_context_service.get_vector

    async def capture(db: AsyncSession, *args: Any, **kwargs: Any) -> dict[str, Any]:
        sessions.append(db)
        loop_threads.append(threading.get_ident())
        return await build(db, *args, **kwargs)

    def construct(cls: type[TenantLLMService], config: dict[str, Any], lang: str = "Chinese", **kwargs: Any) -> FixedContextEmbedding:
        assert sessions and not sessions[-1].in_transaction()
        assert threading.get_ident() != loop_threads[-1]
        calls.append("construct")
        return factory(config, lang=lang, **kwargs)

    def vector(query: str, bundle: LLMBundle, **kwargs: Any) -> Any:
        assert bundle.db is None
        return get_vector(query, bundle, **kwargs)

    def encode(model: FixedContextEmbedding, query: str) -> tuple[list[float], int]:
        assert sessions and not sessions[-1].in_transaction()
        calls.append(query)
        return [1.0, 0.0], 0

    monkeypatch.setattr(memory_context_service, "query_context", capture)
    monkeypatch.setattr(TenantLLMService, "model_instance_from_config", classmethod(construct))
    monkeypatch.setattr(memory_context_service, "get_vector", vector)
    monkeypatch.setattr(FixedContextEmbedding, "encode_queries", encode)
    assert request(env, "POST", f"/memories/{mid}/context", json={"query": "connection ownership"})["retcode"] == 0
    assert calls == ["construct", "connection ownership"]


def test_query_context_storage_schema_failure_returns_500_without_writes(memory_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """A real Milvus field/dimension rejection must not become an empty success."""
    env, mid = memory_api, memory_api["memories"]["personal"]
    fixed_context_embedding(monkeypatch)
    monkeypatch.setattr(FixedContextEmbedding, "encode_queries", lambda self, query: ([1.0, 0.0, 0.0], 0))
    before = snapshot(env)
    response = env["client"].post(env["base"] + f"/api/v1/memories/{mid}/context", json={"query": "dimension mismatch"}, headers={"Authorization": f"Bearer {env['jwts'][1]}"}, timeout=30)
    assert response.status_code == 500 and response.json()["retcode"] == int(RetCode.SERVER_ERROR), response.text
    assert response.json()["data"] is False
    assert snapshot(env) == before


def test_visible_count_filters_and_pages(memory_api: dict[str, Any]) -> None:
    env, mids = memory_api, memory_api["memories"]
    for filters in ["", f"&tenant_id={env['owners'][0]},{env['owners'][1]},{env['owners'][2]}", f"&owner_ids={','.join(env['owners'])}"]:
        result = request(env, "GET", "/memories?page_size=1" + filters)["data"]
        assert result["total_count"] == 2 and len(result["memory_list"]) == 1
        second = request(env, "GET", "/memories?page_size=1&page=2" + filters)["data"]
        assert {result["memory_list"][0]["id"], second["memory_list"][0]["id"]} == {mids["team"], mids["personal"]}
    for filters in [f"tenant_id={env['owners'][2]}", f"owner_ids={env['owners'][2]}", f"tenant_id={env['owners'][0]}&owner_ids={env['owners'][1]}", "owner_ids="]:
        assert request(env, "GET", "/memories?" + filters)["data"] == {"memory_list": [], "total_count": 0}
    assert request(env, "GET", "/memories", actor=0)["data"]["total_count"] == 2


@pytest.mark.parametrize("key", ["private", "foreign"])
def test_denied_direct_and_mixed_writes_have_zero_effect(memory_api: dict[str, Any], key: str) -> None:
    env, mid = memory_api, memory_api["memories"][key]
    before = snapshot(env)
    for method, path, body in [
        ("GET", f"/memories/{mid}/config", None),
        ("GET", f"/memories/{mid}", None),
        ("PUT", f"/memories/{mid}", {"description": "unauthorized"}),
        ("DELETE", f"/memories/{mid}", None),
        ("GET", f"/messages/{mid}:900/content", None),
        ("DELETE", f"/messages/{mid}:900", None),
        ("PUT", f"/messages/{mid}:900", {"status": False}),
    ]:
        response = request(env, method, path, **({"json": body} if body is not None else {}))
        assert response["retcode"] != 0 and response["data"] is False
    for ids in [[mid], [env["memories"]["personal"], mid]]:
        assert request(env, "POST", "/messages", json={"memory_id": ids, "agent_id": "a", "session_id": "s", "user_input": "denied", "agent_response": "denied"})["retcode"] != 0
    assert request(env, "GET", f"/messages?memory_id={mid}")["data"] == []
    assert request(env, "GET", f"/messages/search?memory_id={mid}&query=test")["data"] == []
    assert snapshot(env) == before


def test_team_and_personal_messages_real_readback(memory_api: dict[str, Any]) -> None:
    env, mids = memory_api, memory_api["memories"]
    mixed = ",".join(mids.values())
    assert {m["memory_id"] for m in request(env, "GET", f"/messages?memory_id={mixed}")["data"]} == {mids["team"], mids["personal"]}
    assert {m["memory_id"] for m in request(env, "GET", f"/messages/search?memory_id={mixed}&query=personal&similarity_threshold=0")["data"]} == {mids["team"], mids["personal"]}
    for key in ["team", "personal"]:
        mid = mids[key]
        assert request(env, "GET", f"/memories/{mid}/config")["retcode"] == 0
        assert request(env, "GET", f"/memories/{mid}")["data"]["messages"]["total_count"] == 1
        assert request(env, "GET", f"/messages/{mid}:900/content")["data"]["content"] == key
        assert request(env, "PUT", f"/memories/{mid}", json={"description": "authorized"})["retcode"] == 0
        with Session(env["engine"]) as db:
            assert db.get(Memory, mid).description == "authorized"
        unchanged = request(env, "PUT", f"/messages/{mid}:900", json={"status": False})
        assert unchanged["retcode"] != 0 and unchanged["data"] is False
        assert MessageService.get_by_message_id(mid, 900, env["tenants"][key])["status"]
        adopt_source_for_management(env, mid)
        assert request(env, "PUT", f"/messages/{mid}:900", json={"status": False})["retcode"] == 0
        assert MessageService.get_by_message_id(mid, 900, env["tenants"][key]) is None
        assert request(env, "DELETE", f"/messages/{mid}:900")["retcode"] == 0
        assert MessageService.get_by_message_id(mid, 900, env["tenants"][key]) is None
        with Session(env["engine"]) as db:
            assert db.get(MemorySourceState, (mid, 900)).state == "forgotten"
    mid = mids["personal"]
    assert request(env, "POST", "/messages", json={"memory_id": [mid, mid], "agent_id": "a", "session_id": "new", "user_input": "hello", "agent_response": "world"})["retcode"] == 0
    with Session(env["engine"]) as db:
        assert list(db.scalars(sa.select(Task).where(Task.doc_id == mid))) == []
        append = db.scalar(sa.select(MemoryWriterAdmission).where(MemoryWriterAdmission.memory_id == mid, MemoryWriterAdmission.kind == "append_raw"))
        assert append is not None and append.publication_state == "ready" and len(append.message_ids) == 1
        raw_id = append.message_ids[0]
    assert "hello" in MessageService.get_by_message_id(mid, raw_id, env["tenants"]["personal"])["content"]
    assert REDIS_CONN.REDIS.xlen(env["queue"]) == 0
    assert request(env, "DELETE", f"/memories/{mid}")["retcode"] == 0
    with Session(env["engine"]) as db:
        assert db.get(Memory, mid) is None
    assert request(env, "GET", f"/messages?memory_id={mid}")["data"] == []
    assert not settings.msgStoreConn.index_exists_strict(index_name(env["tenants"]["personal"]), mid)


def test_membership_state_readback(memory_api: dict[str, Any]) -> None:
    env = memory_api
    for role, status, allowed in [("owner", "1", True), ("admin", "1", True), ("normal", "1", True), ("invite", "1", False), ("normal", "0", False)]:
        with Session(env["engine"]) as db:
            db.execute(sa.update(UserTenant).where(UserTenant.user_id == env["owners"][1], UserTenant.tenant_id == env["owners"][0]).values(role=role, status=status))
            db.commit()
        mid = env["memories"]["team"]
        before = snapshot(env)
        assert (request(env, "GET", f"/memories/{mid}/config")["retcode"] == 0) is allowed
        assert request(env, "GET", "/memories")["data"]["total_count"] == (2 if allowed else 1)
        assert snapshot(env) == before


def test_create_binds_actor_and_config_models_bind_owner(memory_api: dict[str, Any]) -> None:
    env = memory_api
    result = request(env, "POST", "/memories", json={"name": "created", "memory_type": ["raw"], "embd_id": "auth-embedding", "llm_id": "auth-chat", "tenant_id": env["owners"][2]})
    assert result["retcode"] == 0
    mid = result["data"]["id"]
    env["created"].append(mid)
    with Session(env["engine"]) as db:
        row = db.get(Memory, mid)
        assert row.tenant_id == env["owners"][1] and row.permissions == "me"
        assert row.tenant_llm_id == env["model_ids"]["member-chat"]
    team = env["memories"]["team"]
    before = snapshot(env)
    for new_config in [{"tenant_llm_id": env["model_ids"]["foreign-chat"]}, {"tenant_embd_id": env["model_ids"]["foreign-embedding"]}, {"llm_id": "nonexistent-model"}]:
        assert request(env, "PUT", f"/memories/{team}", json=new_config)["retcode"] != 0
        assert snapshot(env) == before
    assert request(env, "PUT", f"/memories/{team}", json={"llm_id": "auth-chat", "tenant_llm_id": env["model_ids"]["foreign-chat"]})["retcode"] == 0
    with Session(env["engine"]) as db:
        assert db.get(Memory, team).tenant_llm_id == env["model_ids"]["owner-chat"]


def test_stale_foreign_model_reference_denied_by_sql(memory_api: dict[str, Any]) -> None:
    from api.db.joint_services.tenant_model_service import get_model_config_by_id

    env = memory_api
    before = snapshot(env)
    with Session(env["engine"]) as db:
        for kind in ["chat", "embedding"]:
            with pytest.raises(LookupError):
                get_model_config_by_id(db, env["model_ids"][f"foreign-{kind}"], tenant_id=env["owners"][0])
    assert snapshot(env) == before


def test_message_attribution_real_auth_storage_queue_and_denials(memory_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """Real credential resolution must agree with independently read RAW storage."""
    env = memory_api
    mid = env["memories"]["team"]
    tenant = env["tenants"]["team"]
    calls: list[str] = []
    encode = FixedEmbedding.encode

    def counted_encode(self: FixedEmbedding, texts: list[str]) -> tuple[list[list[float]], int]:
        calls.extend(texts)
        return encode(self, texts)

    monkeypatch.setattr(FixedEmbedding, "encode", counted_encode)
    cases = [
        ({"Authorization": f"Bearer {env['jwts'][1]}", "X-API-Key": env["keys"][0]}, "forged", env["owners"][1]),
        ({"Authorization": f"Bearer {env['jwts'][0]}"}, "forged", env["owners"][0]),
        ({"Authorization": f"Bearer {env['keys'][1]}"}, " external-subject ", " external-subject "),
        ({"Authorization": f"Bearer {env['keys'][0]}"}, "", ""),
        ({"Authorization": f"Bearer {env['keys'][1]}"}, "   ", "   "),
        ({"Authorization": f"Bearer {env['keys'][1]}"}, None, ""),
    ]
    for headers, requested, expected in cases:
        session_id = uuid4().hex
        body = {
            "memory_id": [mid],
            "agent_id": "a",
            "session_id": session_id,
            "user_input": "attribution",
            "agent_response": "verified",
            "auth_via_api_token": True,
            "auth_type": "API",
            "authentication": {"source": "sdk_api_token"},
        }
        if requested is not None:
            body["user_id"] = requested
        response = env["client"].post(env["base"] + "/api/v1/messages", headers=headers, json=body, timeout=30)
        assert response.status_code == 200 and response.json()["retcode"] == 0, response.text
        with Session(env["engine"]) as db:
            assert db.scalar(sa.select(sa.func.count()).select_from(Task).where(Task.doc_id == mid)) == 0
            admissions = list(db.scalars(sa.select(MemoryWriterAdmission).where(MemoryWriterAdmission.memory_id == mid)))
            ids = [identity for admission in admissions for identity in admission.message_ids]
        matched = [MessageService.get_by_message_id(mid, identity, tenant) for identity in ids]
        stored = next(row for row in matched if row is not None and row["session_id"] == session_id)
        assert REDIS_CONN.REDIS.xlen(env["queue"]) == 0
        assert stored["user_id"] == expected
        assert stored["session_id"] == session_id
    assert len(calls) == len(cases)
    before = snapshot(env)
    for headers, ids in [
        ({"Authorization": f"Bearer {env['jwts'][1]}"}, [mid, env["memories"]["private"]]),
        ({"Authorization": f"Bearer {env['keys'][1]}"}, [mid, env["memories"]["foreign"]]),
        ({"Authorization": f"Bearer {env['keys'][2]}"}, [mid]),
        ({"Authorization": "Bearer invalid-token"}, [mid]),
        ({"Authorization": "Bearer invalid.jwt.token", "X-API-Key": env["keys"][0]}, [mid]),
        ({"Authorization": "", "X-API-Key": env["keys"][0]}, [mid]),
    ]:
        response = env["client"].post(
            env["base"] + "/api/v1/messages",
            headers=headers,
            json={"memory_id": ids, "agent_id": "a", "session_id": "denied", "user_input": "no", "agent_response": "no", "user_id": env["owners"][0], "auth_via_api_token": True},
            timeout=30,
        )
        assert response.status_code in {200, 401}
        if response.status_code == 200:
            assert response.json()["retcode"] != 0
        assert snapshot(env) == before
        assert len(calls) == len(cases)
