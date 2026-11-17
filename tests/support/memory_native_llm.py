"""Owned real LLM→Message executions through the authenticated Agent API."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from agent.canvas import Canvas
from agent.component import llm as llm_module
from agent.component import message as message_module
from api.db.db_models import Memory, Task, UserCanvas
from api.db.joint_services import memory_write_runtime
from api.db.memory_revision_models import ManagedMemoryState
from api.db.memory_writer_models import MemoryWriterAdmission
from memory.revisions.domain import content_hash
from memory.revisions.storage import ManagedMessageStore
from tests.support.memory_writes_http import api_request

DEFAULT_SYSTEM = "Generate only synthetic rehearsal material from the user request. Preserve conditions, negation, exact identifiers, 0, false and empty lists. Do not claim actual execution, verification or publication."


def native_llm_dsl(
    memory_ids: list[str],
    *,
    model_reference: str,
    mode: str = "stream",
    structured: bool = False,
    system_prompt: str = DEFAULT_SYSTEM,
    user_prompt: str = "{sys.query}",
    output_tokens: int = 512,
    message_content: str | None = None,
    include_unselected: bool = False,
) -> dict[str, Any]:
    if mode not in {"stream", "plain", "template"} or type(structured) is not bool or type(include_unselected) is not bool:
        raise ValueError("invalid_native_llm_mode")
    if not memory_ids or len(set(memory_ids)) != len(memory_ids) or any(not isinstance(mid, str) or not mid for mid in memory_ids):
        raise ValueError("invalid_native_llm_memories")
    if not isinstance(model_reference, str) or not model_reference or type(output_tokens) is not int or not 1 <= output_tokens <= 8192:
        raise ValueError("invalid_native_llm_model")
    content = message_content if message_content is not None else "{LLM:answer@content}"
    if mode == "template":
        content = "{% if 1 %}" + content + "{% endif %}"
    params = {
        "llm_id": model_reference,
        "sys_prompt": system_prompt,
        "prompts": [{"role": "user", "content": user_prompt}],
        "max_retries": 0,
        "max_tokens": output_tokens,
        "maxTokensEnabled": True,
        "cite": False,
    }
    components: dict[str, Any] = {
        "begin": {"obj": {"component_name": "Begin", "params": {"prologue": ""}}, "upstream": [], "downstream": ["LLM:answer"]},
        "LLM:answer": {"obj": {"component_name": "LLM", "params": params}, "upstream": ["begin"], "downstream": ["Message:answer"]},
        "Message:answer": {
            "obj": {"component_name": "Message", "params": {"content": [content], "stream": mode == "stream", "memory_ids": memory_ids, "structured_memory": structured}},
            "upstream": ["LLM:answer"],
            "downstream": [],
        },
    }
    if include_unselected:
        components["LLM:unselected"] = {
            "obj": {"component_name": "LLM", "params": {**deepcopy(params), "sys_prompt": "UNSELECTED NODE: must not make a model request."}},
            "upstream": ["begin"],
            "downstream": ["Message:answer"],
        }
        components["begin"]["downstream"].append("LLM:unselected")
        components["Message:answer"]["upstream"].append("LLM:unselected")
    return {"components": components, "path": [], "history": [], "retrieval": []}


def run_native_llm(
    env: dict[str, Any],
    patch: pytest.MonkeyPatch,
    *,
    memory_ids: list[str],
    query: str,
    model_reference: str,
    mode: str = "stream",
    structured: bool = False,
    actor: int = 0,
    agent_id: str | None = None,
    session_id: str | None = None,
    system_prompt: str = DEFAULT_SYSTEM,
    user_prompt: str = "{sys.query}",
    output_tokens: int = 512,
    message_content: str | None = None,
    include_unselected: bool = False,
    allow_failure: bool = False,
    captured_canvases: list[Canvas] | None = None,
    dsl: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """No node replacement or frozen Begin answer; callers own the provider boundary.

    HTTP aggregation is non-streaming so the response includes canonical traces.
    The LLM's actual lazy provider stream is consumed by each selected Message
    mode, rather than claiming provider non-streaming SDK coverage.
    """
    if not re.fullmatch(r"multirag_test_[0-9a-f]{12}", str(env["engine"].url.database)):
        raise ValueError("native_llm_requires_owned_scratch_database")
    if not isinstance(query, str) or not query.strip():
        raise ValueError("native_llm_requires_query")
    owner, agent_id = env["owners"][actor], agent_id or uuid4().hex
    definition = (
        deepcopy(dsl)
        if dsl is not None
        else native_llm_dsl(
            memory_ids,
            model_reference=model_reference,
            mode=mode,
            structured=structured,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            output_tokens=output_tokens,
            message_content=message_content,
            include_unselected=include_unselected,
        )
    )
    with Session(env["engine"]) as reader:
        for mid in memory_ids:
            bank = reader.get(Memory, mid)
            assert bank is not None and bank.tenant_id == owner
        existing = reader.get(UserCanvas, agent_id)
        if existing is not None:
            stored = json.loads(existing.dsl) if isinstance(existing.dsl, str) else existing.dsl
            assert existing.user_id == owner and stored == definition, "native_llm_existing_agent_definition_changed"
        before = {mid: set(reader.scalars(sa.select(MemoryWriterAdmission.operation_id).where(MemoryWriterAdmission.memory_id == mid))) for mid in memory_ids}
        managed_before = {}
        for mid in memory_ids:
            state = reader.get(ManagedMemoryState, mid)
            managed_before[mid] = None if state is None else (state.tenant_id, state.state, state.generation)
    if existing is None:
        env["journal"].event("reserve-owned-native-llm-agent", database=env["engine"].url.database, agent_id=agent_id, owner=owner, memory_ids=memory_ids)
        with Session(env["engine"]) as writer:
            writer.add(UserCanvas(id=agent_id, user_id=owner, title="Owned native LLM memory consumer", dsl=definition))
            writer.commit()
    constructed: list[Canvas] = []
    original_init = Canvas.__init__

    def capture(canvas: Canvas, *args: Any, **kwargs: Any) -> None:
        original_init(canvas, *args, **kwargs)
        constructed.append(canvas)
        if captured_canvases is not None:
            captured_canvases.append(canvas)

    @contextmanager
    def scratch_model_db() -> Iterator[Session]:
        with Session(env["engine"]) as db:
            yield db

    with patch.context() as runtime:
        runtime.setattr(llm_module, "db_connection", scratch_model_db)
        runtime.setattr(message_module, "async_db_connection", memory_write_runtime.async_db_connection)
        runtime.setattr(Canvas, "__init__", capture)
        response = api_request(
            env,
            "POST",
            "/agents/chat/completions",
            actor=actor,
            json={"agent_id": agent_id, "release": False, "query": query, "stream": False, "return_trace": True, **({"session_id": session_id} if session_id is not None else {})},
        )
    body = response.json()
    assert len(constructed) == 1, body
    canvas = constructed[0]
    context = canvas.get_run_context()
    assert context is not None and context.principal is not None and context.platform_user_id == context.tenant_id == owner
    outputs = json.loads(json.dumps(canvas.get_component_obj("Message:answer").output(), default=lambda _: None))
    llm_value = canvas.get_component_obj("LLM:answer").output("content")
    llm_output = llm_value if isinstance(llm_value, str) else None
    envelope = body.get("data")
    payload = envelope.get("data") if isinstance(envelope, dict) else None
    trace_items = payload.get("trace", []) if isinstance(payload, dict) else []
    trace_item = next((deepcopy(item) for item in trace_items if item["component_id"] == "Message:answer"), None)
    if not allow_failure:
        assert response.status_code == 200 and body["retcode"] == 0 and not canvas.error, body
        assert isinstance(llm_output, str) and isinstance(outputs.get("content"), str) and trace_item is not None, body
        inner = trace_item["trace"][0]
        assert inner["component_type"] == "Message" and inner["outputs"]["content"] == outputs["content"]
    sources: list[dict[str, Any]] = []
    for mid in memory_ids:
        with Session(env["engine"]) as reader:
            admissions = list(
                reader.scalars(
                    sa.select(MemoryWriterAdmission).where(MemoryWriterAdmission.memory_id == mid, MemoryWriterAdmission.kind == "append_raw", MemoryWriterAdmission.operation_id.not_in(before[mid]))
                )
            )
            assert len(admissions) <= 1
            state = reader.get(ManagedMemoryState, mid)
            assert (None if state is None else (state.tenant_id, state.state, state.generation)) == managed_before[mid]
            tasks = list(reader.scalars(sa.select(Task.id).where(Task.doc_id == mid)))
            if not admissions:
                assert allow_failure, body
                sources.append({"memory_id": mid, "operation": None, "raw_source_id": None, "raw": None, "provenance": None, "task_ids": tasks})
                continue
            admission = admissions[0]
            assert admission.actor_id == admission.tenant_id == owner
            operation_id, source_id = admission.operation_id, admission.source_message_id
        operation_response = api_request(env, "GET", f"/memories/{mid}/operations/{operation_id}", actor=actor)
        assert operation_response.status_code == 200, operation_response.text
        operation = operation_response.json()["data"]
        record: dict[str, Any] = {"memory_id": mid, "operation": operation, "raw_source_id": str(source_id) if source_id is not None else None, "raw": None, "provenance": None, "task_ids": tasks}
        if operation["status"] == operation["index_sync_status"] == "ready":
            assert source_id is not None
            raw = asyncio.run(ManagedMessageStore(owner, mid).read(source_id))
            assert raw is not None and raw["message_type"] == "raw" and raw["user_id"] == owner and raw["agent_id"] == agent_id and raw["session_id"] == canvas.task_id
            rendered = outputs["content"]
            expected = (
                json.dumps(
                    {"schema": "structured_source", "schema_version": 1, "segments": [{"role": "user", "content": query}, {"role": "assistant", "content": rendered}], "event_time": None},
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                if structured
                else f"User Input: {query}\nAgent Response: {rendered}"
            )
            assert raw["content"] == expected
            provenance = api_request(env, "GET", f"/memories/{mid}/sources/{source_id}/provenance", actor=actor, params={"include_segments": True})
            assert provenance.status_code == 200, provenance.text
            record.update(raw=raw, provenance=provenance.json()["data"], scope={key: raw[key] for key in ("user_id", "agent_id", "session_id")})
            if structured:
                assert outputs["memory_operations"][0]["operation_id"] == operation_id
                assert record["provenance"]["provenance"]["submitted_by"] == owner
        elif not allow_failure:
            raise AssertionError({"operation": operation, "outputs": outputs})
        sources.append(record)
    env["journal"].event(
        "native-llm-source-readback",
        agent_id=agent_id,
        task_id=canvas.task_id,
        mode=mode,
        structured=structured,
        llm_component_covered=True,
        provider_path="lazy_stream_to_message",
        selected_output_sha256=content_hash(outputs["content"]) if isinstance(outputs.get("content"), str) else None,
        sources=[{"memory_id": row["memory_id"], "operation_id": row["operation"]["operation_id"] if row["operation"] else None, "raw_source_id": row["raw_source_id"]} for row in sources],
    )
    return {
        "agent_id": agent_id,
        "task_id": canvas.task_id,
        "session_id": canvas.task_id,
        "response": body,
        "http_status": response.status_code,
        "trace_item": trace_item,
        "outputs": outputs,
        "llm_output": llm_output,
        "canvas": canvas,
        "sources": sources,
        "dsl": definition,
    }
