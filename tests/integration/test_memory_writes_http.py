"""Owned real REST, SQL, Milvus and Redis contracts with controlled models only."""

import asyncio
import json
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from api.db.db_models import Memory, Task
from api.db.joint_services import memory_write_runtime
from api.db.memory_revision_models import ManagedMemoryState
from api.db.memory_writer_models import MemoryWriterAdmission
from common import settings
from common.constants import MemoryType
from core.utils.redis_conn import REDIS_CONN
from memory.revisions.storage import ManagedMessageStore
from memory.services.messages import MessageService, index_name
from tests.support.agent_update_release import release_api as release_api
from tests.support.memory_writes_http import api_request, manage_bank, result
from tests.support.memory_writes_http import writes_api as writes_api


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
