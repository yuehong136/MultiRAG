"""Actual authenticated LLM→Message consumers, with controlled provider output."""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from typing import Any

import pytest
from sqlalchemy.orm import Session

from agent.canvas import Canvas
from agent.component import message as message_module
from api.db.db_models import Memory, Task
from common.constants import MemoryType
from core.utils.redis_conn import REDIS_CONN
from memory.revisions.storage import ManagedMessageStore
from tests.support.agent_update_release import release_api as release_api
from tests.support.memory_native_llm import run_native_llm
from tests.support.memory_native_llm_http import native_api as native_api
from tests.support.memory_writes_http import WriteEmbedding, api_request, result
from tests.support.memory_writes_http import writes_api as writes_api


@pytest.mark.parametrize("mode", ["stream", "plain", "template"])
@pytest.mark.parametrize("structured", [False, True], ids=["legacy-default", "structured-opt-in"])
def test_native_llm_selected_final_text_reaches_actual_raw_and_provenance(native_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, mode: str, structured: bool) -> None:
    env = native_api
    query = "仅合成演练资料：请保留限制、0、false、[]；不要声称通过核验。"
    with Session(env["engine"]) as db:
        bank = db.get(Memory, env["memory_id"])
        assert bank is not None
        bank.memory_type = MemoryType.RAW.value | MemoryType.SEMANTIC.value
        db.commit()
    marker = "[展示包装]\n{LLM:answer@content}\n[结束]"
    produced = run_native_llm(
        env, monkeypatch, memory_ids=[env["memory_id"]], query=query, model_reference=env["native_model_reference"], mode=mode, structured=structured, actor=1, message_content=marker
    )
    assert produced["llm_output"] == env["native_text"]
    assert produced["outputs"]["content"] == marker.replace("{LLM:answer@content}", env["native_text"])
    assert len(env["native_calls"]) == 1 and env["native_calls"][0]["history"][-1]["content"] == query
    source = produced["sources"][0]
    assert source["raw"] is not None and source["scope"]["user_id"] == env["owners"][1]
    assert env["embedding_calls"] == [source["raw"]["content"]] and not env["chat_calls"]
    if structured:
        assert source["provenance"]["format"] == "structured"
        assert source["provenance"]["provenance"]["speaker_identity_verified"] is False
        assert source["provenance"]["provenance"]["content_verified"] is False
        assert source["task_ids"] == [] and REDIS_CONN.REDIS.xlen(env["queue"]) == 0
    else:
        assert source["provenance"]["format"] == "legacy_combined" and source["provenance"]["role_status"] == "role_unknown"
        assert len(source["task_ids"]) == 1 and REDIS_CONN.REDIS.xlen(env["queue"]) == 1
        queued = REDIS_CONN.REDIS.xrange(env["queue"])[0][1]
        task = json.loads(queued.get("message", queued.get(b"message")))
        from api.db.joint_services import memory_write_runtime

        assert asyncio.run(memory_write_runtime.handle_coordinated_task(task))
        assert len(env["chat_calls"]) == 1 and len(env["native_calls"]) == 1
        with Session(env["engine"]) as reader:
            completed = reader.get(Task, task["id"])
            assert completed is not None and completed.progress == 1 and completed.digest == source["raw_source_id"]
        inventory = asyncio.run(ManagedMessageStore(env["owners"][1], env["memory_id"]).inventory())
        derived = [row for row in inventory if row["message_type"] == "semantic" and row["source_id"] == int(source["raw_source_id"])]
        assert len(derived) == 1 and derived[0]["content"] == "A controlled derived memory."
        assert len(env["embedding_calls"]) == 2


