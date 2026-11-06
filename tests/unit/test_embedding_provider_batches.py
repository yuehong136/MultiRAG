"""Provider batch contracts, safe failure semantics and submitted-token counts."""

import io
import json
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
import requests

from common.exceptions import ModelException
from common.token_utils import num_tokens_from_string
from core.llm import embedding

PROVIDERS = [
    embedding.LocalAIEmbed,
    embedding.XinferenceEmbed,
    embedding.ZhipuEmbed,
    embedding.OllamaEmbed,
    embedding.MistralEmbed,
    embedding.BedrockEmbed,
    embedding.GeminiEmbed,
    embedding.NvidiaEmbed,
    embedding.CoHereEmbed,
    embedding.SILICONFLOWEmbed,
    embedding.VoyageEmbed,
]


def sdk_response(batch: list[str]) -> SimpleNamespace:
    return SimpleNamespace(data=[SimpleNamespace(index=index, embedding=[float(len(text)), 1.0]) for index, text in reversed(list(enumerate(batch)))], usage=SimpleNamespace(total_tokens=len(batch)))


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> Any:
    def build(cls: type, *, fail_at: int = 0, malformed: bool = False) -> tuple[Any, list[dict[str, Any]]]:
        model = cls.__new__(cls)
        model.model_name = "embedding-3" if cls is embedding.ZhipuEmbed else "fixture"
        model.base_url, model.headers = "http://fixture.invalid", {}
        model.is_amazon, model.is_cohere = True, False
        model.keep_alive, model.types = -1, SimpleNamespace(EmbedContentConfig=lambda **kw: kw)
        calls: list[dict[str, Any]] = []

        def call(**kw: Any) -> Any:
            calls.append(kw)
            if fail_at and len(calls) == fail_at:
                error = requests.HTTPError("private-key-and-document")
                error.status_code = 401
                raise error
            batch = kw.get("input", kw.get("texts", kw.get("contents", [])))
            if "body" in kw:
                batch = [json.loads(kw["body"])["inputText"]]
            if "json" in kw:
                batch = kw["json"]["input"]
            vectors = [[float(len(text)), 1.0] for text in batch]
            if malformed:
                vectors = [[float("nan")]] * len(batch)
            if cls is embedding.OllamaEmbed:
                return {"embeddings": vectors, "prompt_eval_count": len(batch)}
            if cls is embedding.BedrockEmbed:
                return {"body": io.BytesIO(json.dumps({"embedding": vectors[0]}).encode())}
            if cls is embedding.GeminiEmbed:
                return {"embeddings": [{"values": value} for value in vectors]}
            if cls is embedding.CoHereEmbed:
                return SimpleNamespace(embeddings=SimpleNamespace(float=vectors), usage=SimpleNamespace(total_tokens=len(batch)))
            if cls is embedding.VoyageEmbed:
                return SimpleNamespace(embeddings=vectors, total_tokens=len(batch))
            if cls in {embedding.ZhipuEmbed, embedding.NvidiaEmbed, embedding.SILICONFLOWEmbed}:
                response = requests.Response()
                response.status_code = 200
                response._content_consumed = True
                response._content = json.dumps({"data": [{"index": index, "embedding": value} for index, value in reversed(list(enumerate(vectors)))], "usage": {"total_tokens": len(batch)}}).encode()
                return response
            return sdk_response(batch) if not malformed else SimpleNamespace(data=[SimpleNamespace(index=i, embedding=v) for i, v in enumerate(vectors)])

        model.client = SimpleNamespace(embeddings=SimpleNamespace(create=call), embed=call, invoke_model=call, models=SimpleNamespace(embed_content=call))
        if cls is embedding.MistralEmbed:
            model.client.embeddings = call
        monkeypatch.setattr(requests, "post", call)
        return model, calls

    return build


