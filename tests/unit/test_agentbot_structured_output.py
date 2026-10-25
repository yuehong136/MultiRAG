"""Registered beta completion aggregation, framing and source ownership."""

import json
import sys
from collections.abc import AsyncGenerator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.utils.agent_completion import parse_agent_completion_chunk
from api.utils.api_utils import async_beta_token_required


def _frame(event: dict[str, Any]) -> str:
    return "data:" + json.dumps(event) + "\n\n"


def test_sse_multiline_data_and_metadata_are_decoded_without_losing_frames() -> None:
    chunk = ': heartbeat\r\n\r\nevent: message\r\nid: 1\r\nretry: 100\r\ndata: {"event":"message",\r\ndata: "data":{"content":"hello"}}\r\n\r\ndata:{"event":"message_end","data":{}}\r\n\r\ndata:[DONE]\r\n\r\n'
    assert parse_agent_completion_chunk(chunk) == [{"event": "message", "data": {"content": "hello"}}, {"event": "message_end", "data": {}}]


def _source(client: TestClient, monkeypatch: pytest.MonkeyPatch, chunks: list[Any]) -> dict[str, Any]:
    state: dict[str, Any] = {"closed": False}

    async def completion(**kwargs: Any) -> AsyncGenerator[Any, None]:
        state["request"] = kwargs
        try:
            for chunk in chunks:
                yield chunk
        finally:
            state["closed"] = True

    client.app.dependency_overrides[async_beta_token_required] = lambda: "tenant-unit"
    monkeypatch.setattr(sys.modules["api.apps.restful_apis.bot"], "agent_completion", completion)
    return state


@pytest.mark.parametrize("batch_frames", [False, True])
@pytest.mark.parametrize("terminal", ["workflow_finished", "user_inputs"])
def test_registered_beta_retains_structured_reference_trace_and_terminal(client: TestClient, monkeypatch: pytest.MonkeyPatch, batch_frames: bool, terminal: str) -> None:
    events = [
        {"event": "workflow_started", "data": {}},
        {"event": "message", "data": {"content": "full ", "reference": {"chunks": [1]}}},
        {"event": "node_finished", "data": {"component_id": "Agent:report", "outputs": {"structured": {"count": 2}}}},
        {"event": "node_finished", "data": {"component_id": "Agent:flag", "outputs": {"structured": False}}},
        {"event": "message", "data": {"content": "answer"}},
        {"event": terminal, "data": {"inputs": {"approval": {"type": "boolean"}}, "tips": "Approve", "reference": {"doc_aggs": [2]}}, "session_id": "session"},
    ]
    if terminal == "user_inputs":
        events.append({"event": "message_end", "data": {}, "session_id": "session"})
    chunks = [_frame(event) for event in events]
    if batch_frames:
        chunks = [": keepalive\n\n" + "".join(chunks) + "data:[DONE]\n\n"]
    state = _source(client, monkeypatch, chunks)
    response = client.post("/api/v1/agentbots/agent-1/completions?release=true", json={"query": "question", "stream": False, "return_trace": True})
    body = response.json()
    assert response.status_code == 200 and body["code"] == 0, body
    assert body["data"]["event"] == terminal and body["data"]["session_id"] == "session"
    data = body["data"]["data"]
    assert data["content"] == "full answer"
    assert data["reference"] == {"chunks": [1], "doc_aggs": [2]}
    assert data["structured"] == {"Agent:report": {"count": 2}, "Agent:flag": False}
    assert [item["component_id"] for item in data["trace"]] == ["Agent:report", "Agent:flag"]
    assert data["inputs"] == {"approval": {"type": "boolean"}} and data["tips"] == "Approve"
    assert state["closed"] and state["request"]["tenant_id"] == "tenant-unit" and state["request"]["release"] == "true"


@pytest.mark.parametrize("structured", [None, {}, [], False, 0, ""])
def test_registered_beta_keeps_explicit_structured_values(client: TestClient, monkeypatch: pytest.MonkeyPatch, structured: Any) -> None:
    state = _source(
        client,
        monkeypatch,
        [
            _frame({"event": "message", "data": {"content": "answer"}}),
            {"event": "node_finished", "data": {"component_id": "node", "outputs": {"structured": structured}}},
            _frame({"event": "message_end", "data": {}}),
        ],
    )
    response = client.post("/api/v1/agentbots/agent-1/completions", json={"stream": False})
    assert response.json()["code"] == 0
    result = response.json()["data"]["data"]
    assert result["structured"]["node"] == structured and type(result["structured"]["node"]) is type(structured)
    assert "trace" not in result and state["closed"]


def test_registered_beta_omits_structured_when_no_node_emits_it(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    state = _source(client, monkeypatch, [_frame({"event": "message", "data": {"content": "answer"}}), _frame({"event": "message_end", "data": {}})])
    response = client.post("/api/v1/agentbots/agent-1/completions", json={"stream": False, "release": False})
    assert response.json()["code"] == 0
    assert "structured" not in response.json()["data"]["data"]
    assert state["request"]["release"] is False and state["closed"]


@pytest.mark.parametrize("bad", ["data:broken\n\n", "data:[]\n\n", 'data:{"event":"message_end","data":false}\n\n', 'data:{"code":100,"message":"failed","data":false}\n\n', object()])
def test_registered_beta_bad_frame_after_output_fails_and_closes(client: TestClient, monkeypatch: pytest.MonkeyPatch, bad: Any) -> None:
    state = _source(client, monkeypatch, [_frame({"event": "message", "data": {"content": "tentative"}}), bad, _frame({"event": "message_end", "data": {}})])
    response = client.post("/api/v1/agentbots/agent-1/completions", json={"stream": False})
    assert response.json()["code"] != 0 and "data" not in response.json()
    assert state["closed"]


def test_registered_beta_streaming_preserves_frames_and_closes(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    frames = [_frame({"event": "node_finished", "data": {"component_id": "node", "outputs": {"structured": {"count": 2}}}}), _frame({"event": "message_end", "data": {}})]
    state = _source(client, monkeypatch, frames)
    response = client.post("/api/v1/agentbots/agent-1/completions", json={"stream": True})
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.text == "".join(frames) and state["closed"]
