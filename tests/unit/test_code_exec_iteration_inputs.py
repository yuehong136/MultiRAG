"""CodeExec consumes scoped iteration values without dropping explicit zeroes."""

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from agent.canvas import Canvas
from agent.component.message import Message
from agent.sandbox import client as sandbox_client
from agent.sandbox.providers.base import ExecutionResult
from agent.sandbox.providers.local import LocalProvider


def _canvas(reference: str = "{item}") -> Canvas:
    def node(name: str, params: dict[str, Any] | None = None, parent: str | None = None, upstream: list[str] | None = None, downstream: list[str] | None = None) -> dict[str, Any]:
        return {"obj": {"component_name": name, "params": params or {}}, "parent_id": parent, "upstream": upstream or [], "downstream": downstream or []}

    return Canvas(
        json.dumps(
            {
                "components": {
                    "begin": node("Begin", downstream=["Iteration:values"]),
                    "Iteration:values": node("Iteration", {"items_ref": "env.items"}, upstream=["begin"]),
                    "IterationItem:values": node("IterationItem", parent="Iteration:values", downstream=["CodeExec:child"]),
                    "CodeExec:child": node(
                        "CodeExec",
                        {
                            "lang": "python",
                            "script": "def main(value):\n    return {'value': value}",
                            "arguments": {"value": reference},
                            "outputs": {"result": {"type": "Object", "value": None}, "attachments": {"type": "Array<String>", "value": []}},
                        },
                        parent="Iteration:values",
                        upstream=["IterationItem:values"],
                        downstream=["Message:child"],
                    ),
                    "Message:child": node("Message", {"content": ["{CodeExec:child@result}"], "stream": False}, parent="Iteration:values", upstream=["CodeExec:child"]),
                },
                "globals": {
                    "env.items": [0, False, "", {"nested": [0, False]}],
                    "env.fallback": 42,
                    "sys.query": "",
                    "sys.user_id": "",
                    "sys.conversation_turns": 0,
                    "sys.files": [],
                    "sys.history": [],
                },
                "retrieval": [],
                "path": [],
            }
        )
    )


@pytest.mark.parametrize("value", [0, False, "", None])
def test_explicit_code_argument_overrides_reference_even_when_falsy(monkeypatch: pytest.MonkeyPatch, value: Any) -> None:
    canvas = _canvas("env.fallback")
    tool = canvas.get_component_obj("CodeExec:child")
    monkeypatch.setattr(tool, "_execute_code", lambda **kwargs: kwargs["arguments"])
    try:
        result = tool._invoke(value=value)
        assert result == {"value": value}
        assert type(result["value"]) is type(value)
    finally:
        canvas._thread_pool.shutdown(wait=True)


@pytest.mark.parametrize("reference,expected", [("{item}", 0), ("{result}", 0), ("{index}", 0)])
def test_code_argument_uses_its_direct_iteration_scope(monkeypatch: pytest.MonkeyPatch, reference: str, expected: Any) -> None:
    canvas = _canvas(reference)
    tool = canvas.get_component_obj("CodeExec:child")
    canvas.get_component_obj("IterationItem:values")._invoke()
    monkeypatch.setattr(tool, "_execute_code", lambda **kwargs: kwargs["arguments"])
    try:
        assert tool._invoke() == {"value": expected}
    finally:
        canvas._thread_pool.shutdown(wait=True)


async def test_real_canvas_iteration_runs_local_code_and_message(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SANDBOX_LOCAL_ENABLED", "true")
    provider = LocalProvider()
    provider.initialize({"python_bin": sys.executable, "work_dir": str(tmp_path / "runs"), "timeout": 5})

    def execute(code: str, language: str, timeout: int, arguments: dict[str, Any]) -> ExecutionResult:
        instance = provider.create_instance(language)
        try:
            return provider.execute_code(instance.instance_id, code, language, timeout=timeout, arguments=arguments)
        finally:
            provider.destroy_instance(instance.instance_id)

    monkeypatch.setattr(sandbox_client, "execute_code", execute)
    monkeypatch.setattr(Message, "_save_to_memory", lambda *_: None)
    canvas = _canvas()
    try:
        events = [event async for event in canvas.run()]
        assert not canvas.error
        outputs = [event["data"]["outputs"] for event in events if event["event"] == "node_finished" and event["data"]["component_id"] == "CodeExec:child"]
        assert [item["result"] for item in outputs] == [{"value": value} for value in [0, False, "", {"nested": [0, False]}]]
        assert [type(item["result"]["value"]) for item in outputs] == [int, bool, str, dict]
        assert all(item["attachments"] == [] and not item.get("_ERROR") for item in outputs)
        messages = [event["data"]["outputs"]["content"] for event in events if event["event"] == "node_finished" and event["data"]["component_id"] == "Message:child"]
        assert [json.loads(message) for message in messages] == [item["result"] for item in outputs]
        assert list(provider.work_dir.iterdir()) == []
    finally:
        canvas._thread_pool.shutdown(wait=True)
        for instance_id in list(provider._instances):
            provider.destroy_instance(instance_id)
