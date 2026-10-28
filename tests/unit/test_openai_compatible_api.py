"""HTTP contracts for the OpenAI-compatible chat route and retired legacy alias."""

from __future__ import annotations

import json
import sys
from collections.abc import AsyncIterator, Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from api.db.services.dialog_service import DialogService
from api.db.services.doc_metadata_service import DocMetadataService
from api.db.services.tenant_llm_service import TenantLLMService
from api.utils.api_utils import async_token_required


def test_routes_registered_once_and_legacy_alias_removed(client: Any) -> None:
    from fastapi.routing import iter_route_contexts

    routes = [ctx for ctx in iter_route_contexts(client.app.routes) if ctx.path.endswith("/chat/completions") and ("/openai/" in ctx.path or "/chats_openai/" in ctx.path)]
    assert sorted(ctx.path for ctx in routes) == [
        "/api/v1/openai/{chat_id}/chat/completions",
    ]
    deprecated = {ctx.path: ctx.original_route.deprecated for ctx in routes}
    assert "/api/v1/chats_openai/{chat_id}/chat/completions" not in client.app.openapi()["paths"]
    assert deprecated["/api/v1/openai/{chat_id}/chat/completions"] is not True


@pytest.fixture
def openai_route(client: Any, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Any, SimpleNamespace, list[dict[str, Any]]]:
    route = sys.modules["api.apps.restful_apis.openai"]
    dialog = SimpleNamespace(id="dlg-1", tenant_id="tenant-unit", llm_id="configured-model", tenant_llm_id=1, kb_ids=["kb-1"], prompt_config={})
    calls: list[dict[str, Any]] = []
    client.app.dependency_overrides[async_token_required] = lambda: "tenant-unit"
    monkeypatch.setattr(DialogService, "query", classmethod(lambda cls, db, **kwargs: [dialog]))

    async def fake_chat(dia: Any, messages: list[dict[str, Any]], db: Any, stream: bool, **kwargs: Any) -> Any:
        calls.append({"model": dia.llm_id, "model_id": dia.tenant_llm_id, "messages": messages, "stream": stream, **kwargs})
        if stream:
            yield {"answer": "part", "final": False}
            yield {"answer": "part done", "reference": {"chunks": []}, "final": True}
        else:
            yield {"answer": "part done", "reference": {"chunks": []}}

    monkeypatch.setattr(route, "async_chat", fake_chat)
    return client, route, dialog, calls


def _frames(text: str) -> list[dict[str, Any] | str]:
    return [json.loads(frame[5:]) if frame[5:] != "[DONE]" else "[DONE]" for frame in text.strip().split("\n\n")]


def test_new_route_defaults_to_nonstream_and_keeps_placeholder_model(openai_route: tuple[Any, Any, SimpleNamespace, list[dict[str, Any]]]) -> None:
    client, _, dialog, calls = openai_route
    response = client.post("/api/v1/openai/dlg-1/chat/completions", json={"model": "model", "messages": [{"role": "user", "content": "hi"}]})
    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "chat.completion"
    assert body["model"] == "configured-model"
    assert body["choices"][0]["message"]["content"] == "part done"
    assert calls[0]["stream"] is False
    assert dialog.llm_id == "configured-model"


def test_legacy_alias_returns_404_without_model_execution(openai_route: tuple[Any, Any, SimpleNamespace, list[dict[str, Any]]]) -> None:
    client, _, _, calls = openai_route
    response = client.post("/api/v1/chats_openai/dlg-1/chat/completions", json={"model": "model", "messages": [{"role": "user", "content": "hi"}]})
    assert response.status_code == 404
    assert calls == []


@pytest.mark.parametrize("stream", [None, False, True])
def test_canonical_stream_omission_and_explicit_values(openai_route: tuple[Any, Any, SimpleNamespace, list[dict[str, Any]]], stream: bool | None) -> None:
    client, _, _, calls = openai_route
    response = client.post("/api/v1/openai/dlg-1/chat/completions", json={"model": "model", "messages": [{"role": "user", "content": "hi"}], "stream": stream})
    assert response.status_code == 200
    assert calls[0]["stream"] is (stream is True)
    assert response.headers["content-type"].startswith("text/event-stream" if stream else "application/json")


