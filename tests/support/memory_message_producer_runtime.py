"""Owned native Message producers through the authenticated Agent HTTP runtime."""

from __future__ import annotations

import asyncio
import json
import re
from copy import deepcopy
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from agent.canvas import Canvas
from agent.component import message as message_module
from api.db.db_models import Memory, Task, UserCanvas
from api.db.joint_services import memory_write_runtime
from api.db.memory_revision_models import ManagedMemoryState
from api.db.memory_writer_models import MemoryWriterAdmission
from common.constants import MemoryType
from memory.revisions.storage import ManagedMessageStore
from tests.support.memory_writes_http import api_request, result


def native_message_dsl(targets: dict[str, tuple[str, bool]]) -> dict[str, Any]:
    """Read frozen answer data through Begin, never interpret it as a template."""
    if not targets or any(not re.fullmatch(r"[a-z][a-z_]{0,31}", name) or type(structured) is not bool for name, (_, structured) in targets.items()):
        raise ValueError("invalid_native_message_targets")
    nodes = ["Message:" + name for name in targets]
    components: dict[str, Any] = {
        "begin": {
            "obj": {"component_name": "Begin", "params": {"prologue": "", "inputs": {"answer": {"type": "line", "optional": False}}}},
            "downstream": [nodes[0]],
            "upstream": [],
        }
    }
    for index, (name, (memory_id, structured)) in enumerate(targets.items()):
        components[nodes[index]] = {
            "obj": {"component_name": "Message", "params": {"content": ["{begin@answer}"], "stream": False, "memory_ids": [memory_id], "structured_memory": structured}},
            "upstream": [nodes[index - 1] if index else "begin"],
            "downstream": nodes[index + 1 : index + 2],
        }
    return {"components": components, "path": [], "history": [], "retrieval": []}


def _trace_outputs(value: dict[str, Any]) -> dict[str, dict[str, Any]]:
    outputs = {}
    for node in value["data"]["data"]["trace"]:
        component_id = node["component_id"]
        if isinstance(component_id, str) and component_id.startswith("Message:"):
            spans = node["trace"]
            assert len(spans) == 1 and spans[0]["component_id"] == component_id
            assert spans[0]["component_type"] == "Message", "native_trace_missing_canonical_message_type"
            outputs[component_id.removeprefix("Message:")] = deepcopy(spans[0]["outputs"])
    return outputs


