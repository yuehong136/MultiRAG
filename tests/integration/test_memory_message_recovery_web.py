"""Native Message handles recover through the existing read-only Web contract."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from agent.component import message as message_module
from core.utils.redis_conn import REDIS_CONN
from tests.support.agent_update_release import release_api as release_api
from tests.support.memory_message_producer_runtime import save_native_message_sources
from tests.support.memory_message_recovery_web import memory_effects as memory_effects
from tests.support.memory_message_recovery_web import no_sql_mutations as no_sql_mutations
from tests.support.memory_message_recovery_web import recovery_consumer as recovery_consumer
from tests.support.memory_writes_http import WriteEmbedding, api_request, result
from tests.support.memory_writes_http import writes_api as writes_api

QUERY = "仅用于内部检查，先核对来源；没有发布授权。"
ANSWER = "冻结回答含 {literal} 和 {{ not_a_template }}，不表示工具已经运行。"


def test_native_message_http_source_uses_authenticated_context_and_frozen_text(writes_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = writes_api
    produced = save_native_message_sources(env, monkeypatch, targets={"structured": (env["memory_id"], True)}, query=QUERY, answer=ANSWER, actor=1)
    source = produced["sources"]["structured"]
    assert source["scope"] == {"user_id": env["owners"][1], "agent_id": produced["agent_id"], "session_id": produced["session_id"]}
    assert source["provenance"]["provenance"]["operation_id"] == source["operation"]["operation_id"]
    assert source["provenance"]["provenance"]["content_verified"] is False
    assert source["provenance"]["event_time"]["status"] == "unknown"
    assert len(env["embedding_calls"]) == 1 and not env["chat_calls"] and REDIS_CONN.REDIS.xlen(env["queue"]) == 0
    # Explicit adoption activates this same bank before a legitimate later run.
    result(
        api_request(
            env,
            "POST",
            f"/memories/{env['memory_id']}/sources/{source['raw_source_id']}/adoptions",
            json={"operation_id": "explicit-source-adoption", "expected_content_sha256": source["provenance"]["content_sha256"]},
        )
    )
    correction = save_native_message_sources(
        env, monkeypatch, targets={"structured": (env["memory_id"], True)}, query=QUERY + " 后续更正。", answer=ANSWER + " 后续回答。", actor=1, agent_id=produced["agent_id"]
    )
    assert correction["agent_id"] == produced["agent_id"] and correction["task_id"] != produced["task_id"]
    assert correction["sources"]["structured"]["operation"]["operation_id"] != source["operation"]["operation_id"]
    assert len(env["embedding_calls"]) == 2 and not env["chat_calls"] and REDIS_CONN.REDIS.xlen(env["queue"]) == 0


@pytest.mark.external_consumer
def test_native_message_ready_handle_web_reopen_is_read_only_and_acl_checked(writes_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    env, mid = writes_api, writes_api["memory_id"]
    produced = save_native_message_sources(env, monkeypatch, targets={"structured": (mid, True)}, query=QUERY, answer=ANSWER, actor=1)
    source = produced["sources"]["structured"]
    operation_id = source["operation"]["operation_id"]
    before = memory_effects(env)
    with no_sql_mutations() as mutations:
        first = recovery_consumer(env, tmp_path, source, operation_id, suffix="first")
        reopened = recovery_consumer(env, tmp_path, source, operation_id, suffix="reopened")
        assert first == reopened
        path = f"/memories/{mid}/operations/{operation_id}"
        assert api_request(env, "GET", path, actor=2).status_code == 404
        assert api_request(env, "GET", path, token="invalid-session-token").status_code == 401
        assert api_request(env, "GET", f"/memories/{env['memories']['foreign']}/operations/{operation_id}").status_code == 404
        assert api_request(env, "GET", f"/memories/{mid}/operations/missing-{uuid4().hex}").status_code == 404
        assert result(api_request(env, "GET", path))["raw_source_id"] == source["raw_source_id"]
    assert mutations == [] and memory_effects(env) == before
    assert int(source["raw_source_id"]) > 2**53 and isinstance(source["output"]["memory_operations"][0]["raw_source_id"], str)
    env["journal"].event(
        "message-recovery-read-only-effects",
        memory_id=mid,
        operation_id=operation_id,
        sql_sha256=before["sql_sha256"],
        sql_mutations=mutations,
        embedding_delta=0,
        chat_delta=0,
        queue_delta=0,
        counter_unchanged=True,
    )


@pytest.mark.external_consumer
def test_unknown_native_message_web_get_and_same_slot_replay_do_not_retry_io(writes_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    env, mid = writes_api, writes_api["memory_id"]
    attempts: list[list[str]] = []
    saves: list[tuple[bool, str]] = []
    unavailable = True
    original_encode = WriteEmbedding.encode
    original_save = message_module.Message._save_to_memory

    def encode(model: WriteEmbedding, texts: list[str]) -> tuple[list[list[float]], int]:
        attempts.append(list(texts))
        if unavailable:
            raise RuntimeError("Controlled unavailable embedding before an unknown operation is reconciled")
        return original_encode(model, texts)

    async def save_then_replay(node: message_module.Message, content: Any) -> tuple[bool, str]:
        # Reuse the actual authenticated execution while its cancellation fence
        # is still live. Closed HTTP runtimes correctly reject further work.
        nonlocal unavailable
        first = await original_save(node, content)
        first_operations = node.output("memory_operations")
        unavailable = False
        replay = await original_save(node, content)
        saves.extend((first, replay))
        assert node.output("memory_operations") == first_operations
        return replay

    monkeypatch.setattr(WriteEmbedding, "encode", encode)
    monkeypatch.setattr(message_module.Message, "_save_to_memory", save_then_replay)
    produced = save_native_message_sources(env, monkeypatch, targets={"structured": (mid, True)}, query=QUERY, answer=ANSWER, actor=1, allow_unsettled=True)
    source = produced["sources"]["structured"]
    operation_id = source["operation"]["operation_id"]
    assert source["operation"]["status"] == "persistence_unknown" and len(attempts) == 1
    assert len(saves) == 2 and all(saved[0] is False for saved in saves)
    before = memory_effects(env)
    with no_sql_mutations() as mutations:
        recovery_consumer(env, tmp_path, source, operation_id, suffix="unknown-first")
        recovery_consumer(env, tmp_path, source, operation_id, suffix="unknown-reopened")
        current = result(api_request(env, "GET", f"/memories/{mid}/operations/{operation_id}"))
        assert current["status"] == "persistence_unknown" and current["can_reconcile"] is True
    assert mutations == [] and memory_effects(env) == before and len(attempts) == 1
    recovered = result(api_request(env, "POST", f"/memories/{mid}/operations/{operation_id}/reconciliations", json={}))
    assert recovered["operation_id"] == operation_id and recovered["status"] == recovered["index_sync_status"] == "ready"
    assert int(recovered["raw_source_id"]) > 2**53 and len(attempts) == 2 and len(env["embedding_calls"]) == 1
    settled = memory_effects(env)
    recovery_consumer(env, tmp_path, source, operation_id, suffix="settled-same-handle")
    assert memory_effects(env) == settled and len(attempts) == 2
    assert not env["chat_calls"] and REDIS_CONN.REDIS.xlen(env["queue"]) == 0
    env["journal"].event(
        "message-recovery-explicit-reconciliation",
        memory_id=mid,
        operation_id=operation_id,
        initial_attempts=1,
        implicit_retry_attempts=0,
        explicit_reconciliation_attempts=1,
        raw_source_id=recovered["raw_source_id"],
    )