@pytest.mark.parametrize("stream", ["true", "false", 0, 1])
def test_canonical_stream_rejects_coercion(openai_route: tuple[Any, Any, SimpleNamespace, list[dict[str, Any]]], stream: Any) -> None:
    client, _, _, calls = openai_route
    assert client.post("/api/v1/openai/dlg-1/chat/completions", json={"model": "model", "messages": [{"role": "user", "content": "hi"}], "stream": stream}).status_code == 422
    assert calls == []


def test_specific_model_validated_and_dialog_not_mutated(openai_route: tuple[Any, Any, SimpleNamespace, list[dict[str, Any]]], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _, dialog, calls = openai_route
    monkeypatch.setattr(TenantLLMService, "get_api_key", classmethod(lambda cls, db, tenant, model, kind: SimpleNamespace(id=7) if model == "other-model" else None))
    payload = {"model": "other-model", "messages": [{"role": "user", "content": "hi"}], "stream": False}
    response = client.post("/api/v1/openai/dlg-1/chat/completions", json=payload)
    assert response.json()["model"] == "other-model"
    assert calls[0]["model"] == "other-model"
    assert calls[0]["model_id"] == 7
    assert dialog.llm_id == "configured-model"
    assert dialog.tenant_llm_id == 1
    payload["model"] = "unknown-model"
    denied = client.post("/api/v1/openai/dlg-1/chat/completions", json=payload).json()
    assert denied["code"] != 0
    assert "doesn't exist" in denied["message"]
    assert len(calls) == 1


@pytest.mark.parametrize("messages", [[], "bad", [{"role": "user"}], [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "x"}}]}], [{"role": "assistant", "content": "hi"}]])
def test_invalid_messages_return_business_error(openai_route: tuple[Any, Any, SimpleNamespace, list[dict[str, Any]]], messages: Any) -> None:
    client, _, _, calls = openai_route
    response = client.post("/api/v1/openai/dlg-1/chat/completions", json={"model": "model", "messages": messages})
    assert response.status_code == 200
    assert response.json()["code"] != 0
    assert not calls