def save_native_message_sources(
    env: dict[str, Any],
    patch: pytest.MonkeyPatch,
    *,
    targets: dict[str, tuple[str, bool]],
    query: str,
    answer: str,
    actor: int = 0,
    agent_id: str | None = None,
    allow_unsettled: bool = False,
    captured_canvases: list[Canvas] | None = None,
) -> dict[str, Any]:
    """Save already generated text once per target using real authenticated Canvas.

    A paired run has one real task/session and one principal. This covers Message
    consumption of frozen answer data, not a Generate component's implementation.
    No append/source HTTP endpoint is used to create either source.
    """
    if not re.fullmatch(r"multirag_test_[0-9a-f]{12}", str(env["engine"].url.database)):
        raise ValueError("native_message_requires_owned_scratch_database")
    if not isinstance(query, str) or not query.strip() or not isinstance(answer, str) or not answer.strip():
        raise ValueError("native_message_requires_real_text")
    dsl = native_message_dsl(targets)
    owner = env["owners"][actor]
    agent_id = agent_id or uuid4().hex
    with Session(env["engine"]) as reader:
        reader.execute(sa.text("SET TRANSACTION READ ONLY"))
        for mid, _ in targets.values():
            memory = reader.get(Memory, mid)
            assert memory is not None and memory.tenant_id == owner and memory.memory_type == MemoryType.RAW.value
        existing = reader.get(UserCanvas, agent_id)
        if existing is not None:
            stored = json.loads(existing.dsl) if isinstance(existing.dsl, str) else existing.dsl
            assert existing.user_id == owner and stored == dsl, "native_message_agent_targets_changed"
        before = {mid: set(reader.scalars(sa.select(MemoryWriterAdmission.operation_id).where(MemoryWriterAdmission.memory_id == mid))) for mid, _ in targets.values()}
        managed_before = {}
        for mid, _ in targets.values():
            managed = reader.get(ManagedMemoryState, mid)
            assert managed is None or managed.state == "active"
            managed_before[mid] = None if managed is None else (managed.tenant_id, managed.state, managed.generation)
    if existing is None:
        env["journal"].event("reserve-owned-native-message-agent", database=env["engine"].url.database, agent_id=agent_id, owner=owner, targets={name: mid for name, (mid, _) in targets.items()})
        with Session(env["engine"]) as writer:
            writer.add(UserCanvas(id=agent_id, user_id=owner, title="Owned native Message source producer", dsl=dsl))
            writer.commit()
    constructed: list[Canvas] = []
    original_init = Canvas.__init__

    def capture(canvas: Canvas, *args: Any, **kwargs: Any) -> None:
        original_init(canvas, *args, **kwargs)
        constructed.append(canvas)

    with patch.context() as runtime_patch:
        runtime_patch.setattr(message_module, "async_db_connection", memory_write_runtime.async_db_connection)
        runtime_patch.setattr(Canvas, "__init__", capture)
        response = api_request(
            env,
            "POST",
            "/agents/chat/completions",
            actor=actor,
            json={"agent_id": agent_id, "release": False, "query": query, "inputs": {"answer": {"value": answer}}, "stream": False, "return_trace": True},
        )
    assert response.status_code == 200 and response.json()["retcode"] == 0, response.text
    body = response.json()
    outputs = _trace_outputs(body)
    trace_items = {item["component_id"].removeprefix("Message:"): deepcopy(item) for item in body["data"]["data"]["trace"] if item["component_id"].startswith("Message:")}
    assert set(outputs) == set(targets) and len(constructed) == 1, body
    canvas = constructed[0]
    context = canvas.get_run_context()
    assert context is not None and context.principal is not None and context.platform_user_id == owner and context.tenant_id == owner
    assert context.mcp_read_only is True
    if captured_canvases is not None:
        captured_canvases.append(canvas)
    sources = {}
    for name, (mid, structured) in targets.items():
        output = outputs[name]
        assert output["content"] == answer, output
        with Session(env["engine"]) as reader:
            reader.execute(sa.text("SET TRANSACTION READ ONLY"))
            admissions = list(reader.scalars(sa.select(MemoryWriterAdmission).where(MemoryWriterAdmission.memory_id == mid, MemoryWriterAdmission.operation_id.not_in(before[mid]))))
            assert len(admissions) == 1, output
            admission = admissions[0]
            assert admission.kind == "append_raw" and admission.actor_id == owner and admission.tenant_id == owner
            operation_id, source_id = admission.operation_id, admission.source_message_id
            managed = reader.get(ManagedMemoryState, mid)
            assert (None if managed is None else (managed.tenant_id, managed.state, managed.generation)) == managed_before[mid]
            assert reader.scalar(sa.select(sa.func.count()).select_from(Task).where(Task.doc_id == mid)) == 0
        operation = result(api_request(env, "GET", f"/memories/{mid}/operations/{operation_id}", actor=actor))
        if structured:
            assert len(output["memory_operations"]) == 1 and output["memory_operations"][0]["operation_id"] == operation_id
        else:
            assert not output.get("memory_operations")
        value: dict[str, Any] = {"output": output, "trace_item": trace_items[name], "operation": operation, "raw_source_id": str(source_id) if source_id is not None else None}
        if operation["status"] == operation["index_sync_status"] == "ready":
            assert source_id is not None and int(source_id) > 2**53 and not output.get("memory_error")
            raw = asyncio.run(ManagedMessageStore(owner, mid).read(source_id))
            assert raw is not None and raw["user_id"] == owner and raw["agent_id"] == agent_id and raw["session_id"] == canvas.task_id
            expected = json.dumps(
                {"schema": "structured_source", "schema_version": 1, "segments": [{"role": "user", "content": query}, {"role": "assistant", "content": answer}], "event_time": None},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            assert raw["content"] == (expected if structured else f"User Input: {query}\nAgent Response: {answer}")
            provenance = result(api_request(env, "GET", f"/memories/{mid}/sources/{source_id}/provenance", actor=actor, params={"include_segments": True}))
            value.update(raw=raw, provenance=provenance, scope={key: raw[key] for key in ("user_id", "agent_id", "session_id")})
        else:
            assert allow_unsettled and output.get("memory_error"), output
        sources[name] = value
    env["journal"].event(
        "native-message-source-readback",
        agent_id=agent_id,
        task_id=canvas.task_id,
        authenticated_actor=owner,
        targets={
            name: {"memory_id": targets[name][0], "operation_id": value["operation"]["operation_id"], "raw_source_id": value["raw_source_id"], "status": value["operation"]["status"]}
            for name, value in sources.items()
        },
        generates_answer=False,
        trace_shapes={name: {"wrapper_keys": sorted(item), "data_keys": sorted(item["trace"][0]), "component_type": item["trace"][0]["component_type"]} for name, item in trace_items.items()},
    )
    return {"agent_id": agent_id, "task_id": canvas.task_id, "session_id": canvas.task_id, "sources": sources, "outputs": outputs, "response": body}


def save_message_pair(
    env: dict[str, Any],
    patch: pytest.MonkeyPatch,
    *,
    memory_ids: dict[str, str],
    query: str,
    answer: str,
    label: str = "native-message-pair",
    agent_id: str | None = None,
    actor: int = 0,
    order: tuple[str, str] = ("legacy", "structured"),
    allow_unsettled: bool = False,
) -> dict[str, Any]:
    if set(memory_ids) != {"legacy", "structured"} or len(set(memory_ids.values())) != 2 or set(order) != set(memory_ids):
        raise ValueError("native_message_requires_two_owned_banks")
    env["journal"].event("native-message-pair-start", label=label, order=order, answer_is_frozen_data=True, generation_component_covered=False)
    return save_native_message_sources(
        env, patch, targets={name: (memory_ids[name], name == "structured") for name in order}, query=query, answer=answer, agent_id=agent_id, actor=actor, allow_unsettled=allow_unsettled
    )