@pytest.mark.parametrize("cls", PROVIDERS)
def test_provider_batches_keep_order_counts_and_query_shape(provider: Any, cls: type) -> None:
    model, calls = provider(cls)
    texts = ["x" * (i + 1) for i in range(20)]
    vectors, tokens = model.encode(texts)
    np.testing.assert_array_equal(vectors, [[len(text), 1] for text in texts])
    assert len(calls) == (20 if cls is embedding.BedrockEmbed else 2)
    expected = sum(num_tokens_from_string(t) for t in texts) if cls in {embedding.BedrockEmbed, embedding.GeminiEmbed} else 20
    assert tokens == expected
    vector, _ = model.encode_queries("query")
    assert vector.shape == (2,)
    assert vector[0] == 5
    if cls is embedding.NvidiaEmbed:
        assert calls[0]["json"]["input_type"] == "passage" and calls[-1]["json"]["input_type"] == "query"
    if cls is embedding.CoHereEmbed:
        assert calls[-1]["texts"] == ["query"] and calls[-1]["input_type"] == "search_query" and calls[-1]["truncate"] == "END"
    if cls is embedding.VoyageEmbed:
        assert calls[-1]["texts"] == ["query"] and calls[-1]["input_type"] == "query" and calls[-1]["truncation"] is True


@pytest.mark.parametrize("cls", PROVIDERS)
def test_empty_provider_batch_performs_no_io(provider: Any, cls: type) -> None:
    model, calls = provider(cls)
    vectors, tokens = model.encode([])
    assert vectors.size == 0 and tokens == 0 and calls == []


@pytest.mark.parametrize("cls", PROVIDERS)
def test_partial_batch_failure_is_classified_without_partial_success(provider: Any, cls: type, caplog: pytest.LogCaptureFixture) -> None:
    model, calls = provider(cls, fail_at=2)
    with pytest.raises(ModelException) as caught:
        model.encode(["document"] * 20)
    assert caught.value.status_code == 401 and caught.value.retryable is False
    assert len(calls) == 2
    assert "private-key-and-document" not in str(caught.value) + caplog.text


@pytest.mark.parametrize("cls", PROVIDERS)
def test_invalid_vectors_are_rejected_on_every_provider(provider: Any, cls: type) -> None:
    model, _ = provider(cls, malformed=True)
    with pytest.raises(ModelException):
        model.encode(["document"])


@pytest.mark.parametrize("cls,limit", [(embedding.MistralEmbed, 8192), (embedding.BedrockEmbed, 8192), (embedding.GeminiEmbed, 2048), (embedding.ZhipuEmbed, 3072)])
def test_query_and_document_truncation_and_accounting(provider: Any, cls: type, limit: int) -> None:
    model, calls = provider(cls)
    huge = "word " * 12000
    _, tokens = model.encode_queries(huge)
    call = calls[-1]
    submitted = json.loads(call["body"])["inputText"] if cls is embedding.BedrockEmbed else call["json"]["input"][0] if cls is embedding.ZhipuEmbed else call.get("contents", call.get("input"))[0]
    assert num_tokens_from_string(submitted) <= limit
    if cls in {embedding.BedrockEmbed, embedding.GeminiEmbed}:
        assert tokens == num_tokens_from_string(submitted)


@pytest.mark.parametrize("tokens", [None, 0, 42])
def test_localai_uses_reported_or_estimated_tokens(tokens: int | None) -> None:
    model = embedding.LocalAIEmbed.__new__(embedding.LocalAIEmbed)
    model.model_name = "fixture"

    def call(**kw: Any) -> SimpleNamespace:
        response = sdk_response(kw["input"])
        response.usage = SimpleNamespace(total_tokens=tokens) if tokens is not None else None
        return response

    model.client = SimpleNamespace(embeddings=SimpleNamespace(create=call))
    _, actual = model.encode(["hello world"])
    assert actual == (tokens if tokens is not None else num_tokens_from_string("hello world"))


def test_mistral_retries_only_transient_statuses(monkeypatch: pytest.MonkeyPatch) -> None:
    model = embedding.MistralEmbed.__new__(embedding.MistralEmbed)
    model.model_name = "fixture"
    calls: list[int] = []
    waits: list[int] = []

    def call(**kw: Any) -> Any:
        calls.append(1)
        error = requests.HTTPError("private")
        error.status_code = 503
        raise error

    model.client = SimpleNamespace(embeddings=call)
    monkeypatch.setattr(embedding.time, "sleep", waits.append)
    with pytest.raises(ModelException) as caught:
        model.encode_queries("query")
    assert caught.value.retryable is True and len(calls) == 5 and waits == [20] * 4


