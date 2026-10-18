"""Exercise real scheduling, components and serialized state across pauses."""

import json
from collections.abc import Iterator
from typing import Any

import pytest

from agent.canvas import Canvas
from agent.component.message import Message
from core.utils.redis_conn import REDIS_CONN
from tests.support.canvas_path_reset import paused_dsl


@pytest.fixture
def runtime(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[Canvas]]:
    monkeypatch.setattr(Canvas, "is_canceled", lambda self: False)
    monkeypatch.setattr(REDIS_CONN, "delete", lambda *args: None)
    canvases: list[Canvas] = []
    yield canvases
    for canvas in canvases:
        canvas._thread_pool.shutdown(wait=True)


def load(runtime: list[Canvas], dsl: dict[str, Any]) -> Canvas:
    canvas = Canvas(json.dumps(dsl), tenant_id="unit-owner")
    runtime.append(canvas)
    return canvas


def messages(events: list[dict[str, Any]]) -> str:
    return "".join(event["data"]["content"] for event in events if event["event"] == "message")


async def test_two_pauses_keep_completed_outputs_without_recomputing(runtime: list[Canvas], monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    original = Message._invoke

    def track(self: Message, **kwargs: Any) -> None:
        calls.append(self._id)
        original(self, **kwargs)

    monkeypatch.setattr(Message, "_invoke", track)
    canvas = load(runtime, paused_dsl())
    first = [event async for event in canvas.run(query="email")]
    assert messages(first) == "Draft:email"
    assert first[-1]["event"] == "user_inputs" and first[-1]["data"]["tips"] == "Approve Draft:email"
    assert canvas.path == ["UserFillUp:first"]
    canvas = load(runtime, json.loads(str(canvas)))
    canvas.get_component_obj("UserFillUp:first").set_output("obsolete", "old result")
    events = canvas.run(query="confirm", inputs={"approval": {"value": "yes"}})
    assert (await anext(events))["event"] == "workflow_started"
    assert canvas.get_variable_value("UserFillUp:first@obsolete") is None
    assert canvas.get_variable_value("Message:draft@content") == "Draft:email"
    second = [event async for event in events]
    assert messages(second) == "Draft:email/yes"
    assert second[-1]["event"] == "user_inputs" and second[-1]["data"]["tips"] == "Deliver Draft:email/yes"
    assert canvas.path == ["UserFillUp:second"]
    canvas = load(runtime, json.loads(str(canvas)))
    final = [event async for event in canvas.run(query="send", inputs={"delivery": {"value": "sent"}})]
    assert messages(final) == "Draft:email/yes/Draft:email/yes/sent"
    assert final[-1]["event"] == "workflow_finished"
    assert calls == ["Message:draft", "Message:approved", "Message:answer"]
    assert canvas.get_variable_value("Message:unvisited@content") == "saved branch"
    assert [role for role, _ in canvas.history] == ["user", "user", "user", "assistant"]
    assert canvas.globals["sys.conversation_turns"] == 3


async def test_completed_path_is_reset_before_a_fresh_message(runtime: list[Canvas]) -> None:
    dsl = paused_dsl()
    dsl["components"]["Message:draft"]["downstream"] = []
    canvas = load(runtime, dsl)
    assert messages([event async for event in canvas.run(query="one")]) == "Draft:one"
    assert canvas.path == ["begin", "Message:draft"]
    canvas = load(runtime, json.loads(str(canvas)))
    canvas.get_component_obj("Message:draft").set_output("obsolete", "clear on rerun")
    events = canvas.run(query="two")
    assert (await anext(events))["event"] == "workflow_started"
    assert canvas.get_variable_value("Message:draft@content") is None
    assert canvas.get_variable_value("Message:draft@obsolete") is None
    assert canvas.get_variable_value("Message:unvisited@content") == "saved branch"
    assert messages([event async for event in events]) == "Draft:two"
    assert canvas.globals["sys.conversation_turns"] == 2


@pytest.mark.parametrize("keep_memory", [False, True])
def test_full_reset_clears_every_component_and_pending_path(runtime: list[Canvas], keep_memory: bool) -> None:
    dsl = paused_dsl()
    dsl["path"] = ["UserFillUp:second"]
    canvas = load(runtime, dsl)
    canvas.history = [["user", "prior user"]]
    canvas.memory = [["question", "answer", "summary"]]
    for component in canvas.components.values():
        component["obj"].set_input_value("runtime", "prior input")
        component["obj"].set_output("runtime", "prior output")
    canvas.reset(mem=keep_memory)
    assert canvas.path == []
    assert bool(canvas.history) is keep_memory and bool(canvas.memory) is keep_memory
    assert all(component["obj"].get_input_value("runtime") is None and component["obj"].output("runtime") is None for component in canvas.components.values())
    assert canvas.get_variable_value("Message:unvisited@content") is None
