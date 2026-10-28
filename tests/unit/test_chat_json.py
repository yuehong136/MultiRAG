"""Strict JSON at completion boundaries without rewriting source numerics."""

from __future__ import annotations

import json
import math
import sys
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from api.utils.chat_json import sanitize_chat_json


def reject_constant(value: str) -> Any:
    raise AssertionError(f"Non-standard JSON token: {value}")


def test_nested_numerics_preserve_finite_values_and_source() -> None:
    source = {"scores": [float("nan"), np.float32("inf"), np.float16("-inf"), (np.float32(0.25), 0, False)], "integer": np.int64(7), "boolean": np.bool_(True), "huge": 10**1000, "text": "NaN"}
    result = sanitize_chat_json(source)
    decoded = json.loads(json.dumps(result, allow_nan=False), parse_constant=reject_constant)
    assert decoded["scores"] == [None, None, None, [0.25, 0, False]]
    assert decoded["integer"] == 7 and decoded["boolean"] is True
    assert decoded["huge"] == source["huge"] and decoded["text"] == "NaN"
    assert result is not source and result["scores"] is not source["scores"]
    assert math.isnan(source["scores"][0]) and np.isinf(source["scores"][1])
    assert isinstance(source["integer"], np.int64)


def test_unsupported_objects_are_not_stringified() -> None:
    with pytest.raises(TypeError):
        json.dumps(sanitize_chat_json({"unsupported": object()}), allow_nan=False)


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("protocol", ["rest", "openai"])
def test_completion_json_is_strict_with_references(client: Any, monkeypatch: pytest.MonkeyPatch, stream: bool, protocol: str) -> None:
    from api.utils.api_utils import async_token_required

    route = sys.modules[f"api.apps.restful_apis.{'chat' if protocol == 'rest' else 'openai'}"]
    dialog = SimpleNamespace(id="chat", tenant_id="tenant-unit", llm_id="saved", tenant_llm_id=3, kb_ids=[], prompt_config={})
    source = {
        "answer": "ok",
        "reference": {
            "chunks": [
                {
                    "chunk_id": "c",
                    "document_id": "doc",
                    "similarity": np.float32("nan"),
                    "vector_similarity": np.float16("inf"),
                    "term_similarity": np.float32(0.25),
                    "positions": [[np.int64(0), np.int32(2)]],
                }
            ]
        },
        "final": True,
    }
    client.app.dependency_overrides[async_token_required] = lambda: "tenant-unit"
    monkeypatch.setattr(route.DialogService, "query", lambda *args, **kwargs: [dialog])
    if protocol == "rest":
        monkeypatch.setattr(route, "_build_default_completion_dialog", lambda *args: dialog)

    async def chat(*args: Any, **kwargs: Any) -> Any:
        yield source

    monkeypatch.setattr(route, "async_chat", chat)
    path = "/api/v1/chat/completions" if protocol == "rest" else "/api/v1/openai/chat/chat/completions"
    body = {"messages": [{"role": "user", "content": "hi"}], "stream": stream, "model": "model", "reference": True}
    response = client.post(path, json=body)
    assert response.status_code == 200
    if stream:
        frames = [json.loads(line[5:], parse_constant=reject_constant) if line[5:] != "[DONE]" else "[DONE]" for line in response.text.splitlines() if line.startswith("data:")]
        if protocol == "rest":
            assert frames[-1]["data"] is True
            reference = frames[-2]["data"]["reference"]["chunks"]
        else:
            assert frames[-1] == "[DONE]" and frames[-2]["choices"][0]["finish_reason"] == "stop"
            reference = frames[-2]["choices"][0]["delta"]["reference"]
    else:
        result = json.loads(response.text, parse_constant=reject_constant)
        reference = result["data"]["reference"]["chunks"] if protocol == "rest" else result["choices"][0]["message"]["reference"]
    assert reference[0]["similarity"] is None and reference[0]["vector_similarity"] is None
    assert reference[0]["term_similarity"] == 0.25 and reference[0]["positions"] == [[0, 2]]
    assert np.isnan(source["reference"]["chunks"][0]["similarity"])
