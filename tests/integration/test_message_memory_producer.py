"""Real Message/Canvas writes with owned SQL, Milvus and Redis; controlled models."""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
import uvicorn  # noqa: F401 -- initialize before the legacy Message asyncio bridge
from sqlalchemy.orm import Session

from agent.component import message as message_module
from api.db.db_models import Memory, Task
from api.db.joint_services import memory_write_runtime
from api.db.memory_revision_models import ManagedMemoryState, MemorySourceState
from api.db.memory_writer_models import MemoryWriterAdmission
from common.constants import MemoryType
from core.utils.redis_conn import REDIS_CONN
from memory.revisions.storage import ManagedMessageStore
from tests.support.agent_update_release import release_api as release_api
from tests.support.memory_structured_sources_http import provenance
from tests.support.memory_writes_http import WriteEmbedding, api_request, result
from tests.support.memory_writes_http import writes_api as writes_api
from tests.support.message_memory_producer import iteration_memory_canvas, memory_run_context, message_memory_canvas


def semantic_bank(env: dict[str, Any]) -> None:
    with Session(env["engine"]) as db:
        db.execute(sa.update(Memory).where(Memory.id == env["memory_id"]).values(memory_type=MemoryType.RAW.value | MemoryType.SEMANTIC.value))
        db.commit()


def run_canvas(
    env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    *,
    task_id: str | None = None,
    mode: str = "plain",
    content: str = "Answer",
    turn: int = 0,
    structured: bool = True,
    denied_actor: bool = False,
    selected: list[str] | None = None,
    iteration: bool = False,
    query: str = "Question",
    expect_error: bool = False,
) -> list[dict[str, Any]]:
    task_id = task_id or "message-producer-" + uuid4().hex
    keys = [task_id + "-logs", task_id + "-cancel"]
    for key in keys:
        env["journal"].event("reserve-redis-key", key=key)
        assert not REDIS_CONN.REDIS.exists(key)
    env["journal"].event("reserve-message-producer-execution", task_id=task_id, memory_ids=selected or [env["memory_id"]], structured_memory=structured)
    context = memory_run_context(env["tenants"]["personal"], env["owners"][2 if denied_actor else 1])
    assert context.mcp_read_only is True
    if iteration:
        canvas = iteration_memory_canvas(selected or [env["memory_id"]], context=context, task_id=task_id, stream=mode == "stream")
    else:
        canvas = message_memory_canvas(selected or [env["memory_id"]], context=context, task_id=task_id, mode=mode, structured_memory=structured, content=content, turn=turn)
    canvas.globals["sys.query"] = query
    monkeypatch.setattr(message_module, "async_db_connection", memory_write_runtime.async_db_connection)

    async def consume() -> list[dict[str, Any]]:
        return [event async for event in canvas.run()]

    try:
        events = asyncio.run(consume())
        assert bool(canvas.error) is expect_error, canvas.error
        return [event["data"]["outputs"] for event in events if event["event"] == "node_finished" and event["data"]["component_id"] == "Message:answer"]
    finally:
        canvas._thread_pool.shutdown(wait=True)
        REDIS_CONN.REDIS.delete(*keys)
        assert not REDIS_CONN.REDIS.exists(*keys)
        env["journal"].event("message-producer-execution-cleaned", task_id=task_id, keys=keys, absent=True)


def read_raw(env: dict[str, Any], source_id: str) -> dict[str, Any]:
    row = asyncio.run(ManagedMessageStore(env["tenants"]["personal"], env["memory_id"]).read(int(source_id)))
    assert row is not None
    return row


