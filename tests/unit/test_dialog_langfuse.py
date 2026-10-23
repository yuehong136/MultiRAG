"""Real chat orchestration and HTTP/SSE with a recording v4 observation.

Counts remain local text estimates, including the decorated answer and thinking;
these tests do not contact a model provider or hosted Langfuse.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncIterator, Iterator
from copy import deepcopy
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from langfuse import LangfuseGeneration
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from api.db.services import dialog_service
from api.db.services.llm_service import LLMBundle
from common.token_utils import num_tokens_from_string
from core.prompts.generator import message_fit_in

_KEYS = SimpleNamespace(public_key="pk-test-tracing", secret_key="sk-test-tracing", host="http://langfuse.invalid")
_MODEL = {"llm_name": "controlled-model", "max_tokens": 256}
_ANSWER = "<think>Reasoning about the evidence.</think>A model reply."
_DECORATED = "<think>Reasoning about the evidence.</think>A decorated reply ##0$$."
_MESSAGES = [
    {"role": "user", "content": "old dropped question " * 300},
    {"role": "assistant", "content": "old dropped answer " * 300},
    {"role": "user", "content": "What is the evidence about?"},
]


@dataclass
class _Probe:
    dialog: SimpleNamespace
    failure: str = ""
    answer: str = _ANSWER
    keys: SimpleNamespace | None = field(default_factory=lambda: _KEYS)
    starts: list[dict[str, Any]] = field(default_factory=list)
    updates: list[dict[str, Any]] = field(default_factory=list)
    model_calls: list[dict[str, Any]] = field(default_factory=list)
    ends: int = 0


class _Observation(LangfuseGeneration):
    def __init__(self, probe: _Probe) -> None:
        self.probe = probe

    def update(self, **kwargs: Any) -> _Observation:
        self.probe.updates.append(kwargs)
        if self.probe.failure == "update":
            raise RuntimeError(_KEYS.secret_key)
        return self

    def end(self, **kwargs: Any) -> None:
        assert kwargs == {}
        self.probe.ends += 1
        if self.probe.failure == "end":
            raise RuntimeError(_KEYS.secret_key)

    def __bool__(self) -> bool:
        # A valid observation is identified by non-nullness, never truthiness.
        return False


class _ChatBundle(LLMBundle):
    def __init__(self, probe: _Probe) -> None:
        self.probe = probe
        self.db = None

    def chat(self, system: str, history: list[dict[str, Any]], gen_conf: dict[str, Any], **kwargs: Any) -> str:
        self.probe.model_calls.append({"system": system, "history": deepcopy(history), "gen_conf": dict(gen_conf)})
        if self.probe.failure == "model":
            raise ValueError("controlled model failure")
        return self.probe.answer

    def chat_streamly(self, system: str, history: list[dict[str, Any]], gen_conf: dict[str, Any], **kwargs: Any) -> Iterator[str]:
        answer = self.chat(system, history, gen_conf, **kwargs)
        if self.probe.failure != "empty-stream":
            yield answer

    async def async_chat(self, system: str, history: list[dict[str, Any]], gen_conf: dict[str, Any], **kwargs: Any) -> str:
        return self.chat(system, history, gen_conf, **kwargs)

    async def async_chat_streamly_delta(self, system: str, history: list[dict[str, Any]], gen_conf: dict[str, Any], **kwargs: Any) -> AsyncIterator[str]:
        if self.probe.failure == "cancel":
            raise asyncio.CancelledError
        answer = self.chat(system, history, gen_conf, **kwargs)
        if answer:
            yield answer


@pytest.fixture
def tracing_chat(monkeypatch: pytest.MonkeyPatch) -> _Probe:
    probe = _Probe(
        dialog=SimpleNamespace(
            tenant_id="tenant-unit",
            kb_ids=["kb-1"],
            llm_id="controlled-model",
            llm_setting={"max_tokens": 64},
            prompt_config={"system": "Use the evidence.{knowledge}", "parameters": [{"key": "knowledge", "optional": False}]},
            meta_data_filter=None,
            top_n=6,
            similarity_threshold=0.2,
            vector_similarity_weight=0.3,
            search_mode=None,
        )
    )
    kb = SimpleNamespace(id="kb-1", name="controlled-kb", tenant_id="tenant-unit")
    bundle = _ChatBundle(probe)
    chunks = [{"doc_id": "doc-1", "content_ltks": "Evidence text.", "vector": [1.0]}]

    async def retrieval(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"total": 1, "chunks": deepcopy(chunks), "doc_aggs": [{"doc_id": "doc-1"}]}

    def insert_citations(answer: str, *args: Any, **kwargs: Any) -> tuple[str, set[int]]:
        return "A decorated reply ##0$$.", {0}

    class RecordingLangfuse:
        # Deliberately has no auth_check or start_generation.
        def __init__(self, public_key: str, secret_key: str, host: str) -> None:
            assert (public_key, secret_key, host) == (_KEYS.public_key, _KEYS.secret_key, _KEYS.host)
            if probe.failure == "init":
                raise RuntimeError(secret_key)

        def create_trace_id(self) -> str:
            if probe.failure == "trace_id":
                raise RuntimeError(_KEYS.secret_key)
            return "trace-id"

        def start_observation(self, **kwargs: Any) -> _Observation:
            probe.starts.append(deepcopy(kwargs))
            if probe.failure == "start":
                raise RuntimeError(_KEYS.secret_key)
            return _Observation(probe)

    monkeypatch.setattr(dialog_service, "Langfuse", RecordingLangfuse)
    monkeypatch.setattr(dialog_service, "resolve_model_type", lambda *_args: ["chat"])
    monkeypatch.setattr(dialog_service, "_resolve_dialog_primary_model_config", lambda *_args: _MODEL)
    monkeypatch.setattr(dialog_service.TenantLangfuseService, "filter_by_tenant", lambda *_args, **_kwargs: probe.keys)
    monkeypatch.setattr(dialog_service, "get_models", lambda *_args: ([kb], SimpleNamespace(db=None), None, bundle, None))
    monkeypatch.setattr(dialog_service.KnowledgebaseService, "get_field_map", lambda *_args: {})
    monkeypatch.setattr(dialog_service, "label_question", lambda *_args: {})
    monkeypatch.setattr(dialog_service, "kb_prompt", lambda *_args: ["Evidence text."])
    monkeypatch.setattr(
        dialog_service.settings,
        "retriever",
        SimpleNamespace(retrieval=retrieval, retrieval_by_children=lambda chunks, *_args: chunks, insert_citations=insert_citations),
    )
    return probe


def _assert_usage(probe: _Probe, answer: str = _DECORATED) -> None:
    assert len(probe.starts) == len(probe.updates) == probe.ends == 1
    start = probe.starts[0]
    assert set(start) == {"as_type", "trace_context", "name", "model", "input"}
    assert start["as_type"] == "generation"
    assert start["trace_context"] == {"trace_id": "trace-id"}
    assert start["name"] == "chat" and start["model"] == _MODEL["llm_name"]
    fitted_input, fitted = message_fit_in([{"role": "system", "content": "Use the evidence.\n------\nEvidence text."}, *_MESSAGES], int(_MODEL["max_tokens"] * 0.95))
    assert len(fitted) == 2  # The original oversized history must not be counted.
    assert start["input"] == {"prompt": fitted[0]["content"], "prompt4citation": dialog_service.citation_prompt(), "messages": fitted}
    assert probe.model_calls[0]["system"] == fitted[0]["content"] + dialog_service.citation_prompt()
    assert probe.model_calls[0]["history"] == fitted[1:]
    output = num_tokens_from_string(answer)
    assert probe.updates[0]["usage_details"] == {"input": fitted_input, "output": output, "total": fitted_input + output}
    assert set(probe.updates[0]) == {"output", "usage_details"}
    assert set(probe.updates[0]["output"]) == {"time_elapsed:", "created_at"}
    assert "Generated tokens(approximately)" in probe.updates[0]["output"]["time_elapsed:"]


@pytest.mark.parametrize("stream", [False, True])
async def test_async_chat_records_fitted_input_and_decorated_output(tracing_chat: _Probe, async_db: AsyncSession, stream: bool) -> None:
    events = [event async for event in dialog_service.async_chat(tracing_chat.dialog, deepcopy(_MESSAGES), async_db, stream=stream)]
    assert events[-1]["answer"] == _DECORATED
    assert events[-1]["reference"]["chunks"] == [{"doc_id": "doc-1", "content_ltks": "Evidence text."}]
    if stream:
        assert events[-1]["final"] is True
        assert sum(event.get("final", False) for event in events) == 1
    _assert_usage(tracing_chat)


@pytest.mark.parametrize("stream", [False, True])
def test_sync_chat_records_same_usage(tracing_chat: _Probe, db: Session, stream: bool) -> None:
    events = list(dialog_service.chat(tracing_chat.dialog, deepcopy(_MESSAGES), db, stream=stream))
    assert events[-1]["answer"] == _DECORATED
    _assert_usage(tracing_chat)


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("failure", ["init", "trace_id", "start", "update", "end", "disabled"])
async def test_tracing_failures_preserve_chat_without_exposing_keys(tracing_chat: _Probe, async_db: AsyncSession, caplog: pytest.LogCaptureFixture, stream: bool, failure: str) -> None:
    tracing_chat.failure = failure
    if failure == "disabled":
        tracing_chat.keys = None
    events = [event async for event in dialog_service.async_chat(tracing_chat.dialog, deepcopy(_MESSAGES), async_db, stream=stream)]
    assert events[-1]["answer"] == _DECORATED
    assert tracing_chat.ends == (1 if failure in {"update", "end"} else 0)
    assert len(tracing_chat.updates) == (1 if failure in {"update", "end"} else 0)
    assert _KEYS.public_key not in str(events) + caplog.text
    assert _KEYS.secret_key not in str(events) + caplog.text


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("failure", ["init", "trace_id", "start", "update", "end", "disabled"])
def test_sync_tracing_failures_preserve_chat(tracing_chat: _Probe, db: Session, caplog: pytest.LogCaptureFixture, stream: bool, failure: str) -> None:
    tracing_chat.failure = failure
    if failure == "disabled":
        tracing_chat.keys = None
    events = list(dialog_service.chat(tracing_chat.dialog, deepcopy(_MESSAGES), db, stream=stream))
    assert events[-1]["answer"] == _DECORATED
    assert tracing_chat.ends == (1 if failure in {"update", "end"} else 0)
    assert len(tracing_chat.updates) == (1 if failure in {"update", "end"} else 0)
    assert _KEYS.public_key not in str(events) + caplog.text
    assert _KEYS.secret_key not in str(events) + caplog.text


@pytest.mark.parametrize("stream", [False, True])
async def test_model_failure_ends_observation_without_reporting_zero_usage(tracing_chat: _Probe, async_db: AsyncSession, stream: bool) -> None:
    tracing_chat.failure = "model"
    with pytest.raises(ValueError, match="controlled model failure"):
        _ = [event async for event in dialog_service.async_chat(tracing_chat.dialog, deepcopy(_MESSAGES), async_db, stream=stream)]
    assert tracing_chat.ends == 1 and tracing_chat.updates == []


async def test_cancellation_ends_observation_without_reporting_zero_usage(tracing_chat: _Probe, async_db: AsyncSession) -> None:
    tracing_chat.failure = "cancel"
    with pytest.raises(asyncio.CancelledError):
        _ = [event async for event in dialog_service.async_chat(tracing_chat.dialog, deepcopy(_MESSAGES), async_db)]
    assert tracing_chat.ends == 1 and tracing_chat.updates == []


async def test_closing_partial_stream_ends_observation_without_usage(tracing_chat: _Probe, async_db: AsyncSession) -> None:
    stream = dialog_service.async_chat(tracing_chat.dialog, deepcopy(_MESSAGES), async_db)
    event = await anext(stream)
    assert event["final"] is False and tracing_chat.ends == 0
    await stream.aclose()
    await stream.aclose()
    assert tracing_chat.ends == 1 and tracing_chat.updates == []


def test_closing_sync_partial_stream_ends_observation_without_usage(tracing_chat: _Probe, db: Session) -> None:
    stream = dialog_service.chat(tracing_chat.dialog, deepcopy(_MESSAGES), db)
    next(stream)
    assert tracing_chat.ends == 0
    stream.close()
    stream.close()
    assert tracing_chat.ends == 1 and tracing_chat.updates == []


async def test_empty_stream_ends_without_fabricated_usage(tracing_chat: _Probe, async_db: AsyncSession) -> None:
    tracing_chat.answer = ""
    events = [event async for event in dialog_service.async_chat(tracing_chat.dialog, deepcopy(_MESSAGES), async_db)]
    assert events == []
    assert tracing_chat.ends == 1 and tracing_chat.updates == []


def test_sync_empty_stream_preserves_response_without_fabricated_usage(tracing_chat: _Probe, db: Session) -> None:
    tracing_chat.failure = "empty-stream"
    tracing_chat.dialog.prompt_config["quote"] = False
    events = list(dialog_service.chat(tracing_chat.dialog, deepcopy(_MESSAGES), db))
    assert len(events) == 1 and events[0]["answer"] == ""
    assert tracing_chat.ends == 1 and tracing_chat.updates == []


def test_sync_explicit_empty_chunk_retains_legal_zero_usage(tracing_chat: _Probe, db: Session) -> None:
    tracing_chat.answer = ""
    tracing_chat.dialog.prompt_config["quote"] = False
    events = list(dialog_service.chat(tracing_chat.dialog, deepcopy(_MESSAGES), db))
    assert len(events) == 1 and events[0]["answer"] == ""
    assert tracing_chat.ends == 1 and len(tracing_chat.updates) == 1
    usage = tracing_chat.updates[0]["usage_details"]
    assert usage["output"] == 0 and usage["input"] > 0 and usage["total"] == usage["input"]


async def test_successful_empty_answer_retains_legal_zero_output_usage(tracing_chat: _Probe, async_db: AsyncSession) -> None:
    tracing_chat.answer = ""
    tracing_chat.dialog.prompt_config["quote"] = False
    events = [event async for event in dialog_service.async_chat(tracing_chat.dialog, deepcopy(_MESSAGES), async_db, stream=False)]
    assert events[-1]["answer"] == ""
    assert tracing_chat.ends == 1 and len(tracing_chat.updates) == 1
    usage = tracing_chat.updates[0]["usage_details"]
    assert usage["output"] == 0 and usage["input"] > 0 and usage["total"] == usage["input"]


@pytest.mark.parametrize("counter", ["input", "output"])
async def test_unavailable_estimate_is_not_reported_as_zero(tracing_chat: _Probe, async_db: AsyncSession, monkeypatch: pytest.MonkeyPatch, counter: str) -> None:
    if counter == "input":
        monkeypatch.setattr(dialog_service, "message_fit_in", lambda messages, budget: (0, message_fit_in(messages, budget)[1]))
    else:
        monkeypatch.setattr(dialog_service, "num_tokens_from_string", lambda _text: 0)
    events = [event async for event in dialog_service.async_chat(tracing_chat.dialog, deepcopy(_MESSAGES), async_db, stream=False)]
    assert events[-1]["answer"] == _DECORATED
    assert tracing_chat.ends == 1 and len(tracing_chat.updates) == 1
    assert tracing_chat.updates[0]["usage_details"] is None


@pytest.mark.parametrize("stream", [False, True])
def test_http_json_and_sse_execute_real_chat(client: TestClient, monkeypatch: pytest.MonkeyPatch, tracing_chat: _Probe, stream: bool) -> None:
    chat_api = sys.modules["api.apps.restful_apis.chat"]
    assert chat_api.async_chat is dialog_service.async_chat
    monkeypatch.setattr(chat_api, "_build_default_completion_dialog", lambda *_args: tracing_chat.dialog)
    response = client.post("/api/v1/chat/completions", json={"messages": deepcopy(_MESSAGES), "stream": stream})
    assert response.status_code == 200, response.text
    if stream:
        assert response.headers["content-type"].startswith("text/event-stream")
        frames = [json.loads(line.removeprefix("data:")) for line in response.text.splitlines() if line.startswith("data:")]
        assert all(frame["code"] == 0 for frame in frames)
        assert frames[-1]["data"] is True
        answers = [frame["data"] for frame in frames if isinstance(frame["data"], dict)]
        assert answers[-1]["answer"] == _DECORATED and answers[-1]["final"] is True
    else:
        assert response.json()["code"] == 0
        assert response.json()["data"]["answer"] == _DECORATED
    _assert_usage(tracing_chat)
