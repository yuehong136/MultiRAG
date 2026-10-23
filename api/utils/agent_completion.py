"""JSON-safe event aggregation shared by native Agent execution surfaces."""

from __future__ import annotations

import copy
import json
from typing import Any


def canvas_json_default(value: Any) -> None:
    """Deferred streaming handles are not public output; reject other opaque objects."""
    if callable(value):
        return None
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def normalize_agent_event(event: Any) -> dict[str, Any]:
    if not isinstance(event, dict):
        raise ValueError("Invalid agent completion event.")
    normalized = json.loads(json.dumps(event, ensure_ascii=False, default=canvas_json_default, allow_nan=False))
    if not isinstance(normalized.get("data", {}), dict):
        raise ValueError("Invalid agent completion event data.")
    normalized.setdefault("data", {})
    return normalized


def parse_agent_completion_chunk(chunk: Any) -> list[dict[str, Any]]:
    """Decode complete SSE chunks, including batched frames and multiline data.

    Keep failure envelopes intact so callers can read their error before
    normalizing successful event data. Malformed frames must fail the run.
    """
    if isinstance(chunk, dict):
        return [chunk]
    if not isinstance(chunk, str):
        raise ValueError("Invalid agent completion chunk.")
    events: list[dict[str, Any]] = []
    data_lines: list[str] = []
    for line in [*chunk.splitlines(), ""]:
        if not line:
            payload = "\n".join(data_lines).strip()
            data_lines = []
            if not payload or payload == "[DONE]":
                continue
            event = json.loads(payload)
            if not isinstance(event, dict):
                raise ValueError("Invalid agent completion event.")
            events.append(event)
        elif not line.startswith(":"):
            field, _, value = line.partition(":")
            if field == "data":
                data_lines.append(value.removeprefix(" "))
            elif field not in {"event", "id", "retry"}:
                raise ValueError("Invalid agent completion SSE field.")
    return events


class AgentCompletionAccumulator:
    def __init__(self, *, return_trace: bool = False) -> None:
        self.return_trace = return_trace
        self.content = ""
        self.reference: dict[str, Any] = {}
        self.structured: dict[str, Any] = {}
        self.trace: list[dict[str, Any]] = []
        self.final: dict[str, Any] = {}
        self.terminal: dict[str, Any] = {}
        self.has_output = False

    def add(self, event: dict[str, Any]) -> None:
        data = event.get("data", {})
        kind = event.get("event")
        if kind == "message":
            content = data.get("content", "")
            if not isinstance(content, str):
                raise ValueError("Invalid agent message content.")
            self.content += content
            self.has_output |= bool(content)
        if reference := data.get("reference"):
            if not isinstance(reference, dict):
                raise ValueError("Invalid agent reference.")
            self.reference.update(copy.deepcopy(reference))
            self.has_output |= any(reference.values())
        if kind == "node_finished":
            outputs = data.get("outputs", {})
            if not isinstance(outputs, dict):
                raise ValueError("Invalid agent node outputs.")
            component_id = data.get("component_id")
            if component_id is not None and "structured" in outputs:
                self.structured[str(component_id)] = copy.deepcopy(outputs["structured"])
                self.has_output |= outputs["structured"] not in (None, "", [], {})
            if self.return_trace:
                self.trace.append({"component_id": component_id, "trace": [copy.deepcopy(data)]})
        if kind in {"message_end", "workflow_finished", "user_inputs"}:
            # Session delivery flushes earlier message ends after committing
            # the pause. Keep the form as the JSON response's terminal event.
            if kind != "message_end" or self.terminal.get("event") != "user_inputs":
                self.terminal = copy.deepcopy(event)
            if kind == "workflow_finished" and isinstance(outputs := data.get("outputs"), dict):
                self.has_output |= any(not key.startswith("_") and value not in (None, "", [], {}) for key, value in outputs.items())
            if kind == "user_inputs":
                self.has_output |= bool(data.get("inputs") or data.get("tips"))
            self.has_output |= bool(data.get("attachment"))
        if kind == "a2ui_command":
            self.has_output |= bool(data.get("commands"))
        self.final = copy.deepcopy(event)

    def finish(self, *, require_terminal: bool = False, require_output: bool = True) -> dict[str, Any]:
        if not self.final:
            raise ValueError("Agent completion returned no events.")
        if require_terminal and not self.terminal:
            raise ValueError("Agent completion ended without a terminal event.")
        if require_output and not self.has_output:
            raise ValueError("Agent completion returned no output.")
        answer = copy.deepcopy(self.terminal or self.final)
        data = answer.setdefault("data", {})
        data.update(content=self.content, reference=copy.deepcopy(self.reference))
        if self.structured:
            data["structured"] = copy.deepcopy(self.structured)
        if self.return_trace:
            data["trace"] = copy.deepcopy(self.trace)
        return answer
