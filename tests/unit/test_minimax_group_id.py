"""MiniMax authentication must survive real SDK URL and header preparation."""

import asyncio
import json
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

import httpx
import pytest
import requests
from openai import AsyncOpenAI

from common.log_utils import sanitize_message
from core.llm import ChatModel, chat
from core.llm.chat_model.minimax_auth import decode_minimax_key
from core.llm.chat_model.models.minimax_chat import MiniMaxChat

GROUP = "group &中文/+?="
BASE_URL = "http://fixture.invalid/v1?GroupId=old&GroupId=stale&keep=1&keep=2&empty="


@pytest.fixture
def minimax_clients(monkeypatch: pytest.MonkeyPatch) -> list[AsyncOpenAI]:
    clients: list[AsyncOpenAI] = []
    real_client = chat.AsyncOpenAI

    def create(**kwargs: Any) -> AsyncOpenAI:
        client = real_client(**kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(chat, "AsyncOpenAI", create)
    return clients


def _check_request(url: str, headers: Any, *, group: str) -> None:
    parsed = urlsplit(url)
    assert parsed.hostname == "fixture.invalid"
    assert parsed.path == "/v1/chat/completions"
    assert parse_qs(parsed.query, keep_blank_values=True) == {"GroupId": [group], "keep": ["1", "2"], "empty": [""], "caller": ["a & b"]}
    assert headers["Authorization"] == "Bearer fixture-key"
    assert headers["X-Caller"] == "preserved"
    assert "GroupId" not in headers


def _body(stream: bool) -> dict[str, Any]:
    return {
        "id": "completion-fixture",
        "object": "chat.completion.chunk" if stream else "chat.completion",
        "created": 1,
        "model": "MiniMax-M2",
        "choices": [{"index": 0, "delta" if stream else "message": {"role": "assistant", "content": "answer"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
    }


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("key_kind", ["raw", "json", "dict"])
async def test_litellm_minimax_prepared_request(monkeypatch: pytest.MonkeyPatch, minimax_clients: list[AsyncOpenAI], stream: bool, key_kind: str) -> None:
    seen: list[httpx.Request] = []

    async def send(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        _check_request(str(request.url), request.headers, group="stale" if key_kind == "raw" else GROUP)
        payload = json.loads(request.content)
        assert payload["model"] == "MiniMax-M2"
        assert "extra_query" not in payload and "group_id" not in payload
        if stream:
            content = f"data: {json.dumps(_body(True))}\n\ndata: [DONE]\n\n"
            return httpx.Response(200, content=content, headers={"content-type": "text/event-stream"}, request=request)
        return httpx.Response(200, json=_body(False), request=request)

    transport = httpx.MockTransport(send)
    monkeypatch.setattr(httpx.AsyncClient, "_transport_for_url", lambda self, url: transport)
    credentials = {"api_key": "fixture-key", "group_id": GROUP}
    key: str | dict[str, Any] = "fixture-key" if key_kind == "raw" else json.dumps(credentials) if key_kind == "json" else credentials
    original = dict(credentials)
    model = ChatModel["MiniMax"](key, "MiniMax-M2", BASE_URL, provider="MiniMax", max_retries=0)
    options = {"extra_headers": {"X-Caller": "preserved"}, "extra_query": {"caller": "a & b"}}
    if stream:
        parts = [part async for part in model.async_chat_streamly("", [{"role": "user", "content": "hello"}], {}, **options)]
        answer = "".join(part for part in parts if isinstance(part, str))
    else:
        answer, tokens = await model.async_chat("", [{"role": "user", "content": "hello"}], {}, **options)
        assert tokens == 5
    assert answer == "answer"
    assert len(seen) == 1
    assert len(minimax_clients) == 1 and minimax_clients[0].is_closed()
    assert credentials == original and options["extra_headers"] == {"X-Caller": "preserved"}


@pytest.mark.parametrize("ending", ["transport_error", "cancel", "consumer_close"])
async def test_minimax_sdk_client_cleanup(monkeypatch: pytest.MonkeyPatch, minimax_clients: list[AsyncOpenAI], ending: str) -> None:
    async def send(request: httpx.Request) -> httpx.Response:
        if ending == "cancel":
            raise asyncio.CancelledError
        if ending == "transport_error":
            raise httpx.ConnectError("fixture transport failure", request=request)
        content = f"data: {json.dumps(_body(True))}\n\ndata: [DONE]\n\n"
        return httpx.Response(200, content=content, headers={"content-type": "text/event-stream"}, request=request)

    transport = httpx.MockTransport(send)
    monkeypatch.setattr(httpx.AsyncClient, "_transport_for_url", lambda self, url: transport)
    model = ChatModel["MiniMax"]({"api_key": "fixture-key", "group_id": GROUP}, "MiniMax-M2", BASE_URL, provider="MiniMax", max_retries=0)
    if ending == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await model.async_chat("", [{"role": "user", "content": "hello"}], {})
    elif ending == "transport_error":
        answer, _ = await model.async_chat("", [{"role": "user", "content": "hello"}], {})
        assert "**ERROR**" in answer
    else:
        stream = model.async_chat_streamly("", [{"role": "user", "content": "hello"}], {})
        assert await anext(stream) == "answer"
        await stream.aclose()
    assert len(minimax_clients) == 1 and minimax_clients[0].is_closed()


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("key_kind", ["raw", "json", "dict"])
def test_sync_minimax_prepared_request(monkeypatch: pytest.MonkeyPatch, stream: bool, key_kind: str) -> None:
    seen: list[requests.PreparedRequest] = []

    def send(session: requests.Session, request: requests.PreparedRequest, **kwargs: Any) -> requests.Response:
        seen.append(request)
        _check_request(request.url or "", request.headers, group="stale" if key_kind == "raw" else GROUP)
        response = requests.Response()
        response.status_code = 200
        body = json.dumps(_body(stream))
        response._content = (f"data: {body}\n\n" if stream else body).encode()
        return response

    monkeypatch.setattr(requests.Session, "send", send)
    credentials = {"api_key": "fixture-key", "group_id": GROUP}
    key: str | dict[str, Any] = "fixture-key" if key_kind == "raw" else json.dumps(credentials) if key_kind == "json" else credentials
    model = MiniMaxChat(key, "MiniMax-M2", BASE_URL.replace("/v1?", "/v1/chat/completions?") + "&caller=a%20%26%20b", extra_headers={"X-Caller": "preserved"})
    try:
        if stream:
            answer = "".join(part for part in model.chat_streamly("", [{"role": "user", "content": "hello"}], {}) if isinstance(part, str))
        else:
            answer, tokens = model.chat("", [{"role": "user", "content": "hello"}], {})
            assert tokens == 5
        assert answer == "answer" and len(seen) == 1
    finally:
        model.client.close()


@pytest.mark.parametrize("message", [f"https://fixture.invalid/v1?GroupId={quote(GROUP, safe='')}&keep=ok", json.dumps({"group_id": GROUP}), "GroupId=encoded%20private%2Fvalue"])
def test_minimax_group_id_is_redacted(message: str) -> None:
    rendered = sanitize_message(message)
    assert "group &" not in rendered and "encoded%20private%2Fvalue" not in rendered
    assert "\\u4e2d" not in rendered and "%E4%B8%AD" not in rendered
    assert "***" in rendered


@pytest.mark.parametrize("key", [{"api_key": []}, {"api_key": "fixture-key", "group_id": {"invalid": True}}])
def test_invalid_credential_payload_is_rejected_without_echoing_it(key: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="Invalid MiniMax credential field type") as exc:
        decode_minimax_key(key)
    assert "fixture-key" not in str(exc.value)
