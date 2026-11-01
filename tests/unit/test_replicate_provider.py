"""Active Replicate adapters exercise the pinned SDK and actual HTTP/SSE decoding."""

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from copy import deepcopy
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pytest

from api.db.model_contract import ModelRegistration
from api.db.services import model_provider_service, provider_verification
from common import settings
from common.exceptions import ModelException
from common.token_utils import num_tokens_from_string
from core.llm import ChatModel, EmbeddingModel
from core.llm.chat import ReplicateChat
from core.llm.embedding import ReplicateEmbed
from core.llm.replicate_auth import normalize_replicate_key


class SDKHTTP:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import replicate.client

        self.clients: list[httpx.AsyncClient] = []
        self.requests: list[httpx.Request] = []
        self.handler: Callable[[httpx.Request], Awaitable[httpx.Response]] = self.success
        original = replicate.client._build_httpx_client

        async def handle(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return await self.handler(request)

        def build(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
            assert args[0] is httpx.AsyncClient
            client = original(*args, **kwargs, transport=httpx.MockTransport(handle))
            self.clients.append(client)
            return client

        monkeypatch.setattr(replicate.client, "_build_httpx_client", build)

    @staticmethod
    def prediction(output: Any = None, **kwargs: Any) -> dict[str, Any]:
        return {"id": "prediction", "model": "meta/model", "version": "version", "status": "succeeded", "output": output, **kwargs}

    async def success(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=self.prediction(["Hello", " world"]))

    def assert_closed(self) -> None:
        assert self.clients and all(client.is_closed for client in self.clients)


@pytest.fixture
def sdk_http(monkeypatch: pytest.MonkeyPatch) -> SDKHTTP:
    return SDKHTTP(monkeypatch)


@pytest.mark.parametrize("key", ["fixture", '{"api_key":"fixture"}', {"api_key": "fixture"}])
async def test_active_chat_credentials_input_and_completion(sdk_http: SDKHTTP, key: Any) -> None:
    assert ChatModel["Replicate"] is ReplicateChat
    model = ChatModel["Replicate"](key, "meta/model", base_url="https://replicate.test", timeout=1)
    history = [{"role": "user", "content": "Hi"}]
    config = {"max_tokens": 16, "temperature": 0.1}
    before = deepcopy((history, config))
    assert await model.async_chat("Be concise", history, config) == ("Hello world", num_tokens_from_string("Hello world"))
    assert (history, config) == before
    assert len(sdk_http.requests) == 1
    request = sdk_http.requests[0]
    assert request.url.host == "replicate.test"
    assert request.headers["authorization"] == "Bearer fixture"
    assert json.loads(request.content)["input"] == {"system_prompt": "Be concise", "prompt": "user:Hi", "temperature": 0.1}
    sdk_http.assert_closed()


@pytest.mark.parametrize("key", [{}, {"api_key": None}, {"api_key": {}}, '{"api_key":null}', "", " secret ", "secret\n"])
def test_invalid_credentials_fail_without_echo_or_network(key: Any, sdk_http: SDKHTTP) -> None:
    with pytest.raises(ValueError, match=r"^Invalid Replicate credential$"):
        normalize_replicate_key(key)
    assert not sdk_http.requests and not sdk_http.clients


def test_custom_tool_flag_cannot_select_the_inherited_openai_tool_client(sdk_http: SDKHTTP) -> None:
    model = ChatModel["Replicate"]("fixture", "meta/model")
    with pytest.raises(ValueError, match="Replicate tool calling is unavailable"):
        model.bind_tools(tools=[{"type": "function"}])
    assert model.is_tools is False
    assert not sdk_http.requests and not sdk_http.clients


class PausedSSE(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b"event: output\ndata: first\nid: 1\n\n"
        await self.release.wait()
        yield b"event: output\ndata:  second\nid: 2\n\n"
        yield b"event: done\ndata: {}\nid: 3\n\n"

    async def aclose(self) -> None:
        self.closed = True


async def stream_response(sdk_http: SDKHTTP, body: PausedSSE) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            assert json.loads(request.content)["stream"] is True
            return httpx.Response(201, json=sdk_http.prediction(urls={"stream": "https://replicate.test/stream"}))
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=body)

    sdk_http.handler = handler


async def test_stream_yields_before_completion_and_counts_the_entire_answer(sdk_http: SDKHTTP) -> None:
    body = PausedSSE()
    await stream_response(sdk_http, body)
    model = ReplicateChat('{"api_key":"fixture"}', "meta/model", base_url="https://replicate.test", timeout=1)
    history = [{"role": "user", "content": "Hi"}]
    before = deepcopy(history)
    stream = model.async_chat_streamly("system", history, {"max_tokens": 16})
    try:
        assert await asyncio.wait_for(anext(stream), 0.5) == "first"
        assert not body.release.is_set() and not body.closed
        body.release.set()
        rest = [chunk async for chunk in stream]
        assert rest == [" second", num_tokens_from_string("first second")]
    finally:
        await stream.aclose()
    assert history == before and body.closed
    sdk_http.assert_closed()


@pytest.mark.parametrize("mode", ["early-close", "cancel", "timeout"])
async def test_stream_closes_http_response_on_every_exit(sdk_http: SDKHTTP, mode: str) -> None:
    body = PausedSSE()
    await stream_response(sdk_http, body)
    model = ReplicateChat("fixture", "meta/model", timeout=0.03 if mode == "timeout" else 1)
    stream = model.async_chat_streamly("", [], {})
    assert await anext(stream) == "first"
    if mode == "cancel":
        task = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    elif mode == "timeout":
        assert await anext(stream) == "**ERROR**: Replicate chat request failed"
    await stream.aclose()
    assert body.closed
    sdk_http.assert_closed()


async def test_sync_stream_bridge_works_inside_a_running_loop(sdk_http: SDKHTTP) -> None:
    body = PausedSSE()
    body.release.set()
    await stream_response(sdk_http, body)
    model = ReplicateChat("fixture", "meta/model")
    assert list(model.chat_streamly("", [], {})) == ["first", " second", num_tokens_from_string("first second")]
    assert body.closed
    sdk_http.assert_closed()


async def test_sync_stream_bridge_keeps_the_deadline_after_the_first_chunk(sdk_http: SDKHTTP) -> None:
    class SlowSSE(PausedSSE):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b"event: output\ndata: first\nid: 1\n\n"
            await asyncio.sleep(0.2)
            yield b"event: output\ndata: late\nid: 2\n\n"

    body = SlowSSE()
    await stream_response(sdk_http, body)
    model = ReplicateChat("fixture", "meta/model", timeout=0.05)
    assert list(model.chat_streamly("", [], {})) == ["first", "**ERROR**: Replicate chat request failed", num_tokens_from_string("first")]
    assert body.closed
    sdk_http.assert_closed()


async def test_provider_errors_never_echo_credentials_or_private_responses(sdk_http: SDKHTTP) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "private-key private-prompt"})

    sdk_http.handler = handler
    model = ReplicateChat("private-key", "meta/model")
    assert await model.async_chat("", [], {}) == ("**ERROR**: Replicate chat request failed", 0)
    assert [item async for item in model.async_chat_streamly("", [], {})] == ["**ERROR**: Replicate chat request failed", 0]
    with pytest.raises(ModelException, match=r"^Replicate embedding request failed$"):
        ReplicateEmbed("private-key", "meta/embedding").encode(["private-prompt"])
    sdk_http.assert_closed()


