"""Exercise tenant construction and real protocol adapters without provider credentials."""

import json
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pytest
from openai import AsyncOpenAI, OpenAI
from sqlalchemy.orm import Session

from api.db.services.tenant_llm_service import TenantLLMService
from common.token_utils import num_tokens_from_string
from core.llm import FACTORY_DEFAULT_BASE_URL, LITELLM_PROVIDER_PREFIX, SupportedLiteLLMProvider, cv, embedding, rerank, sequence2txt, tts
from core.llm import chat as chat_module

DEFAULT = "https://futurmix.ai/v1"


def test_catalog_and_litellm_metadata() -> None:
    data = json.loads((Path(__file__).parents[2] / "configs/llm_factories.json").read_text())
    entries = [f for f in data["factory_llm_infos"] if f["name"] == "FuturMix"]
    assert len(entries) == 1
    assert entries[0]["url"] == DEFAULT
    assert len(entries[0]["llm"]) == 15
    assert {m["mdl_type"] for m in entries[0]["llm"]} == {"chat", "embedding", "image2text", "rerank", "speech2text", "tts"}
    assert all("model_type" not in m for m in entries[0]["llm"])
    assert FACTORY_DEFAULT_BASE_URL[SupportedLiteLLMProvider.FuturMix] == DEFAULT
    assert LITELLM_PROVIDER_PREFIX[SupportedLiteLLMProvider.FuturMix] == "openai/"


@pytest.mark.parametrize("configured_url", [None, "", DEFAULT, "https://gateway.test/proxy/v1/"])
async def test_tenant_adapters_send_and_consume_protocol(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, db: Session, configured_url: str | None) -> None:
    calls: list[httpx.Request] = []
    base = (configured_url or DEFAULT).rstrip("/")

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.headers["authorization"] == "Bearer fixture-key"
        if request.url.path.endswith("/embeddings"):
            body = json.loads(request.content)
            return httpx.Response(200, json={"data": [{"embedding": [i, len(t)], "index": i, "object": "embedding"} for i, t in enumerate(body["input"])], "usage": {"total_tokens": 7}})
        if request.url.path.endswith("/audio/transcriptions"):
            assert b"whisper-1" in request.content
            assert b"fixture-audio" in request.content
            return httpx.Response(200, json={"text": " recognized speech "})
        assert str(request.url) == base + "/chat/completions"
        body = json.loads(request.content)
        assert body["model"] == "gpt-4o"
        return httpx.Response(
            200,
            json={
                "id": "fixture",
                "object": "chat.completion",
                "created": 1,
                "model": "gpt-4o",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": " response "}, "finish_reason": "stop"}],
                "usage": {"total_tokens": 9},
            },
        )

    transport = httpx.MockTransport(respond)

    def sync_client(**kwargs: Any) -> OpenAI:
        return OpenAI(**kwargs, http_client=httpx.Client(transport=transport))

    def async_client(**kwargs: Any) -> AsyncOpenAI:
        return AsyncOpenAI(**kwargs, http_client=httpx.AsyncClient(transport=transport))

    for module in (chat_module, cv, embedding, sequence2txt):
        monkeypatch.setattr(module, "OpenAI", sync_client)
    for module in (chat_module, cv):
        monkeypatch.setattr(module, "AsyncOpenAI", async_client)

    def model(kind: str, name: str) -> Any:
        return TenantLLMService.model_instance(db, "tenant", {"mdl_type": kind, "llm_factory": "FuturMix", "api_key": "fixture-key", "llm_name": name, "api_base": configured_url})

    chat = model("chat", "gpt-4o")
    assert await chat.async_chat("system", [{"role": "user", "content": "question"}], {}) == ("response", 9)
    vision = model("image2text", "gpt-4o")
    assert vision.describe(b"image") == ("response", 9)
    vision_body = json.loads(calls[-1].content)
    assert "image_url" in json.dumps(vision_body)
    embed = model("embedding", "text-embedding-3-small")
    vectors, tokens = embed.encode(["a", "bb"])
    np.testing.assert_array_equal(vectors, [[0, 1], [1, 2]])
    assert tokens == 7
    query, tokens = embed.encode_queries("ccc")
    np.testing.assert_array_equal(query, [0, 3])
    assert tokens == 7
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"fixture-audio")
    assert model("speech2text", "whisper-1").transcription(str(audio)) == ("recognized speech", num_tokens_from_string("recognized speech"))
    assert str(calls[-1].url) == base + "/audio/transcriptions"

    http_calls: list[tuple[str, dict[str, Any]]] = []

    class Response:
        status_code = 200

        def json(self) -> dict[str, Any]:
            return {"results": [{"index": 1, "relevance_score": 0.9}, {"index": 0, "relevance_score": 0.1}]}

        def iter_content(self) -> Any:
            return iter([b"audio", b"", b"bytes"])

    def post(url: str, **kwargs: Any) -> Response:
        assert kwargs["headers"]["Authorization"] == "Bearer fixture-key"
        http_calls.append((url, kwargs))
        return Response()

    monkeypatch.setattr(rerank.requests, "post", post)
    monkeypatch.setattr(tts.requests, "post", post)
    scores, tokens = model("rerank", "jina-reranker-v2-base-multilingual").similarity("query", ["first", "second"])
    np.testing.assert_array_equal(scores, [0.0, 1.0])
    assert tokens == sum(num_tokens_from_string(s) for s in ["first", "second"])
    assert http_calls[-1][0] == base + "/rerank"
    assert http_calls[-1][1]["json"]["documents"] == ["first", "second"]
    assert b"".join(model("tts", "tts-1").tts("hello")) == b"audiobytes"
    assert http_calls[-1][0] == base + "/audio/speech"
    assert http_calls[-1][1]["json"] == {"model": "tts-1", "voice": "alloy", "input": "hello"}
    await chat.async_client.close()
    await vision.async_client.close()
    for instance in (chat, vision, embed):
        instance.client.close()


@pytest.mark.parametrize("configured_url", [DEFAULT, DEFAULT + "/rerank", DEFAULT + "/rerank/", "https://gateway.test/prefix/v1/rerank"])
def test_rerank_endpoint_is_not_duplicated(configured_url: str) -> None:
    instance = rerank.FuturMixRerank("key", "model", configured_url)
    assert instance.base_url == (configured_url.rstrip("/") if configured_url.rstrip("/").endswith("/rerank") else configured_url + "/rerank")