@pytest.mark.parametrize("flattened", [False, True])
def test_text_parts_metadata_filter_and_reference_metadata(openai_route: tuple[Any, Any, SimpleNamespace, list[dict[str, Any]]], monkeypatch: pytest.MonkeyPatch, flattened: bool) -> None:
    client, _, _, calls = openai_route
    route = sys.modules["api.apps.restful_apis.openai"]
    seen: list[tuple[list[str], str]] = []
    monkeypatch.setattr(DocMetadataService, "get_flatted_meta_by_kbs", classmethod(lambda cls, db, kb_ids: {"author": {"bob": ["doc-1"]}}))

    def get_metadata(cls: Any, db: Any, doc_ids: list[str], kb_id: str) -> dict[str, dict[str, str]]:
        seen.append((doc_ids, kb_id))
        return {"doc-1": {"author": "bob", "secret": "hidden"}}

    monkeypatch.setattr(DocMetadataService, "get_metadata_for_documents", classmethod(get_metadata))

    async def fake_chat(dia: Any, messages: list[dict[str, Any]], db: Any, stream: bool, **kwargs: Any) -> Any:
        calls.append({"messages": messages, "stream": stream, **kwargs})
        yield {"answer": "ok", "reference": {"chunks": [{"chunk_id": "c1", "doc_id": "doc-1", "kb_id": "kb-1"}]}}

    monkeypatch.setattr(route, "async_chat", fake_chat)
    options = {
        "reference": True,
        "reference_metadata": {"include": True, "fields": ["author"]},
        "metadata_condition": {"logic": "and", "conditions": [{"name": "author", "comparison_operator": "is", "value": "bob"}]},
    }
    body = {"model": "model", "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}, {"type": "text", "text": "there"}]}]}
    if flattened:
        body.update(options)
    else:
        body["extra_body"] = options
    response = client.post("/api/v1/openai/dlg-1/chat/completions", json=body)
    assert response.json()["choices"][0]["message"]["reference"][0]["document_metadata"] == {"author": "bob"}
    assert calls[0]["messages"][-1]["content"] == "hi\nthere"
    assert calls[0]["doc_ids"] == "doc-1"
    assert seen == [(["doc-1"], "kb-1")]


def test_stream_error_has_no_success_ending(openai_route: tuple[Any, Any, SimpleNamespace, list[dict[str, Any]]], monkeypatch: pytest.MonkeyPatch) -> None:
    client, route, _, _ = openai_route

    async def fail_chat(dia: Any, messages: list[dict[str, Any]], db: Any, stream: bool, **kwargs: Any) -> Any:
        raise RuntimeError("model unavailable")
        yield {}

    monkeypatch.setattr(route, "async_chat", fail_chat)
    response = client.post("/api/v1/openai/dlg-1/chat/completions", json={"model": "model", "messages": [{"role": "user", "content": "hi"}], "stream": True})
    frames = _frames(response.text)
    assert frames[-1] == "[DONE]"
    assert frames[-2]["error"]["type"] == "server_error"
    assert not any(isinstance(frame, dict) and frame.get("choices", [{}])[0].get("finish_reason") == "stop" for frame in frames[:-1])


def test_stream_final_chunk_carries_reference_metadata_once(openai_route: tuple[Any, Any, SimpleNamespace, list[dict[str, Any]]], monkeypatch: pytest.MonkeyPatch) -> None:
    client, route, _, _ = openai_route
    monkeypatch.setattr(DocMetadataService, "get_metadata_for_documents", classmethod(lambda cls, db, doc_ids, kb_id: {"doc-1": {"author": "bob"}}))

    async def fake_chat(dia: Any, messages: list[dict[str, Any]], db: Any, stream: bool, **kwargs: Any) -> Any:
        yield {"answer": "part", "final": False}
        yield {"answer": "part done", "reference": {"chunks": [{"doc_id": "doc-1", "kb_id": "kb-1"}]}, "final": True}

    monkeypatch.setattr(route, "async_chat", fake_chat)
    response = client.post(
        "/api/v1/openai/dlg-1/chat/completions",
        json={
            "model": "model",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
            "reference": True,
            "reference_metadata": {"include": True, "fields": ["author"]},
        },
    )
    frames = _frames(response.text)
    assert frames[-1] == "[DONE]"
    chunks = [frame for frame in frames[:-1] if isinstance(frame, dict)]
    assert "".join(chunk["choices"][0]["delta"].get("content", "") for chunk in chunks) == "part"
    final = chunks[-1]["choices"][0]
    assert final["finish_reason"] == "stop"
    assert final["delta"]["final_content"] == "part done"
    assert final["delta"]["reference"][0]["document_metadata"] == {"author": "bob"}


def test_metadata_filter_no_match_excludes_all_documents(openai_route: tuple[Any, Any, SimpleNamespace, list[dict[str, Any]]], monkeypatch: pytest.MonkeyPatch) -> None:
    client, _, _, calls = openai_route
    monkeypatch.setattr(DocMetadataService, "get_flatted_meta_by_kbs", classmethod(lambda cls, db, kb_ids: {}))
    response = client.post(
        "/api/v1/openai/dlg-1/chat/completions",
        json={
            "model": "model",
            "messages": [{"role": "user", "content": "hi"}],
            "metadata_condition": {"conditions": [{"name": "author", "comparison_operator": "is", "value": "nobody"}]},
        },
    )
    assert response.json()["object"] == "chat.completion"
    assert calls[0]["doc_ids"] == "-999"


def test_auth_and_ownership_enforced(client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    request = {"model": "model", "messages": [{"role": "user", "content": "hi"}]}
    unauthorized = client.post("/api/v1/openai/dlg-1/chat/completions", json=request)
    assert unauthorized.status_code != 200 or unauthorized.json().get("code") != 0
    client.app.dependency_overrides[async_token_required] = lambda: "tenant-unit"
    monkeypatch.setattr(DialogService, "query", classmethod(lambda cls, db, **kwargs: []))
    denied = client.post("/api/v1/openai/dlg-1/chat/completions", json=request)
    assert denied.status_code == 200
    assert denied.json()["code"] == 102
    assert "don't own" in denied.json()["message"]


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("nested", [False, True])
def test_session_hint_is_stateless(openai_route: tuple[Any, Any, SimpleNamespace, list[dict[str, Any]]], monkeypatch: pytest.MonkeyPatch, stream: bool, nested: bool) -> None:
    from api.db.services.conversation_service import ConversationService

    client, _, _, calls = openai_route
    writes: list[str] = []

    def forbidden(*args: Any, **kwargs: Any) -> None:
        writes.append("conversation access")
        raise AssertionError("OpenAI completions must not access stored sessions")

    for method in ("get_by_id", "save", "update_by_id", "query"):
        monkeypatch.setattr(ConversationService, method, forbidden)
    body = {"model": "model", "messages": [{"role": "user", "content": "stateless"}], "stream": stream}
    body.update({"extra_body": {"session_id": "existing-session"}} if nested else {"session_id": "existing-session"})
    response = client.post("/api/v1/openai/dlg-1/chat/completions", json=body)
    assert response.status_code == 200
    assert calls and not writes
    assert "session_id" not in calls[0]
    assert '"session_id"' not in response.text


@pytest.mark.parametrize(
    "content",
    [None, 42, {}, [None], [{"type": "text"}], [{"type": "text", "text": None}], [{"type": "text", "text": 0}], [{"type": "text", "text": "keep"}, {"type": "image_url", "image_url": {"url": "x"}}]],
)
def test_text_arrays_reject_malformed_and_unsupported_parts_before_lookup(openai_route: tuple[Any, Any, SimpleNamespace, list[dict[str, Any]]], monkeypatch: pytest.MonkeyPatch, content: Any) -> None:
    client, route, _, calls = openai_route

    def unexpected_lookup(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Invalid input reached SQL lookup")

    monkeypatch.setattr(route.DialogService, "query", unexpected_lookup)
    response = client.post("/api/v1/openai/dlg-1/chat/completions", json={"model": "model", "messages": [{"role": "user", "content": content}]})
    assert response.status_code == 200 and response.json()["code"] != 0
    assert calls == []


@pytest.fixture
def openai_http_url(openai_route: tuple[Any, Any, SimpleNamespace, list[dict[str, Any]]], request: pytest.FixtureRequest) -> Iterator[str]:
    """Exercise the registered route through a reserved, real TCP listener."""
    import secrets
    import socket
    import threading
    import time

    import uvicorn

    from tests.support.resource_journal import ResourceJournal
    from tests.support.uvicorn_runtime import run_server

    client, _, _, _ = openai_route
    journal = ResourceJournal(request.node.nodeid)
    thread_name = f"openai-sdk-{journal.run_id}"
    for _attempt in range(32):
        port = 20000 + secrets.randbelow(40000)
        journal.event("reserve-listener-thread", host="127.0.0.1", port=port, thread=thread_name)
        listener = socket.socket()
        try:
            listener.bind(("127.0.0.1", port))
            break
        except OSError:
            listener.close()
            journal.event("listener-unavailable", port=port)
    else:
        raise RuntimeError("No registered OpenAI test listener available")
    server = uvicorn.Server(uvicorn.Config(client.app, lifespan="off", log_level="error", access_log=False, loop="asyncio"))
    thread = threading.Thread(target=run_server, args=(server,), kwargs={"sockets": [listener]}, name=thread_name, daemon=True)
    try:
        thread.start()
        deadline = time.monotonic() + 15
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        yield f"http://127.0.0.1:{port}/api/v1/openai/dlg-1"
    finally:
        server.should_exit = True
        if thread.ident is not None:
            thread.join(timeout=15)
        listener.close()
        assert not thread.is_alive()
        with socket.socket() as probe:
            assert probe.connect_ex(("127.0.0.1", port)) != 0
        journal.event("listener-absent", host="127.0.0.1", port=port, thread_alive=False)


@pytest.mark.parametrize("shape", ["incremental", "cumulative", "final-only"])
@pytest.mark.parametrize("reference", [False, True])
def test_sdk_stream_preserves_single_answer_and_terminal_contract(
    openai_route: tuple[Any, Any, SimpleNamespace, list[dict[str, Any]]],
    openai_http_url: str,
    monkeypatch: pytest.MonkeyPatch,
    shape: str,
    reference: bool,
) -> None:
    import httpx
    from openai import OpenAI

    from api.db.services.dialog_service import _stream_with_think_delta

    _, route, _, _ = openai_route
    answer = "你好 🌍"
    decorated = answer + " ##0$$"
    model_chunks = ("<thi", "nk>推理</thi", "nk>你好", " 🌍") if shape == "incremental" else ("<thi", "<think>推理</think>你", "<think>推理</think>你好 🌍")

    async def chat(*args: Any, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        async def chunks() -> AsyncIterator[str]:
            for chunk in model_chunks:
                yield chunk

        if shape != "final-only":
            async for kind, value, state in _stream_with_think_delta(chunks(), min_tokens=0):
                if kind == "marker":
                    yield {"start_to_think" if state.in_think else "end_to_think": True}
                else:
                    yield {"answer": value}
        yield {
            "final": True,
            "answer": decorated if reference and shape != "final-only" else answer,
            "reference": {"chunks": [{"doc_id": "doc", "similarity": float("nan"), "vector_similarity": float("inf"), "term_similarity": 0.25}]},
        }

    monkeypatch.setattr(route, "async_chat", chat)
    messages: Any = [{"role": "user", "content": [{"type": "text", "text": "hi"}, {"type": "text", "text": "there"}]}]
    with OpenAI(base_url=openai_http_url, api_key="controlled") as sdk:
        with sdk.chat.completions.create(model="model", messages=messages, stream=True, extra_body={"reference": reference}) as stream:
            events = list(stream)
    assert "".join(event.choices[0].delta.content or "" for event in events) == answer
    assert "".join(getattr(event.choices[0].delta, "reasoning_content", "") or "" for event in events) == ("" if shape == "final-only" else "推理")
    assert [event.choices[0].finish_reason for event in events if event.choices[0].finish_reason] == ["stop"]
    final = events[-1]
    assert final.model == "configured-model"
    if reference:
        assert final.choices[0].delta.final_content == (answer if shape == "final-only" else decorated)
        assert final.choices[0].delta.reference[0]["similarity"] is None
        assert final.choices[0].delta.reference[0]["vector_similarity"] is None
        assert final.choices[0].delta.reference[0]["term_similarity"] == 0.25
    else:
        assert final.choices[0].delta.model_dump(exclude_unset=True) == {}
    raw = httpx.post(openai_http_url + "/chat/completions", json={"model": "model", "messages": messages, "stream": True, "reference": reference})
    assert raw.status_code == 200
    assert raw.text.count("data:[DONE]\n\n") == 1

    def reject_constant(value: str) -> Any:
        raise AssertionError(f"Non-standard JSON constant: {value}")

    frames = [json.loads(line[5:], parse_constant=reject_constant) for line in raw.text.splitlines() if line.startswith("data:") and line != "data:[DONE]"]
    assert sum(frame["choices"][0]["finish_reason"] == "stop" for frame in frames) == 1
    tokens = sum(route.num_tokens_from_string(frame["choices"][0]["delta"].get(field, "")) for frame in frames for field in ("content", "reasoning_content"))
    prompt_tokens = route.num_tokens_from_string("hi\nthere")
    assert frames[-1]["usage"] == {"prompt_tokens": prompt_tokens, "completion_tokens": tokens, "total_tokens": prompt_tokens + tokens}
    assert final.usage.model_dump(exclude_unset=True) == frames[-1]["usage"]
    assert all(frame["usage"] is None for frame in frames[:-1])


@pytest.mark.parametrize("failure", ["before-content", "after-content", "empty"])
def test_sdk_stream_errors_never_emit_success(
    openai_route: tuple[Any, Any, SimpleNamespace, list[dict[str, Any]]],
    openai_http_url: str,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    import httpx
    from openai import APIError, OpenAI

    _, route, _, _ = openai_route

    async def chat(*args: Any, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        if failure == "empty":
            return
        if failure == "after-content":
            yield {"answer": "partial"}
        raise RuntimeError("private-provider-diagnostic")

    monkeypatch.setattr(route, "async_chat", chat)
    messages: Any = [{"role": "user", "content": "hi"}]
    with OpenAI(base_url=openai_http_url, api_key="controlled") as sdk:
        with sdk.chat.completions.create(model="model", messages=messages, stream=True) as stream:
            with pytest.raises(APIError, match="Chat completion failed"):
                list(stream)
    raw = httpx.post(openai_http_url + "/chat/completions", json={"model": "model", "messages": messages, "stream": True})
    assert "private-provider-diagnostic" not in raw.text
    frames = _frames(raw.text)
    assert frames.count("[DONE]") == 1
    assert frames[-2] == {"error": {"message": "Chat completion failed.", "type": "server_error"}}
    assert all(frame.get("choices", [{}])[0].get("finish_reason") is None for frame in frames[:-1])