@pytest.mark.parametrize("key", ["fixture", '{"api_key":"fixture"}', {"api_key": "fixture"}])
def test_embedding_uses_real_prediction_api_for_documents_and_queries(sdk_http: SDKHTTP, key: Any) -> None:
    assert EmbeddingModel["Replicate"] is ReplicateEmbed

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer fixture"
        batch = json.loads(request.content)["input"]["texts"]
        return httpx.Response(201, json=sdk_http.prediction([[float(text), 1.0] for text in batch]))

    sdk_http.handler = handler
    model = EmbeddingModel["Replicate"](key, "meta/embedding", base_url="https://replicate.test")
    texts = [str(index) for index in range(17)]
    values, tokens = model.encode(texts)
    assert values.shape == (17, 2)
    np.testing.assert_array_equal(values[:, 0], np.arange(17))
    assert tokens == sum(num_tokens_from_string(text) for text in texts)
    query, _ = model.encode_queries("3")
    np.testing.assert_array_equal(query, [3.0, 1.0])
    assert [len(json.loads(request.content)["input"]["texts"]) for request in sdk_http.requests] == [16, 1, 1]
    sdk_http.assert_closed()


@pytest.mark.parametrize("output", [[], [[True]], [[float("nan")]], [[1], [2]], ["secret"]])
def test_embedding_rejects_malformed_results_without_partial_success(sdk_http: SDKHTTP, output: Any) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        # JSON allows NaN here only to exercise the SDK-to-vector validation boundary.
        return httpx.Response(201, content=json.dumps(sdk_http.prediction(output)), headers={"content-type": "application/json"})

    sdk_http.handler = handler
    with pytest.raises(ModelException, match="Invalid embedding provider response"):
        ReplicateEmbed("fixture", "meta/embedding").encode(["Hi"])
    sdk_http.assert_closed()


def test_embedding_deadline_closes_the_request(sdk_http: SDKHTTP) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    sdk_http.handler = handler
    with pytest.raises(ModelException, match=r"^Replicate embedding request failed$") as error:
        ReplicateEmbed("fixture", "meta/embedding", timeout=0.01).encode(["Hi"])
    assert error.value.retryable is True
    sdk_http.assert_closed()


async def test_catalog_identity_and_unsupported_rerank_are_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    root = Path(__file__).resolve().parents[2]
    factories = json.loads((root / "configs/llm_factories.json").read_text())["factory_llm_infos"]
    models = json.loads((root / "configs/models/replicate.json").read_text())["models"]
    monkeypatch.setattr(settings, "FACTORY_LLM_INFOS", factories)
    catalog = model_provider_service.catalog("Replicate")
    assert {(model.model_name, tuple(model.model_types)) for model in catalog} == {(model["name"], tuple(model["model_types"])) for model in models}
    assert len(catalog) == 6 and {capability for model in catalog for capability in model.model_types} == {"chat", "embedding"}
    declaration = ModelRegistration(model_name="unsupported", model_types=["rerank"], max_tokens=0)
    with pytest.raises(ValueError, match=r"^Provider connection verification failed$"):
        await provider_verification.verify_model("Replicate", "private-key", "", declaration)
