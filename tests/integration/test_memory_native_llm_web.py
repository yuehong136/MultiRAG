"""Actual native LLM→Message receipts use the existing pinned Web recovery."""

import json
from pathlib import Path
from typing import Any

import pytest

from tests.support.agent_update_release import release_api as release_api
from tests.support.memory_message_recovery_web import memory_effects, no_sql_mutations, recovery_consumer
from tests.support.memory_native_llm import run_native_llm
from tests.support.memory_native_llm_http import native_api as native_api
from tests.support.memory_writes_http import api_request
from tests.support.memory_writes_http import writes_api as writes_api


@pytest.mark.external_consumer
@pytest.mark.parametrize("mode", ["stream", "template"])
def test_native_final_message_receipt_reopens_through_existing_web_without_effects(native_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: str) -> None:
    env, mid = native_api, native_api["memory_id"]
    query = "Native receipt test: preserve 0, false, [] and literal braces; no execution claim."
    produced = run_native_llm(
        env,
        monkeypatch,
        memory_ids=[mid],
        query=query,
        model_reference=env["native_model_reference"],
        mode=mode,
        structured=True,
        actor=1,
        message_content="Final selected output: {LLM:answer@content}",
        include_unselected=True,
    )
    # Streaming references use ComponentBase.variable_ref_patt, which consumes
    # the space before the reference. The template renderer keeps that space.
    expected_text = "Final selected output:" + (" " if mode == "template" else "") + env["native_text"]
    assert produced["llm_output"] == env["native_text"] and produced["outputs"]["content"] == expected_text
    source = produced["sources"][0]
    assert source["raw"] is not None and source["operation"] is not None
    stored = json.loads(source["raw"]["content"])
    assert stored["segments"] == [{"role": "user", "content": query}, {"role": "assistant", "content": expected_text}]
    assert source["scope"] == {"user_id": env["owners"][1], "agent_id": produced["agent_id"], "session_id": produced["session_id"]}
    assert len(env["native_calls"]) == 1 and "UNSELECTED NODE" not in env["native_calls"][0]["system"]
    operation_id = source["operation"]["operation_id"]
    actual_message = {"output": produced["outputs"], "trace_item": produced["trace_item"]}
    before = memory_effects(env)
    with no_sql_mutations() as writes:
        opened = recovery_consumer(env, tmp_path, actual_message, operation_id, suffix="native-open")
        reopened = recovery_consumer(env, tmp_path, actual_message, operation_id, suffix="native-reopen")
        assert opened == reopened and opened["operations"][0]["status"] == "ready"
        assert api_request(env, "GET", f"/memories/{mid}/operations/{operation_id}", actor=2).status_code == 404
    assert writes == [] and memory_effects(env) == before and len(env["native_calls"]) == 1
    env["journal"].event("native-llm-message-web-readback", mode=mode, operation_id=operation_id, canonical_message_trace=True, final_output_saved=True, recovery_sql_dml=0, recovery_model_calls=0)