@pytest.mark.parametrize("mode", ["stream", "plain", "template"])
def test_repeated_selected_reference_does_not_generate_twice_or_consume_unselected_node(native_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    env = native_api
    produced = run_native_llm(
        env,
        monkeypatch,
        memory_ids=[env["memory_id"]],
        query="Selected source only.",
        model_reference=env["native_model_reference"],
        mode=mode,
        structured=True,
        actor=1,
        include_unselected=True,
        message_content="{LLM:answer@content}\n{LLM:answer@content}",
    )
    assert len(env["native_calls"]) == 1 and "UNSELECTED NODE" not in env["native_calls"][0]["system"]
    assert produced["outputs"]["content"] == env["native_text"] + "\n" + env["native_text"]
    assert len(env["embedding_calls"]) == 1 and not env["chat_calls"]


def test_cancel_during_native_model_io_never_saves_partial_output(native_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = native_api
    env["native_started"], env["native_release"] = Event(), Event()
    canvases: list[Canvas] = []
    env["journal"].event("reserve-native-controlled-request-thread", model_calls_allowed=1)
    with ThreadPoolExecutor(max_workers=1) as worker:
        pending = worker.submit(
            run_native_llm,
            env,
            monkeypatch,
            memory_ids=[env["memory_id"]],
            query="Cancel before a final visible answer.",
            model_reference=env["native_model_reference"],
            actor=1,
            structured=True,
            allow_failure=True,
            captured_canvases=canvases,
        )
        try:
            assert env["native_started"].wait(20) and len(canvases) == 1
            response = api_request(env, "POST", f"/tasks/{canvases[0].task_id}/cancel", actor=1)
            assert response.status_code == 200 and response.json().get("retcode", response.json().get("code")) == 0, response.text
        finally:
            env["native_release"].set()
        produced = pending.result(timeout=30)
    assert len(env["native_calls"]) == 1 and env["embedding_calls"] == [] and env["chat_calls"] == []
    assert produced["sources"][0]["operation"] is None and produced["sources"][0]["task_ids"] == []
    assert REDIS_CONN.REDIS.xlen(env["queue"]) == 0
    env["journal"].event("native-controlled-request-thread-finished", raw_saves=0, request_finished=True)


def test_unknown_save_replaying_real_message_does_not_regenerate_native_model(native_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = native_api
    original_encode, original_save = WriteEmbedding.encode, message_module.Message._save_to_memory
    original_invoke = message_module.Message._invoke_async
    attempts = []
    replaying = False
    unavailable = True

    def encode(model: WriteEmbedding, texts: list[str]) -> tuple[list[list[float]], int]:
        attempts.append(list(texts))
        if unavailable:
            raise RuntimeError("Controlled unavailable RAW embedding, preserve original operation")
        return original_encode(model, texts)

    async def save_then_repeat_message(node: message_module.Message, content: Any) -> tuple[bool, str]:
        nonlocal replaying, unavailable
        first = await original_save(node, content)
        if replaying:
            return first
        operations = node.output("memory_operations")
        unavailable, replaying = False, True
        await original_invoke(node)
        assert node.output("memory_operations") == operations
        return first

    monkeypatch.setattr(WriteEmbedding, "encode", encode)
    monkeypatch.setattr(message_module.Message, "_save_to_memory", save_then_repeat_message)
    produced = run_native_llm(
        env,
        monkeypatch,
        memory_ids=[env["memory_id"]],
        query="Unknown RAW write must not regenerate the answer.",
        model_reference=env["native_model_reference"],
        mode="plain",
        structured=True,
        actor=1,
        allow_failure=True,
    )
    source = produced["sources"][0]
    assert source["operation"]["status"] == "persistence_unknown" and len(attempts) == len(env["native_calls"]) == 1
    assert produced["outputs"]["content"] == produced["llm_output"] == env["native_text"]
    assert env["embedding_calls"] == [] and env["chat_calls"] == [] and REDIS_CONN.REDIS.xlen(env["queue"]) == 0
    operation_id = source["operation"]["operation_id"]
    recovered = result(api_request(env, "POST", f"/memories/{env['memory_id']}/operations/{operation_id}/reconciliations", actor=1, json={}))
    assert recovered["operation_id"] == operation_id and recovered["status"] == recovered["index_sync_status"] == "ready"
    assert len(attempts) == 2 and len(env["native_calls"]) == len(env["embedding_calls"]) == 1 and not env["chat_calls"]
    raw = asyncio.run(ManagedMessageStore(env["owners"][1], env["memory_id"]).read(int(recovered["raw_source_id"])))
    assert raw is not None and raw["user_id"] == env["owners"][1] and raw["agent_id"] == produced["agent_id"] and raw["session_id"] == produced["task_id"]
    assert json.loads(raw["content"])["segments"] == [{"role": "user", "content": "Unknown RAW write must not regenerate the answer."}, {"role": "assistant", "content": env["native_text"]}]
