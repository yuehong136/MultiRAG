"""Provider errors expose safe status/retry metadata on the active embedding chain."""

import asyncio
import json
from typing import Any

import httpx
import pytest
import requests
from openai import OpenAI

from common.exceptions import ModelException
from core.llm import EmbeddingModel, embedding

HTTP_FACTORIES = ["HuggingFace", "SILICONFLOW", "NVIDIA", "Jina", "Perplexity"]
SECRET = "provider-secret-and-private-document"


def reply(status: int, body: Any) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response._content = json.dumps(body).encode()
    response._content_consumed = True
    return response


@pytest.mark.parametrize("factory", HTTP_FACTORIES)
@pytest.mark.parametrize("query", [False, True])
@pytest.mark.parametrize(("status", "retryable"), [(400, False), (401, False), (403, False), (404, False), (408, True), (429, True), (500, True), (503, True)])
def test_http_failures_have_safe_consistent_classification(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, factory: str, query: bool, status: int, retryable: bool) -> None:
    calls: list[dict[str, Any]] = []

    def post(**kwargs: Any) -> requests.Response:
        calls.append(kwargs)
        return reply(status, {"error": SECRET})

    monkeypatch.setattr(requests, "post", post)
    model = EmbeddingModel[factory](SECRET, "model", base_url="http://fixture.invalid")
    with pytest.raises(ModelException) as caught:
        model.encode_queries("query") if query else model.encode(["document"])
    assert caught.value.retryable is retryable
    assert caught.value.status_code == status
    assert str(status) in str(caught.value)
    assert SECRET not in str(caught.value) + caplog.text
    assert len(calls) == 1 and calls[0]["timeout"] == 30


@pytest.mark.parametrize("factory", HTTP_FACTORIES)
def test_malformed_http_response_does_not_leak_provider_body(monkeypatch: pytest.MonkeyPatch, factory: str) -> None:
    monkeypatch.setattr(requests, "post", lambda **kwargs: reply(200, {"error": SECRET}))
    model = EmbeddingModel[factory](SECRET, "model", base_url="http://fixture.invalid")
    with pytest.raises(ModelException, match="Invalid embedding provider response") as caught:
        model.encode(["document"])
    assert caught.value.retryable is False
    assert SECRET not in str(caught.value)


@pytest.mark.parametrize("query", [False, True])
@pytest.mark.parametrize(("status", "retryable"), [(400, False), (401, False), (408, True), (429, True), (503, True)])
def test_real_openai_sdk_errors_are_classified(query: bool, status: int, retryable: bool) -> None:
    calls: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(status, json={"error": {"message": SECRET}})

    model = embedding.OpenAIEmbed.__new__(embedding.OpenAIEmbed)
    model.model_name = "fixture"
    with OpenAI(api_key=SECRET, base_url="http://fixture.invalid/v1", max_retries=0, http_client=httpx.Client(transport=httpx.MockTransport(respond))) as client:
        model.client = client
        with pytest.raises(ModelException) as caught:
            model.encode_queries("query") if query else model.encode(["document"])
    assert caught.value.retryable is retryable and caught.value.status_code == status
    assert SECRET not in str(caught.value)
    assert len(calls) == 1


@pytest.mark.parametrize("factory", HTTP_FACTORIES)
@pytest.mark.parametrize("error", [requests.Timeout, requests.ConnectionError])
def test_http_transport_failure_is_retryable(monkeypatch: pytest.MonkeyPatch, factory: str, error: type[Exception]) -> None:
    def fail(**kwargs: Any) -> Any:
        raise error(SECRET)

    monkeypatch.setattr(requests, "post", fail)
    model = EmbeddingModel[factory](SECRET, "model", base_url="http://fixture.invalid")
    with pytest.raises(ModelException) as caught:
        model.encode_queries("query")
    assert caught.value.retryable and caught.value.status_code is None
    assert SECRET not in str(caught.value)


def test_cancellation_is_not_wrapped_or_retried() -> None:
    def cancel() -> Any:
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        embedding._model_request(cancel)


@pytest.mark.parametrize(("status", "retryable", "attempts"), [(401, False, 1), (408, True, 6), (429, True, 6), (503, True, 6)])
def test_qwen_status_retries_are_bounded_and_redacted(monkeypatch: pytest.MonkeyPatch, status: int, retryable: bool, attempts: int) -> None:
    calls: list[dict[str, Any]] = []
    waits: list[int] = []

    def call(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"status_code": status, "message": SECRET}

    monkeypatch.setattr(embedding.dashscope.TextEmbedding, "call", call)
    monkeypatch.setattr(embedding.time, "sleep", waits.append)
    with pytest.raises(ModelException) as caught:
        embedding.QWenEmbed(SECRET).encode(["document"])
    assert caught.value.retryable is retryable and caught.value.status_code == status
    assert len(calls) == attempts and waits == [10] * (attempts - 1)
    assert SECRET not in str(caught.value)


