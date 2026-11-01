"""The registered Azure adapters share raw/envelope auth on actual SDK requests."""

import json
from typing import Any

import httpx
import numpy as np
import pytest

from core.llm import CvModel, EmbeddingModel
from core.llm.azure_auth import resolve_azure_credentials
from core.llm.cv import AzureGptV4
from core.llm.embedding import AzureEmbed


@pytest.mark.parametrize(
    "key,version",
    [
        ("private-raw-key", "2024-02-01"),
        ('{"api_key":"private-raw-key"}', "2024-02-01"),
        ('{"api_key":"private-raw-key","api_version":"2025-01-01-preview"}', "2025-01-01-preview"),
        ({"api_key": "private-raw-key", "api_version": "2025-01-01-preview"}, "2025-01-01-preview"),
    ],
)
async def test_registered_azure_embedding_and_vision_send_resolved_credentials(key: Any, version: str, caplog: pytest.LogCaptureFixture) -> None:
    assert EmbeddingModel["Azure-OpenAI"] is AzureEmbed
    assert CvModel["Azure-OpenAI"] is AzureGptV4
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.params["api-version"] == version
        assert request.headers["api-key"] == "private-raw-key"
        body = json.loads(request.content)
        if request.url.path.endswith("/embeddings"):
            assert body["model"] == "embed-deployment"
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.0, 1.0]}], "usage": {"total_tokens": 2}})
        assert "/deployments/vision-deployment/chat/completions" in request.url.path
        return httpx.Response(
            200,
            json={
                "id": "test",
                "created": 1,
                "model": "vision-deployment",
                "object": "chat.completion",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}],
                "usage": {"total_tokens": 2},
            },
        )

    embedding = EmbeddingModel["Azure-OpenAI"](key, "embed-deployment", base_url="https://azure.test")
    vision = CvModel["Azure-OpenAI"](key, "vision-deployment", base_url="https://azure.test")
    embedding.client._client.close()
    embedding.client._client = httpx.Client(transport=httpx.MockTransport(handle))
    vision.client._client.close()
    vision.client._client = httpx.Client(transport=httpx.MockTransport(handle))
    await vision.async_client._client.aclose()
    vision.async_client._client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    try:
        vectors, count = embedding.encode(["Hi"])
        np.testing.assert_array_equal(vectors, [[0.0, 1.0]])
        assert count == 2
        query, count = embedding.encode_queries("Hi")
        np.testing.assert_array_equal(query, [0.0, 1.0])
        assert count == 2
        assert vision.describe_with_prompt(b"image", "Read") == ("OK", 2)
        assert await vision.async_chat("", [{"role": "user", "content": "Read"}], {}, images=[b"image"]) == ("OK", 2)
    finally:
        embedding.client.close()
        vision.client.close()
        await vision.async_client.close()
    assert len(requests) == 4
    assert "private-raw-key" not in caplog.text
    assert embedding.client.is_closed() and vision.client.is_closed() and vision.async_client.is_closed()


@pytest.mark.parametrize("key", ["plain-key", "{malformed-key", '"quoted-key"', "[1,2]", "null", "123"])
def test_raw_strings_and_non_object_json_keep_the_exact_key_without_logging(key: str, caplog: pytest.LogCaptureFixture) -> None:
    assert resolve_azure_credentials(key) == (key, "2024-02-01")
    assert key not in caplog.text


@pytest.mark.parametrize("key", ['{"api_key":null}', '{"api_key":{}}', '{"api_key":"private-key","api_version":null}', '{"api_key":"private-key","api_version":""}'])
def test_invalid_object_fields_do_not_echo_the_credential(key: str, caplog: pytest.LogCaptureFixture) -> None:
    with pytest.raises(ValueError, match=r"^Invalid Azure credential envelope$") as error:
        resolve_azure_credentials(key)
    assert "private-key" not in str(error.value) and "private-key" not in caplog.text
