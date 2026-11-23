"""Message consumers must await their actual memory-save helper."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import partial
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from agent.component import message as module
from agent.component.iteration import Iteration, IterationParam
from agent.component.iterationitem import IterationItem, IterationItemParam
from agent.component.loop import Loop, LoopParam
from agent.component.loopitem import LoopItem, LoopItemParam
from agent.component.message import Message, MessageParam
from agent.component.message_memory import operation_metadata, source_operation_id
from api.db.joint_services import memory_write_runtime
from api.identity.run_context import RunContext
from memory.revisions.domain import RevisionValidationError
from tests.support.message_memory_producer import iteration_memory_canvas, memory_run_context, message_memory_canvas


def consumer(mode: str) -> Message:
    component = object.__new__(Message)
    component._id = "Message:answer"
    component._param = MessageParam()
    component._param.content = ["{% if 1 %}answer{% endif %}" if mode == "template" else "answer"]
    component._param.stream = mode != "plain"
    component._param.memory_ids = ["memory"]
    component._canvas = cast(
        Any, SimpleNamespace(_id="agent", task_id="session", is_canceled=lambda: False, get_sys_query=lambda: "question", get_variable_value=lambda _: "", get_run_context=lambda: None)
    )
    return component


@pytest.mark.parametrize("mode", ["plain", "template", "stream"])
async def test_actual_message_consumer_awaits_memory_save(monkeypatch: pytest.MonkeyPatch, async_db: AsyncSession, mode: str) -> None:
    component = consumer(mode)
    calls: list[dict[str, Any]] = []

    @asynccontextmanager
    async def database() -> AsyncIterator[AsyncSession]:
        yield async_db

    async def queue(db: AsyncSession, memory_ids: list[str], message_dict: dict[str, Any]) -> tuple[bool, str]:
        assert db is async_db and memory_ids == ["memory"]
        calls.append(message_dict)
        return True, "saved"

    monkeypatch.setattr(module, "async_db_connection", database)
    monkeypatch.setattr(module, "queue_save_to_memory_task", queue)
    output = await component.invoke_async()
    if mode == "stream":
        assert isinstance(output["content"], partial)
        assert "".join([chunk async for chunk in output["content"]()]) == "answer"
    assert component.output("content") == "answer"
    assert len(calls) == 1
    assert calls[0]["user_input"] == "question" and calls[0]["agent_response"] == "answer"


def test_synchronous_consumer_awaits_the_same_memory_helper(monkeypatch: pytest.MonkeyPatch, async_db: AsyncSession) -> None:
    component = consumer("plain")
    calls: list[str] = []

    @asynccontextmanager
    async def database() -> AsyncIterator[AsyncSession]:
        yield async_db

    async def queue(db: AsyncSession, memory_ids: list[str], message_dict: dict[str, Any]) -> tuple[bool, str]:
        calls.append(message_dict["agent_response"])
        return True, "saved"

    monkeypatch.setattr(module, "async_db_connection", database)
    monkeypatch.setattr(module, "queue_save_to_memory_task", queue)
    assert component.invoke()["content"] == "answer"
    assert calls == ["answer"]


@pytest.mark.parametrize("value", [False, True, 0, 1, "true", None])
def test_structured_memory_is_strictly_boolean_and_defaults_off(value: Any) -> None:
    parameter = MessageParam()
    assert parameter.structured_memory is False
    parameter.content = ["answer"]
    parameter.structured_memory = value
    if type(value) is bool:
        assert parameter.check() is True
    else:
        with pytest.raises(ValueError, match="structured_memory"):
            parameter.check()


def test_stable_slot_excludes_body_but_separates_turn_and_actor() -> None:
    canvas = message_memory_canvas(["memory"], context=memory_run_context("tenant", "actor"), task_id="execution", turn=1, structured_memory=True)
    try:
        message = canvas.get_component_obj("Message:answer")
        assert isinstance(message, Message)
        original = source_operation_id(message)
        message._param.content = ["changed content"]
        canvas.globals["sys.query"] = "changed question"
        assert source_operation_id(message) == original
        canvas.globals["sys.conversation_turns"] = 2
        assert source_operation_id(message) != original
        canvas.globals["sys.conversation_turns"] = 1
        canvas._run_context = memory_run_context("tenant", "another-actor")
        assert source_operation_id(message) != original
    finally:
        canvas._thread_pool.shutdown(wait=True)


def test_parent_iteration_slot_is_bounded_and_does_not_collapse_zero() -> None:
    canvas = iteration_memory_canvas(["memory"], context=memory_run_context("tenant", "actor"), task_id="iteration")
    try:
        canvas.globals["sys.conversation_turns"] = 1
        message = canvas.get_component_obj("Message:answer")
        assert isinstance(message, Message)
        controller = canvas.get_component_obj("IterationItem:outer")
        assert isinstance(controller, IterationItem)
        controller._idx = 1
        first = source_operation_id(message)
        controller._idx = 2
        assert source_operation_id(message) != first
        controller._idx = 0
        with pytest.raises(RevisionValidationError, match="memory_execution_iteration_unavailable"):
            source_operation_id(message)
    finally:
        canvas._thread_pool.shutdown(wait=True)


def test_operation_output_is_exact_and_excludes_content_and_credentials() -> None:
    value = operation_metadata(
        [
            {
                "memory_id": "memory",
                "operation_id": "operation",
                "raw_source_id": 2**53 + 1,
                "status": "ready",
                "retryable": False,
                "extraction_task_id": None,
                "content": "private text",
                "api_key": "private credential",
                "model_payload": {"content": "private text"},
                "error_code": {"content": "not a code"},
            }
        ]
    )
    assert value == [{"memory_id": "memory", "operation_id": "operation", "raw_source_id": "9007199254740993", "status": "ready", "retryable": False, "extraction_task_id": None}]


@pytest.mark.parametrize("outer_kind", ["iteration", "loop"])
def test_nested_parent_slots_and_cycles_remain_distinct(outer_kind: str) -> None:
    canvas = iteration_memory_canvas(["memory"], context=memory_run_context("tenant", "actor"), task_id="nested")
    try:
        canvas.globals["sys.conversation_turns"] = 1
        message = canvas.get_component_obj("Message:answer")
        assert isinstance(message, Message)
        parent: Iteration | Loop
        controller: IterationItem | LoopItem
        if outer_kind == "iteration":
            parent = Iteration(canvas, "Iteration:top", IterationParam())
            controller = IterationItem(canvas, "IterationItem:top", IterationItemParam())
        else:
            parent = Loop(canvas, "Loop:top", LoopParam())
            controller = LoopItem(canvas, "LoopItem:top", LoopItemParam())
        canvas.components[parent._id] = {"obj": parent, "parent_id": None}
        canvas.components[controller._id] = {"obj": controller, "parent_id": parent._id}
        canvas.components["Iteration:outer"]["parent_id"] = parent._id
        inner = canvas.get_component_obj("IterationItem:outer")
        assert isinstance(inner, IterationItem)
        inner._idx, controller._idx = 1, 1
        initial = source_operation_id(message)
        controller._idx = 2
        assert source_operation_id(message) != initial
        controller._idx, inner._idx = 1, 2
        assert source_operation_id(message) != initial
        inner._idx = 1
        assert source_operation_id(message) == initial
        canvas.components[parent._id]["parent_id"] = "Iteration:outer"
        with pytest.raises(RevisionValidationError, match="memory_execution_parent_unavailable"):
            source_operation_id(message)
    finally:
        canvas._thread_pool.shutdown(wait=True)


@pytest.mark.parametrize("denial", ["anonymous", "acl", "actor", "tenant", "agent"])
async def test_structured_scope_denial_precedes_every_external_effect(monkeypatch: pytest.MonkeyPatch, async_db: AsyncSession, denial: str) -> None:
    context = memory_run_context("tenant", "actor", agent_id="expected-agent" if denial == "agent" else None)
    if denial == "anonymous":
        context = RunContext("tenant")
    execute = AsyncMock(return_value=[SimpleNamespace(id="memory", tenant_id="foreign" if denial == "tenant" else "tenant")])
    authorize = AsyncMock(side_effect=PermissionError("Memory not available") if denial == "acl" else AssertionError("authorization should not be reached in this denial"))
    append = AsyncMock(side_effect=AssertionError("external effects must not begin"))
    monkeypatch.setattr(async_db, "execute", execute)
    monkeypatch.setattr(memory_write_runtime, "supports_coordinated_writes", lambda: True)
    monkeypatch.setattr(memory_write_runtime.MemoryWriteProviders, "authorize", authorize)
    monkeypatch.setattr(memory_write_runtime, "append_structured_message", append)
    with pytest.raises(PermissionError):
        await memory_write_runtime.queue_messages(
            async_db,
            ["memory"],
            {"user_id": "forged", "agent_id": "agent", "session_id": "session", "user_input": "question", "agent_response": "answer"},
            operation_id="stable-operation",
            run_context=context,
            structured_memory=True,
            actor_id="forged" if denial == "actor" else None,
        )
    assert execute.await_count == (1 if denial in {"tenant", "acl"} else 0)
    assert authorize.await_count == (1 if denial == "acl" else 0)
    append.assert_not_awaited()


async def test_cancellation_and_unconsumed_stream_do_not_save(monkeypatch: pytest.MonkeyPatch, async_db: AsyncSession) -> None:
    calls = AsyncMock(return_value=(True, "saved"))

    @asynccontextmanager
    async def database() -> AsyncIterator[AsyncSession]:
        yield async_db

    monkeypatch.setattr(module, "async_db_connection", database)
    monkeypatch.setattr(module, "queue_save_to_memory_task", calls)
    canceled = consumer("plain")
    monkeypatch.setattr(canceled._canvas, "is_canceled", lambda: True)
    await canceled.invoke_async()
    calls.assert_not_awaited()
    streaming = consumer("stream")
    output = await streaming.invoke_async()
    generator = output["content"]()
    assert await anext(generator) == "answer"
    await generator.aclose()
    calls.assert_not_awaited()


async def test_mcp_read_only_does_not_replace_memory_acl(monkeypatch: pytest.MonkeyPatch, async_db: AsyncSession) -> None:
    context = memory_run_context("tenant", "actor", mcp_read_only=True)
    append = AsyncMock(return_value={"status": "ready", "index_sync_status": "ready", "queue_state": "not_required"})
    authorize = AsyncMock()
    monkeypatch.setattr(async_db, "execute", AsyncMock(return_value=[SimpleNamespace(id="memory", tenant_id="tenant")]))
    monkeypatch.setattr(memory_write_runtime, "supports_coordinated_writes", lambda: True)
    monkeypatch.setattr(memory_write_runtime.MemoryWriteProviders, "authorize", authorize)
    monkeypatch.setattr(memory_write_runtime, "append_structured_message", append)
    saved = await memory_write_runtime.queue_messages(
        async_db,
        ["memory"],
        {"user_id": "forged", "agent_id": "agent", "session_id": "session", "user_input": "question", "agent_response": "answer"},
        operation_id="stable-operation",
        run_context=context,
        structured_memory=True,
    )
    assert saved[0] is True and context.mcp_read_only is True
    authorize.assert_awaited_once()
    assert append.await_args is not None
    assert append.await_args.args[2]["user_id"] == "actor" and append.await_args.kwargs["actor_id"] == "actor"


async def test_unsupported_backend_never_silently_falls_back_to_legacy(monkeypatch: pytest.MonkeyPatch, async_db: AsyncSession) -> None:
    execute, append = AsyncMock(), AsyncMock()
    monkeypatch.setattr(async_db, "execute", execute)
    monkeypatch.setattr(memory_write_runtime, "supports_coordinated_writes", lambda: False)
    monkeypatch.setattr(memory_write_runtime, "append_message", append)
    with pytest.raises(RevisionValidationError, match="structured_memory_backend_unsupported"):
        await memory_write_runtime.queue_messages(
            async_db,
            ["memory"],
            {"user_id": "forged", "agent_id": "agent", "session_id": "session", "user_input": "question", "agent_response": "answer"},
            operation_id="stable-operation",
            run_context=memory_run_context("tenant", "actor"),
            structured_memory=True,
        )
    execute.assert_not_awaited()
    append.assert_not_awaited()


async def test_unknown_structured_result_keeps_one_safe_original_handle(monkeypatch: pytest.MonkeyPatch, async_db: AsyncSession) -> None:
    canvas = message_memory_canvas(["memory"], context=memory_run_context("tenant", "actor"), task_id="unknown", turn=1, structured_memory=True)
    calls: list[str] = []

    @asynccontextmanager
    async def database() -> AsyncIterator[AsyncSession]:
        yield async_db

    async def queue(db: AsyncSession, memory_ids: list[str], message_dict: dict[str, Any], **kwargs: Any) -> tuple[bool, str]:
        calls.append(kwargs["operation_id"])
        kwargs["operation_results"].append({"memory_id": "memory", "operation_id": calls[-1], "error_code": "memory_write_unconfirmed", "requires_reconciliation": True, "content": "must not escape"})
        return False, "requires reconciliation"

    monkeypatch.setattr(module, "async_db_connection", database)
    monkeypatch.setattr(memory_write_runtime, "queue_messages", queue)
    try:
        message = canvas.get_component_obj("Message:answer")
        await message.invoke_async()
        await message.invoke_async()
        assert len(calls) == 2 and calls[0] == calls[1]
        assert message.output("memory_error") == "requires reconciliation"
        assert message.output("memory_operations") == [{"memory_id": "memory", "operation_id": calls[0], "error_code": "memory_write_unconfirmed", "requires_reconciliation": True}]
        assert message.output("content") == "Answer"
    finally:
        canvas._thread_pool.shutdown(wait=True)


@pytest.mark.parametrize(("user", "assistant", "roles"), [("", "answer", ["assistant"]), ("question", "", ["user"]), ("  ", "\n", [])])
async def test_absent_source_text_is_omitted_without_inventing_speech(monkeypatch: pytest.MonkeyPatch, async_db: AsyncSession, user: str, assistant: str, roles: list[str]) -> None:
    append = AsyncMock(return_value={"status": "ready"})
    factory = AsyncMock(return_value=SimpleNamespace(append_raw=append))
    monkeypatch.setattr(memory_write_runtime, "service_for", factory)
    message = {"user_id": "actor", "agent_id": "agent", "session_id": "session", "user_input": user, "agent_response": assistant}
    if not roles:
        with pytest.raises(RevisionValidationError):
            await memory_write_runtime.append_structured_message(async_db, "memory", message, operation_id="operation", actor_id="actor")
        factory.assert_not_awaited()
        append.assert_not_awaited()
    else:
        await memory_write_runtime.append_structured_message(async_db, "memory", message, operation_id="operation", actor_id="actor")
        assert append.await_args is not None
        arguments = append.await_args.kwargs
        source = json.loads(arguments["content"])
        assert [item["role"] for item in source["segments"]] == roles and source["event_time"] is None
        assert arguments["enqueue_extraction"] is False and arguments["structured_received_at"]
