"""Authenticated registered Memory routes, scratch SQL, real message store/Redis.

Only the external embedding model is controlled; no authorization/storage stubs.
"""

import time
from collections.abc import Iterator
from contextlib import ExitStack
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from api.db.db_models import Memory, Task, TenantLLM, UserTenant
from api.db.joint_services import memory_message_service as mms
from api.db.services.llm_service import LLMBundle
from common import settings
from core.utils.redis_conn import REDIS_CONN
from memory.services.messages import MessageService
from tests.support.agent_update_release import release_api as release_api


class FixedEmbedding(LLMBundle):
    def __init__(self, db: Session, tenant_id: str) -> None:
        self.db = db
        self.tenant_id = tenant_id

    def encode_queries(self, text: str) -> tuple[list[float], int]:
        return [1.0, 0.0], 0

    def encode(self, texts: list[str]) -> tuple[list[list[float]], int]:
        return [[1.0, 0.0] for _ in texts], 0


@pytest.fixture
def memory_api(release_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    env = release_api
    owner, member, outsider = env["owners"]
    memories = {key: uuid4().hex for key in ["private", "team", "personal", "foreign"]}
    tenants = {"private": owner, "team": owner, "personal": member, "foreign": outsider}
    queue = f"memory-auth-{uuid4().hex}"
    redis = REDIS_CONN.REDIS
    increment = REDIS_CONN.generate_auto_increment_id
    monkeypatch.setattr(settings, "get_svr_queue_name", lambda priority: queue)
    monkeypatch.setattr(REDIS_CONN, "generate_auto_increment_id", lambda **kwargs: increment(key_prefix=queue, **kwargs))
    monkeypatch.setattr(mms, "_embedding_bundle_for", lambda db, memory: FixedEmbedding(db, memory.tenant_id))
    monkeypatch.setattr(mms, "LLMBundle", lambda db, tenant_id, config: FixedEmbedding(db, tenant_id))
    monkeypatch.setattr(mms, "get_model_config_by_type_and_name", lambda *args: {})
    created: list[str] = []
    model_ids: dict[str, int] = {}
    with ExitStack() as cleanup:

        def remove() -> None:
            for key, mid in memories.items():
                MessageService.delete_index(tenants[key], mid)
                assert not MessageService.has_index(tenants[key], mid)
                redis.delete(mid)
            for mid in created:
                MessageService.delete_index(member, mid)
                redis.delete(mid)
            redis.delete(queue, f"{queue}:memory")
            assert not redis.exists(queue, f"{queue}:memory")
            with Session(env["engine"]) as db:
                db.execute(sa.delete(Task).where(Task.doc_id.in_([*memories.values(), *created])))
                db.execute(sa.delete(Memory).where(Memory.id.in_([*memories.values(), *created])))
                db.execute(sa.delete(TenantLLM).where(TenantLLM.id.in_(list(model_ids.values()))))
                db.commit()
                assert db.scalar(sa.select(sa.func.count()).select_from(Memory).where(Memory.id.in_([*memories.values(), *created]))) == 0
                assert db.scalar(sa.select(sa.func.count()).select_from(Task).where(Task.doc_id.in_([*memories.values(), *created]))) == 0

        cleanup.callback(remove)
        with Session(env["engine"]) as db:
            db.add(UserTenant(id=uuid4().hex, user_id=member, tenant_id=owner, role="normal", status="1", invited_by=owner))
            for key, mid in memories.items():
                db.add(Memory(id=mid, tenant_id=tenants[key], name=key, permissions="team" if key == "team" else "me", memory_type=1, embd_id="controlled", llm_id="controlled"))
            for label, tenant in [("owner", owner), ("member", member), ("foreign", outsider)]:
                for kind in ["chat", "embedding"]:
                    model = TenantLLM(tenant_id=tenant, llm_factory="controlled", llm_name=f"auth-{kind}", mdl_type=kind, api_key="unused-test-value")
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
        yield dict(env, memories=memories, tenants=tenants, queue=queue, created=created, model_ids=model_ids)


def request(env: dict[str, Any], method: str, path: str, *, actor: int = 1, **kwargs: Any) -> Any:
    response = env["client"].request(method, env["base"] + "/api/v1" + path, headers={"Authorization": f"Bearer {env['jwts'][actor]}"}, timeout=30, **kwargs)
    assert response.status_code == 200, response.status_code
    return response.json()


def snapshot(env: dict[str, Any]) -> Any:
    with Session(env["engine"]) as db:
        rows = [m.to_dict() for m in db.scalars(sa.select(Memory).where(Memory.id.in_(list(env["memories"].values()))).order_by(Memory.id))]
        tasks = [t.to_dict() for t in db.scalars(sa.select(Task).where(Task.doc_id.in_(list(env["memories"].values()))))]
    messages = {key: MessageService.get_by_message_id(mid, 900, env["tenants"][key]) for key, mid in env["memories"].items()}
    redis = REDIS_CONN.REDIS
    return rows, tasks, messages, redis.xrange(env["queue"]), redis.get(f"{env['queue']}:memory"), {mid: redis.get(mid) for mid in env["memories"].values()}


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
        assert request(env, "PUT", f"/messages/{mid}:900", json={"status": False})["retcode"] == 0
        assert not MessageService.get_by_message_id(mid, 900, env["tenants"][key])["status"]
        assert request(env, "DELETE", f"/messages/{mid}:900")["retcode"] == 0
        assert MessageService.get_by_message_id(mid, 900, env["tenants"][key])["forget_at"]
    mid = mids["personal"]
    assert request(env, "POST", "/messages", json={"memory_id": [mid, mid], "agent_id": "a", "session_id": "new", "user_input": "hello", "agent_response": "world"})["retcode"] == 0
    with Session(env["engine"]) as db:
        tasks = list(db.scalars(sa.select(Task).where(Task.doc_id == mid)))
        assert len(tasks) == 1
    assert "hello" in MessageService.get_by_message_id(mid, int(tasks[0].digest), env["tenants"]["personal"])["content"]
    assert REDIS_CONN.REDIS.xlen(env["queue"]) == 1
    assert request(env, "DELETE", f"/memories/{mid}")["retcode"] == 0
    with Session(env["engine"]) as db:
        assert db.get(Memory, mid) is None
    # HTTP authorization rejects the deleted resource immediately. The backend
    # uses bounded consistency, so direct physical deletion readback may lag.
    assert request(env, "GET", f"/messages?memory_id={mid}")["data"] == []
    deadline = time.monotonic() + 15
    while MessageService.get_recent_messages([env["tenants"]["personal"]], [mid], "", "", 10):
        assert time.monotonic() < deadline, "deleted messages remained in storage"
        time.sleep(0.2)


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