@pytest.mark.parametrize("factory", ["NVIDIA", "SILICONFLOW", "Jina", "Perplexity", "HuggingFace", "OpenAI"])
def test_valid_vectors_remain_ordered_and_usage_is_preserved(monkeypatch: pytest.MonkeyPatch, factory: str) -> None:
    import base64
    from types import SimpleNamespace

    import numpy as np

    payload: Any = {"data": [{"index": 1, "embedding": [2.0, 0.0]}, {"index": 0, "embedding": [1.0, 0.0]}], "usage": {"total_tokens": 7}}
    if factory == "Perplexity":
        for entry in payload["data"]:
            entry["embedding"] = base64.b64encode(bytes([int(entry["embedding"][0]), 0])).decode()
    if factory == "HuggingFace":
        payload = [[1.0, 0.0], [2.0, 0.0]]
    monkeypatch.setattr(requests, "post", lambda **kwargs: reply(200, payload))
    model_name = "jina-embeddings-v3" if factory == "Jina" else "model"
    model = EmbeddingModel[factory]("fixture", model_name, base_url="http://fixture.invalid")
    if factory == "OpenAI":
        model.client.close()
        model.client = SimpleNamespace(
            embeddings=SimpleNamespace(
                create=lambda **kwargs: SimpleNamespace(data=[SimpleNamespace(index=1, embedding=[2.0, 0.0]), SimpleNamespace(index=0, embedding=[1.0, 0.0])], usage=SimpleNamespace(total_tokens=7))
            )
        )
    vectors, tokens = model.encode(["one", "two"])
    np.testing.assert_array_equal(vectors, [[1.0, 0.0], [2.0, 0.0]])
    assert tokens == (2 if factory == "HuggingFace" else 7)


@pytest.mark.parametrize("entries", [[{"index": 0}, {"index": 0}], [{"index": -1}, {"index": 1}], [{"index": True}, {"index": 1}], [{"index": 0}], [{}, {}]])
def test_duplicate_missing_and_invalid_indices_fail(entries: list[dict[str, Any]]) -> None:
    with pytest.raises(ModelException):
        embedding._ordered_embedding_entries(entries, 2)


@pytest.mark.parametrize("vectors", [[[1.0], [float("nan")]], [[float("inf")], [1.0]], [[1.0], [1.0, 2.0]], [[], []], [[None], [1.0]], [[True], [False]], [["1"], ["2"]]])
def test_invalid_vectors_are_never_accepted(vectors: Any) -> None:
    with pytest.raises(ModelException, match="malformed vectors"):
        embedding._validated_embedding_vectors(vectors, 2)


@pytest.mark.parametrize("model_name", ["jina-embeddings-v3", "jina-embeddings-v4"])
def test_jina_multivectors_keep_float32_and_input_alignment(monkeypatch: pytest.MonkeyPatch, model_name: str) -> None:
    import numpy as np

    payload = {
        "data": [{"index": 1, "embedding": [3.0, 4.0], "embeddings": [[2.0, 3.0], [4.0, 5.0]]}, {"index": 0, "embedding": [1.0, 2.0], "embeddings": [[0.0, 1.0], [2.0, 3.0]]}],
        "usage": {"total_tokens": 0},
    }
    monkeypatch.setattr(requests, "post", lambda **kwargs: reply(200, payload))
    vectors, tokens = embedding.JinaMultiVecEmbed("fixture", model_name).encode(["one", "two"])
    np.testing.assert_array_equal(vectors, [[1.0, 2.0], [3.0, 4.0]])
    assert vectors.dtype == np.float32 and tokens == 0


def test_contextualized_perplexity_preserves_document_and_chunk_indices(monkeypatch: pytest.MonkeyPatch) -> None:
    import base64

    import numpy as np

    payload = {"data": [{"index": index, "data": [{"index": 0, "embedding": base64.b64encode(bytes([index + 1, 0])).decode()}]} for index in [1, 0]], "usage": {"total_tokens": 0}}
    calls: list[dict[str, Any]] = []

    def post(**kwargs: Any) -> requests.Response:
        calls.append(kwargs)
        return reply(200, payload)

    monkeypatch.setattr(requests, "post", post)
    vectors, tokens = embedding.PerplexityEmbed("fixture", "pplx-embed-context-v1-0.6b").encode(["one", "two"])
    np.testing.assert_array_equal(vectors, [[1.0, 0.0], [2.0, 0.0]])
    assert calls[0]["json"]["input"] == [["one"], ["two"]]
    assert vectors.dtype == np.float32 and tokens == 0


@pytest.mark.parametrize("factory", HTTP_FACTORIES)
def test_empty_batch_makes_no_provider_call(monkeypatch: pytest.MonkeyPatch, factory: str) -> None:
    def post(**kwargs: Any) -> requests.Response:
        pytest.fail("Empty embedding batch must not call provider")

    monkeypatch.setattr(requests, "post", post)
    model = EmbeddingModel[factory]("fixture", "model", base_url="http://fixture.invalid")
    vectors, tokens = model.encode([])
    assert vectors.size == 0 and tokens == 0
