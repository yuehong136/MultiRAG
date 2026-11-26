"""``/v1/messages``: what real Anthropic clients send and what they must get back."""

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from api.model_gateway import anthropic, anthropic_stream
from tests.unit.test_model_gateway_relay import _SSE, AUTH, Chunks, Gateway, _completion

MESSAGES = "/v1/messages"
ANTHROPIC = {"x-api-key": "caller-key", "anthropic-version": "2023-06-01"}
_HELLO: dict[str, Any] = {"model": "chat-default", "max_tokens": 64, "messages": [{"role": "user", "content": "hi"}]}
_WEATHER = {"name": "get_weather", "description": "weather", "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}}}
_USAGE = {"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 7, "total_tokens": 107, "prompt_tokens_details": {"cached_tokens": 40}}}


def _chunk(delta: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
    return {"id": "chatcmpl-1", "object": "chat.completion.chunk", "model": "up", "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


def _tool(index: int, arguments: str, name: str | None = None) -> dict[str, Any]:
    call: dict[str, Any] = {"index": index, "function": {"arguments": arguments}}
    if name is not None:
        call.update(id=f"call_{index}", type="function")
        call["function"]["name"] = name
    return call


async def _sse(chunks: list[dict[str, Any]], delay: float = 0.0) -> AsyncIterator[bytes]:
    for chunk in chunks:
        if delay:
            await asyncio.sleep(delay)
        yield b"data: " + json.dumps(chunk).encode() + b"\n\n"
    yield b"data: [DONE]\n\n"


async def _translate(chunks: list[dict[str, Any]], payload: dict[str, Any] | None = None, delay: float = 0.0) -> list[dict[str, Any]]:
    translation = await anthropic.translate_request({**_HELLO, "stream": True, **(payload or {})})
    events = []
    async for raw in translation.stream(_sse(chunks, delay)):
        lines = raw.decode().splitlines()
        assert lines[0] == f"event: {json.loads(lines[1][5:])['type']}"
        events.append(json.loads(lines[1][5:]))
    return events


def _blocks(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The content a client assembles from the events, with block order and nesting checked."""
    blocks: dict[int, dict[str, Any]] = {}
    open_index: int | None = None
    for event in events:
        if event["type"] == "content_block_start":
            assert open_index is None and event["index"] == len(blocks)
            open_index = event["index"]
            blocks[open_index] = {**event["content_block"], "_json": ""}
        elif event["type"] == "content_block_delta":
            assert event["index"] == open_index
            block, delta = blocks[event["index"]], event["delta"]
            assert {"text_delta": "text", "thinking_delta": "thinking", "input_json_delta": "tool_use"}[delta["type"]] == block["type"]
            if delta["type"] == "input_json_delta":
                block["_json"] += delta["partial_json"]
            else:
                key = "text" if delta["type"] == "text_delta" else "thinking"
                block[key] += delta[key]
        elif event["type"] == "content_block_stop":
            assert event["index"] == open_index
            open_index = None
    assert open_index is None
    result = []
    for block in blocks.values():
        raw = block.pop("_json")
        if block["type"] == "tool_use":
            block["input"] = json.loads(raw) if raw else {}
        result.append(block)
    return result


async def test_tool_arguments_that_arrive_with_the_tool_name_are_kept():
    # DashScope and vLLM put the first arguments in the chunk that names the tool.
    events = await _translate(
        [_chunk({"role": "assistant", "content": ""}), _chunk({"tool_calls": [_tool(0, '{"city":', "get_weather")]}), _chunk({"tool_calls": [_tool(0, '"Paris"}')]}), _chunk({}, "tool_calls"), _USAGE],
        {"tools": [_WEATHER]},
    )

    assert [(block["type"], block["name"], block["input"]) for block in _blocks(events)] == [("tool_use", "get_weather", {"city": "Paris"})]
    assert events[0]["type"] == "message_start" and events[0]["message"]["model"] == "chat-default" and events[0]["message"]["id"].startswith("msg_")
    assert [event["type"] for event in events[-2:]] == ["message_delta", "message_stop"]
    assert events[-2]["delta"] == {"stop_reason": "tool_use", "stop_sequence": None}


async def test_parallel_tool_calls_each_get_their_own_block_with_their_arguments():
    one_chunk_each = [_chunk({"tool_calls": [_tool(0, '{"x":1}', "a")]}), _chunk({"tool_calls": [_tool(1, '{"y":2}', "b")]}), _chunk({}, "tool_calls")]
    same_chunk = [_chunk({"content": "calling"}), _chunk({"tool_calls": [_tool(0, '{"x":1}', "a"), _tool(1, '{"y":2}', "b")]}), _chunk({}, "tool_calls")]

    assert [(block["name"], block["input"]) for block in _blocks(await _translate(one_chunk_each))] == [("a", {"x": 1}), ("b", {"y": 2})]
    blocks = _blocks(await _translate(same_chunk))
    assert [block["type"] for block in blocks] == ["text", "tool_use", "tool_use"]
    assert [(block["name"], block["input"]) for block in blocks[1:]] == [("a", {"x": 1}), ("b", {"y": 2})]


async def test_reasoning_becomes_a_thinking_block_before_the_text():
    events = await _translate(
        [_chunk({"role": "assistant", "reasoning_content": "AAA"}), _chunk({"reasoning_content": "BBB"}), _chunk({"content": "Hello"}), _chunk({"content": " world"}), _chunk({}, "stop"), _USAGE]
    )

    assert [(block["type"], block.get("thinking") or block.get("text")) for block in _blocks(events)] == [("thinking", "AAABBB"), ("text", "Hello world")]
    assert events[-2]["delta"]["stop_reason"] == "end_turn"
    # Cached input is reported apart from input_tokens, as Anthropic counts it.
    assert events[-2]["usage"] == {"input_tokens": 60, "output_tokens": 7, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 40}


async def test_concurrent_streams_do_not_see_each_others_events():
    first = [_chunk({"tool_calls": [_tool(0, '{"a":1}', "tool_of_a")]}), _chunk({}, "tool_calls")]
    second = [_chunk({"content": "B1"}), _chunk({"content": "B2"}), _chunk({}, "stop")]

    a, b = await asyncio.gather(_translate(first, delay=0.01), _translate(second, delay=0.01))

    assert [(block["type"], block.get("name")) for block in _blocks(a)] == [("tool_use", "tool_of_a")]
    assert [(block["type"], block.get("text")) for block in _blocks(b)] == [("text", "B1B2")]


async def test_a_silent_upstream_is_bridged_with_pings(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(anthropic_stream, "PING_INTERVAL_SECONDS", 0.02)

    events = await _translate([_chunk({"content": "late"}), _chunk({}, "stop")], delay=0.07)

    kinds = [event["type"] for event in events]
    # The message opens before the first ping, so a client never sees a ping outside a message.
    assert kinds[:2] == ["message_start", "ping"] and kinds.count("message_start") == 1
    assert [block["text"] for block in _blocks([event for event in events if event["type"] != "ping"])] == ["late"]


@pytest.mark.parametrize(("finish", "expected"), [("length", "max_tokens"), ("content_filter", "refusal"), ("stop", "end_turn"), ("function_call", "tool_use")])
async def test_finish_reasons_map_to_anthropics_stop_reasons(finish: str, expected: str):
    events = await _translate([_chunk({"content": "x"}), _chunk({}, finish)])

    assert events[-2]["delta"]["stop_reason"] == expected


async def test_what_an_openai_upstream_cannot_take_is_translated_or_left_out():
    payload = {
        **_HELLO,
        "top_k": 5,
        "stop_sequences": ["END"],
        "metadata": {"user_id": "u1"},
        "thinking": {"type": "enabled", "budget_tokens": 4000},
        "output_config": {"effort": "high"},
        "context_management": {"edits": []},
        "service_tier": "auto",
        "system": "be brief",
        "tools": [_WEATHER, {"type": "bash_20250124", "name": "bash"}, {"type": "web_search_20250305", "name": "web_search", "max_uses": 3}],
        "messages": [
            {"role": "system", "content": "mid system"},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "hi"},
                    {"type": "document", "source": {"type": "text", "media_type": "text/plain", "data": "doc text"}},
                    {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": "JVBERi0="}},
                ],
            },
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "hmm", "signature": "sig"},
                    {"type": "text", "text": "calling"},
                    {"type": "tool_use", "id": "t1", "name": "get_weather", "input": {"city": "Paris"}},
                ],
            },
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": [{"type": "text", "text": "sunny"}]}]},
        ],
    }

    body = (await anthropic.translate_request(payload)).body

    assert not body.keys() & {"top_k", "stop_sequences", "thinking", "output_config", "context_management", "service_tier", "web_search_options", "metadata"}
    assert (body["stop"], body["user"]) == (["END"], "u1")
    # Server-side tools run at Anthropic only; the function tool is all that is left.
    assert [(tool["type"], tool["function"]["name"]) for tool in body["tools"]] == [("function", "get_weather")]
    system, user, assistant, tool = body["messages"]
    assert system["role"] == "system" and "be brief" in json.dumps(system) and "mid system" in json.dumps(system)
    assert [part["type"] for part in user["content"]] == ["text", "text", "file"] and user["content"][1]["text"] == "doc text"
    assert user["content"][2]["file"]["file_data"].startswith("data:application/pdf;base64,")
    assert "thinking_blocks" not in assistant and assistant["tool_calls"][0]["function"]["name"] == "get_weather"
    assert (tool["role"], tool["tool_call_id"], tool["content"]) == ("tool", "t1", "sunny")


def test_a_complete_answer_uses_anthropics_ids_names_and_block_shapes():
    def answer(request: httpx.Request) -> httpx.Response:
        call = {"id": "c1", "type": "function", "function": {"name": "get_weather", "arguments": '{"city":"Paris"}'}}
        message = {"role": "assistant", "content": "", "reasoning_content": "think", "tool_calls": [call]}
        usage = {"prompt_tokens": 100, "completion_tokens": 7, "total_tokens": 107, "prompt_tokens_details": {"cached_tokens": 40}}
        return httpx.Response(200, json={"id": "chatcmpl-1", "model": "qwen-upstream", "choices": [{"index": 0, "finish_reason": "tool_calls", "message": message}], "usage": usage})

    gateway = Gateway(answer)

    response = gateway.call("POST", MESSAGES, headers=ANTHROPIC, json={**_HELLO, "tools": [_WEATHER], "metadata": {"user_id": "u1"}})

    message = response.json()
    assert message["id"].startswith("msg_") and message["model"] == "chat-default" and message["stop_reason"] == "tool_use"
    assert message["content"] == [{"type": "thinking", "thinking": "think", "signature": ""}, {"type": "tool_use", "id": "c1", "name": "get_weather", "input": {"city": "Paris"}}]
    assert message["usage"] == {"input_tokens": 60, "output_tokens": 7, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 40}
    assert response.headers["request-id"] == response.headers["x-request-id"]
    # Anthropic names the end user in metadata; the call log keeps it.
    assert gateway.only_record().end_user == "u1"


def test_a_streamed_call_through_the_gateway_carries_tools_and_usage():
    chunks = [_chunk({"tool_calls": [_tool(0, '{"city":"Paris"}', "get_weather")]}), _chunk({}, "tool_calls"), _USAGE]
    stream = Chunks([b"data: " + json.dumps(chunk).encode() + b"\n\n" for chunk in chunks] + [b"data: [DONE]\n\n"])
    gateway = Gateway(lambda request: httpx.Response(200, headers=_SSE, stream=stream))

    response = gateway.call("POST", MESSAGES, headers=ANTHROPIC, json={**_HELLO, "stream": True, "tools": [_WEATHER]})

    events = [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith("data:")]
    assert [(block["name"], block["input"]) for block in _blocks(events)] == [("get_weather", {"city": "Paris"})]
    assert events[-2]["usage"]["input_tokens"] == 60 and events[-2]["usage"]["cache_read_input_tokens"] == 40
    assert gateway.only_record().usage.total_tokens == 107


def test_count_tokens_answers_locally_and_checks_access():
    gateway = Gateway(_completion)

    counted = gateway.call("POST", f"{MESSAGES}/count_tokens", headers=ANTHROPIC, json={"model": "chat-default", "messages": [{"role": "user", "content": "hello there"}], "tools": [_WEATHER]})
    longer = gateway.call("POST", f"{MESSAGES}/count_tokens", headers=ANTHROPIC, json={"model": "chat-default", "messages": [{"role": "user", "content": "hello there " * 50}]})
    hidden = gateway.call("POST", f"{MESSAGES}/count_tokens", headers=ANTHROPIC, json={"model": "restricted", "messages": [{"role": "user", "content": "x"}]})
    anonymous = gateway.call("POST", f"{MESSAGES}/count_tokens", json={"model": "chat-default", "messages": [{"role": "user", "content": "x"}]})

    assert counted.status_code == 200 and set(counted.json()) == {"input_tokens"} and counted.json()["input_tokens"] > 0
    assert longer.json()["input_tokens"] > counted.json()["input_tokens"]
    assert (hidden.status_code, hidden.json()["error"]["type"]) == (403, "permission_error")
    assert (anonymous.status_code, anonymous.json()["type"]) == (401, "error")
    # Nothing is sent upstream and nothing is logged as a call.
    assert gateway.upstream_requests == [] and gateway.store.records == []


def test_models_are_listed_in_the_shape_the_caller_speaks():
    gateway = Gateway(_completion)

    page = gateway.call("GET", "/v1/models", headers=ANTHROPIC).json()
    listing = gateway.call("GET", "/v1/models", headers=AUTH).json()

    assert listing["object"] == "list" and {model["object"] for model in listing["data"]} == {"model"}
    assert [model["id"] for model in page["data"]] == [model["id"] for model in listing["data"]]
    assert all(model["type"] == "model" and model["display_name"] == model["id"] and model["created_at"].endswith("Z") for model in page["data"])
    assert (page["has_more"], page["first_id"], page["last_id"]) == (False, page["data"][0]["id"], page["data"][-1]["id"])


def test_unknown_paths_under_messages_fail_in_anthropics_envelope():
    gateway = Gateway(_completion)

    missing = gateway.call("POST", f"{MESSAGES}/batches", headers=ANTHROPIC, json={})
    wrong_method = gateway.call("GET", MESSAGES, headers=ANTHROPIC)

    assert (missing.status_code, missing.json()["type"], missing.json()["error"]["type"]) == (404, "error", "not_found_error")
    assert wrong_method.status_code == 405 and wrong_method.json()["type"] == "error"
    assert missing.json()["request_id"] == missing.headers["request-id"]


def test_a_timeout_is_reported_as_anthropics_timeout_error():
    assert anthropic.error_type(504) == "timeout_error"
    assert anthropic.error_body(429, "slow down", "req-1") == {"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}, "request_id": "req-1"}