def test_bedrock_cohere_reads_plural_vectors_and_closes_stream() -> None:
    model = embedding.BedrockEmbed.__new__(embedding.BedrockEmbed)
    model.model_name, model.is_amazon, model.is_cohere = "cohere.embed", False, True
    streams: list[io.BytesIO] = []
    bodies: list[dict[str, Any]] = []

    def invoke(**kw: Any) -> dict[str, Any]:
        bodies.append(json.loads(kw["body"]))
        stream = io.BytesIO(b'{"embeddings":[[1,2]]}')
        streams.append(stream)
        return {"body": stream}

    model.client = SimpleNamespace(invoke_model=invoke)
    assert model.encode(["document"])[0].tolist() == [[1, 2]]
    assert model.encode_queries("query")[0].tolist() == [1, 2]
    assert [body["input_type"] for body in bodies] == ["search_document", "search_query"]
    assert all(stream.closed for stream in streams)


def test_locked_sdks_accept_batch_and_truncation_contracts() -> None:
    import inspect

    import cohere
    import ollama
    import voyageai
    from mistralai.client import MistralClient

    assert "input" in inspect.signature(ollama.Client.embed).parameters
    assert "truncate" in inspect.signature(ollama.Client.embed).parameters
    assert "truncate" in inspect.signature(cohere.Client.embed).parameters
    assert "truncation" in inspect.signature(voyageai.Client.embed).parameters
    assert "input" in inspect.signature(MistralClient.embeddings).parameters


def test_bedrock_sdk_error_keeps_http_status_without_diagnostics() -> None:
    from botocore.exceptions import ClientError

    def fail() -> Any:
        raise ClientError({"Error": {"Code": "ThrottlingException", "Message": "private-credential"}, "ResponseMetadata": {"HTTPStatusCode": 429}}, "InvokeModel")

    with pytest.raises(ModelException) as caught:
        embedding._model_request(fail)
    assert caught.value.status_code == 429 and caught.value.retryable is True
    assert "private-credential" not in str(caught.value)


@pytest.mark.parametrize("factory", [embedding.JinaMultiVecEmbed, embedding.NvidiaEmbed, embedding.SILICONFLOWEmbed, embedding.ZhipuEmbed, embedding.HuggingFaceEmbed, embedding.PerplexityEmbed])
def test_http_parse_failure_releases_response(monkeypatch: pytest.MonkeyPatch, factory: type) -> None:
    response = requests.Response()
    response.status_code, response._content, response._content_consumed = 200, b'{"error":"private"}', True
    closed: list[int] = []
    monkeypatch.setattr(response, "close", lambda: closed.append(1))
    monkeypatch.setattr(requests, "post", lambda **_kw: response)
    model = factory("fixture", "fixture", base_url="http://fixture.invalid")
    with pytest.raises(ModelException):
        model.encode(["document"])
    assert closed == [1]


@pytest.mark.parametrize("sdk,status,retryable", [("voyage", 429, True), ("mistral", 503, True), ("gemini", 500, True), ("cohere", 401, False), ("ollama", 408, True)])
def test_actual_locked_sdk_exception_metadata_is_preserved(sdk: str, status: int, retryable: bool, caplog: pytest.LogCaptureFixture) -> None:
    from cohere.core.api_error import ApiError
    from google.genai.errors import APIError
    from mistralai.exceptions import MistralAPIException
    from ollama import ResponseError
    from voyageai.error import RateLimitError

    error = {
        "voyage": RateLimitError("private-sdk-credential", http_status=status),
        "mistral": MistralAPIException("private-sdk-credential", http_status=status),
        "gemini": APIError(status, {"error": {"message": "private-sdk-credential", "code": status}}),
        "cohere": ApiError(status_code=status, body={"message": "private-sdk-credential"}),
        "ollama": ResponseError("private-sdk-credential", status_code=status),
    }[sdk]

    def fail() -> Any:
        raise error

    with pytest.raises(ModelException) as caught:
        embedding._model_request(fail)
    assert caught.value.status_code == status and caught.value.retryable is retryable
    assert "private-sdk-credential" not in str(caught.value) + caplog.text


@pytest.mark.parametrize("sdk", ["mistral", "voyage", "httpx"])
def test_actual_locked_sdk_transport_failures_are_retryable(sdk: str) -> None:
    import httpx
    from mistralai.exceptions import MistralConnectionException
    from voyageai.error import Timeout

    error = {"mistral": MistralConnectionException("private"), "voyage": Timeout("private"), "httpx": httpx.ConnectError("private")}[sdk]

    def fail() -> Any:
        raise error

    with pytest.raises(ModelException) as caught:
        embedding._model_request(fail)
    assert caught.value.status_code is None and caught.value.retryable is True
    assert "private" not in str(caught.value)