@pytest.mark.parametrize("mode", ["plain", "template", "stream"])
def test_message_structured_opt_in_is_raw_only_in_all_real_modes(writes_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    env, mid = writes_api, writes_api["memory_id"]
    semantic_bank(env)
    outputs = run_canvas(env, monkeypatch, mode=mode)
    assert len(outputs) == 1 and outputs[0]["content"] == "Answer" and not outputs[0].get("memory_error")
    operation = outputs[0]["memory_operations"][0]
    assert operation["status"] == operation["index_sync_status"] == "ready" and operation["queue_state"] == "not_required" and operation["extraction_task_id"] is None
    sid = operation["raw_source_id"]
    assert isinstance(sid, str) and int(sid) > 2**53
    raw = read_raw(env, sid)
    assert raw["message_type"] == "raw" and raw["user_id"] == env["owners"][1] and raw["agent_id"] == "memory-agent"
    body = json.loads(raw["content"])
    assert body["segments"] == [{"role": "user", "content": "Question"}, {"role": "assistant", "content": "Answer"}] and body["event_time"] is None
    view = provenance(env, sid)
    assert view["format"] == "structured" and view["role_status"] == "caller_declared"
    assert view["event_time"] == {"value": None, "status": "unknown"}
    assert view["provenance"]["submitted_by"] == env["owners"][1] and view["provenance"]["content_verified"] is view["provenance"]["speaker_identity_verified"] is False
    assert view["provenance"]["operation_id"] == operation["operation_id"]
    assert env["embedding_calls"] == [raw["content"]] and not env["chat_calls"] and REDIS_CONN.REDIS.xlen(env["queue"]) == 0
    with Session(env["engine"]) as reader:
        admission = reader.get(MemoryWriterAdmission, (mid, operation["operation_id"]))
        assert admission is not None and admission.source_message_id == int(sid) and admission.actor_id == env["owners"][1]
        source = reader.get(MemorySourceState, (mid, int(sid)))
        assert source is not None and source.state == "active"
        assert reader.get(ManagedMemoryState, mid) is None
        assert reader.scalar(sa.select(sa.func.count()).select_from(Task).where(Task.doc_id == mid)) == 0


def test_message_without_user_text_records_only_the_real_assistant(writes_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    output = run_canvas(writes_api, monkeypatch, query="")[0]
    raw = read_raw(writes_api, output["memory_operations"][0]["raw_source_id"])
    assert json.loads(raw["content"])["segments"] == [{"role": "assistant", "content": "Answer"}]


def test_message_legacy_default_still_queues_and_extracts(writes_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env, mid = writes_api, writes_api["memory_id"]
    semantic_bank(env)
    output = run_canvas(env, monkeypatch, structured=False)[0]
    assert output["content"] == "Answer" and "memory_operations" not in output and not output.get("memory_error")
    queued = REDIS_CONN.REDIS.xrange(env["queue"])
    assert len(queued) == 1
    payload = json.loads(queued[0][1].get("message", queued[0][1].get(b"message")))
    raw = read_raw(env, str(payload["source_id"]))
    assert raw["content"] == "User Input: Question\nAgent Response: Answer" and raw["user_id"] == env["owners"][1]
    view = provenance(env, str(payload["source_id"]))
    assert view["role_status"] == "role_unknown"
    assert asyncio.run(memory_write_runtime.handle_coordinated_task(payload))
    with Session(env["engine"]) as reader:
        task = reader.get(Task, payload["id"])
        assert task is not None and task.progress == 1
    rows = asyncio.run(ManagedMessageStore(env["tenants"]["personal"], mid).inventory())
    derived = [row for row in rows if row["message_type"] == "semantic" and row["source_id"] == payload["source_id"]]
    assert len(derived) == 1 and env["chat_calls"] and len(env["embedding_calls"]) == 2


def test_same_slot_replay_and_changed_body_do_not_duplicate_but_new_turn_writes(writes_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = writes_api
    task = "message-replay-" + uuid4().hex
    first = run_canvas(env, monkeypatch, task_id=task)[0]["memory_operations"][0]
    original = read_raw(env, first["raw_source_id"])
    count = REDIS_CONN.REDIS.get(env["counter"])
    again = run_canvas(env, monkeypatch, task_id=task)[0]["memory_operations"][0]
    assert again == first and len(env["embedding_calls"]) == 1 and REDIS_CONN.REDIS.get(env["counter"]) == count
    conflict = run_canvas(env, monkeypatch, task_id=task, content="Different answer")[0]
    assert conflict["memory_error"] and conflict["memory_operations"][0]["operation_id"] == first["operation_id"]
    assert conflict["memory_operations"][0]["requires_reconciliation"] is True
    assert read_raw(env, first["raw_source_id"]) == original and len(env["embedding_calls"]) == 1 and REDIS_CONN.REDIS.get(env["counter"]) == count
    next_turn = run_canvas(env, monkeypatch, task_id=task, turn=1)[0]["memory_operations"][0]
    assert next_turn["operation_id"] != first["operation_id"] and next_turn["raw_source_id"] != first["raw_source_id"] and len(env["embedding_calls"]) == 2


@pytest.mark.parametrize("mode", ["plain", "stream"])
def test_actual_iteration_keeps_distinct_slots_and_replay(writes_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    env = writes_api
    task = "message-iteration-" + uuid4().hex
    first = run_canvas(env, monkeypatch, task_id=task, iteration=True, mode=mode)
    assert [item["content"] for item in first] == ["item:first", "item:second"]
    operations = [item["memory_operations"][0] for item in first]
    assert len({item["operation_id"] for item in operations}) == 2 and len(env["embedding_calls"]) == 2
    replay = run_canvas(env, monkeypatch, task_id=task, iteration=True, mode=mode)
    assert [item["memory_operations"][0] for item in replay] == operations and len(env["embedding_calls"]) == 2


@pytest.mark.parametrize("denial", ["tenant", "acl"])
def test_message_denied_scope_does_not_write_any_selected_memory(writes_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, denial: str) -> None:
    env = writes_api
    before = REDIS_CONN.REDIS.get(env["counter"])
    selected = [env["memory_id"], env["memories"]["foreign"]] if denial == "tenant" else [env["memory_id"]]
    output = run_canvas(env, monkeypatch, selected=selected, denied_actor=denial == "acl", expect_error=denial == "acl")[0]
    if denial == "acl":
        assert output["_ERROR"] == "Memory not found."
    else:
        assert output["memory_error"] == "memory_write_not_available"
    assert output["memory_operations"] == []
    assert not env["embedding_calls"] and not env["chat_calls"] and REDIS_CONN.REDIS.get(env["counter"]) == before and REDIS_CONN.REDIS.xlen(env["queue"]) == 0
    with Session(env["engine"]) as reader:
        assert reader.scalar(sa.select(sa.func.count()).select_from(MemoryWriterAdmission).where(MemoryWriterAdmission.memory_id.in_(selected))) == 0


def test_message_unknown_embedding_preserves_operation_and_does_not_retry(writes_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = writes_api
    original_encode = WriteEmbedding.encode

    def fail(self: WriteEmbedding, texts: list[str]) -> tuple[list[list[float]], int]:
        self.calls.extend(texts)
        raise RuntimeError("controlled embedding outcome unknown")

    monkeypatch.setattr(WriteEmbedding, "encode", fail)
    task = "message-unknown-" + uuid4().hex
    first = run_canvas(env, monkeypatch, task_id=task)[0]
    operation = first["memory_operations"][0]
    assert first["memory_error"] and operation["status"] == "persistence_unknown" and operation["index_sync_status"] == "unknown" and len(env["embedding_calls"]) == 1
    count = REDIS_CONN.REDIS.get(env["counter"])
    second = run_canvas(env, monkeypatch, task_id=task)[0]
    assert second["memory_operations"][0]["operation_id"] == operation["operation_id"] and second["memory_error"]
    assert len(env["embedding_calls"]) == 1 and REDIS_CONN.REDIS.get(env["counter"]) == count
    current = result(api_request(env, "GET", f"/memories/{env['memory_id']}/operations/{operation['operation_id']}"))
    assert current["status"] == "persistence_unknown" and current["can_reconcile"] is True and current["needs_operator_reconciliation"] is False
    assert not env["chat_calls"] and REDIS_CONN.REDIS.xlen(env["queue"]) == 0
    monkeypatch.setattr(WriteEmbedding, "encode", original_encode)
    recovered = result(api_request(env, "POST", f"/memories/{env['memory_id']}/operations/{operation['operation_id']}/reconciliations", json={}))
    assert recovered["status"] == recovered["index_sync_status"] == "ready" and recovered["queue_state"] == "not_required"
    assert len(env["embedding_calls"]) == 2 and read_raw(env, recovered["raw_source_id"])
    assert result(api_request(env, "GET", f"/memories/{env['memory_id']}/operations/{operation['operation_id']}"))["status"] == "ready"


def test_concurrent_same_slot_only_the_new_admission_runs_embedding(writes_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = writes_api
    task_id = "message-concurrent-" + uuid4().hex
    keys = [task_id + "-logs", task_id + "-cancel"]
    for key in keys:
        env["journal"].event("reserve-redis-key", key=key)
        assert not REDIS_CONN.REDIS.exists(key)
    context = memory_run_context(env["tenants"]["personal"], env["owners"][1])
    canvases = [message_memory_canvas([env["memory_id"]], context=context, task_id=task_id, turn=1, structured_memory=True) for _ in range(2)]
    started, release = threading.Event(), threading.Event()
    original = WriteEmbedding.encode
    entered: list[list[str]] = []

    def embedding(self: WriteEmbedding, texts: list[str]) -> tuple[list[list[float]], int]:
        entered.append(texts)
        started.set()
        assert release.wait(15), "owned model release was not signaled"
        return original(self, texts)

    monkeypatch.setattr(WriteEmbedding, "encode", embedding)
    monkeypatch.setattr(message_module, "async_db_connection", memory_write_runtime.async_db_connection)

    async def simultaneous() -> tuple[dict[str, Any], dict[str, Any]]:
        first = asyncio.create_task(canvases[0].get_component_obj("Message:answer").invoke_async())
        try:
            assert await asyncio.to_thread(started.wait, 10)
            second = await canvases[1].get_component_obj("Message:answer").invoke_async()
            assert len(entered) == 1 and second["memory_operations"][0]["status"] != "ready" and second["memory_error"]
            release.set()
            return await first, second
        finally:
            release.set()
            await asyncio.gather(first, return_exceptions=True)

    try:
        first, second = asyncio.run(simultaneous())
        saved = first["memory_operations"][0]
        assert saved["status"] == saved["index_sync_status"] == "ready" and len(env["embedding_calls"]) == 1
        assert second["memory_operations"][0]["operation_id"] == saved["operation_id"]
        with Session(env["engine"]) as reader:
            count = reader.scalar(sa.select(sa.func.count()).select_from(MemoryWriterAdmission).where(MemoryWriterAdmission.memory_id == env["memory_id"]))
            assert count == 1
        assert read_raw(env, saved["raw_source_id"])
    finally:
        release.set()
        for canvas in canvases:
            canvas._thread_pool.shutdown(wait=True)
        REDIS_CONN.REDIS.delete(*keys)
        assert not REDIS_CONN.REDIS.exists(*keys)
