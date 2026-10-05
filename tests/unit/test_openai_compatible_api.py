"""HTTP contracts for the OpenAI-compatible chat route and its legacy alias."""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace
from typing import Any

import pytest

from api.db.services.dialog_service import DialogService
from api.db.services.doc_metadata_service import DocMetadataService
from api.db.services.tenant_llm_service import TenantLLMService
from api.utils.api_utils import async_token_required


def test_routes_registered_once_and_legacy_alias_deprecated(client: Any) -> None:
    from fastapi.routing import iter_route_contexts

    routes = [ctx for ctx in iter_route_contexts(client.app.routes) if ctx.path.endswith("/chat/completions") and ("/openai/" in ctx.path or "/chats_openai/" in ctx.path)]
    assert sorted(ctx.path for ctx in routes) == [
        "/api/v1/chats_openai/{chat_id}/chat/completions",
        "/api/v1/openai/{chat_id}/chat/completions",
    ]
    deprecated = {ctx.path: ctx.original_route.deprecated for ctx in routes}
    assert deprecated["/api/v1/chats_openai/{chat_id}/chat/completions"] is True
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


def test_legacy_alias_still_streams_by_default(openai_route: tuple[Any, Any, SimpleNamespace, list[dict[str, Any]]]) -> None:
    client, _, _, calls = openai_route
    response = client.post("/api/v1/chats_openai/dlg-1/chat/completions", json={"model": "model", "messages": [{"role": "user", "content": "hi"}]})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    frames = _frames(response.text)
    assert frames[-1] == "[DONE]"
    assert "".join(frame["choices"][0]["delta"].get("content", "") for frame in frames[:-1] if isinstance(frame, dict)) == "part"
    assert calls[0]["stream"] is True


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
