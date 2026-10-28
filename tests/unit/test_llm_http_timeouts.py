import ast
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import requests

from common.exceptions import ModelException
from core.llm import CvModel, EmbeddingModel, Seq2txtModel, TTSModel
from core.llm.tts import HTTPBasedTTS


@pytest.mark.parametrize("module", ["cv", "embedding", "rerank", "sequence2txt", "tts"])
def test_active_http_calls_have_explicit_bounded_timeout(module: str) -> None:
    tree = ast.parse((Path(__file__).parents[2] / "core" / "llm" / f"{module}.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) and node.func.value.id == "requests" and node.func.attr == "post":
            timeout = next((kw.value for kw in node.keywords if kw.arg == "timeout"), None)
            assert isinstance(timeout, ast.Constant) and isinstance(timeout.value, (int, float)) and 0 < timeout.value <= 120, node.lineno


@pytest.mark.parametrize("factory", ["HuggingFace", "SILICONFLOW", "NVIDIA", "Jina", "Perplexity"])
@pytest.mark.parametrize("query", [False, True])
def test_embedding_timeout_reaches_transport(monkeypatch: pytest.MonkeyPatch, factory: str, query: bool) -> None:
    calls: list[dict[str, Any]] = []

    def post(*args: Any, **kwargs: Any) -> requests.Response:
        calls.append(kwargs)
        raise requests.Timeout("controlled timeout")

    monkeypatch.setattr(requests, "post", post)
    model_name = "jina-embeddings-v3" if factory == "Jina" else "model"
    model = EmbeddingModel[factory]("x", model_name, base_url="http://provider.invalid")
    with pytest.raises(ModelException) as caught:
        model.encode_queries("document") if query else model.encode(["document"])
    assert caught.value.retryable and caught.value.status_code is None
    assert calls and all(call["timeout"] == 30 for call in calls)


@pytest.mark.parametrize("factory", ["NVIDIA", "ZHIPU-AI"])
def test_vision_timeout_and_generation_config_isolation(monkeypatch: pytest.MonkeyPatch, factory: str) -> None:
    def post(*args: Any, **kwargs: Any) -> requests.Response:
        assert kwargs["timeout"] == 60
        raise requests.Timeout("controlled timeout")

    monkeypatch.setattr(requests, "post", post)
    model = CvModel[factory]("x", "nvidia/model", base_url="http://provider.invalid")
    if factory == "ZHIPU-AI":
        # Legacy _request is also exercised by generation-config contract tests.
        model.base_url = "http://provider.invalid"
        model.api_key = "x"
    conf = {"temperature": 0.2}
    with pytest.raises(requests.Timeout, match="controlled timeout"):
        model._request([], False, conf) if factory == "ZHIPU-AI" else model._request([], conf)
    assert conf == {"temperature": 0.2}


def test_asr_timeout_keeps_explicit_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def post(*args: Any, **kwargs: Any) -> requests.Response:
        assert kwargs["timeout"] == 60
        raise requests.Timeout("controlled timeout")

    monkeypatch.setattr(requests, "post", post)
    result, tokens = Seq2txtModel["Xinference"]("x", "whisper", base_url="http://provider.invalid").transcription(b"audio")
    assert result == "**ERROR**: controlled timeout"
    assert tokens == 0


@pytest.mark.parametrize("factory", ["OpenAI", "RAGcon"])
def test_tts_closes_response_on_consumer_cancellation(monkeypatch: pytest.MonkeyPatch, factory: str) -> None:
    closed: list[bool] = []

    class Response(requests.Response):
        def __init__(self) -> None:
            super().__init__()
            self.status_code = 200

        def iter_content(self, *args: Any, **kwargs: Any) -> Iterator[bytes]:
            yield b"first"
            yield b"second"

        def close(self) -> None:
            closed.append(True)

    def post(*args: Any, **kwargs: Any) -> Response:
        assert kwargs["timeout"] == 60
        return Response()

    monkeypatch.setattr(requests, "post", post)
    stream = TTSModel[factory]("x", "model", base_url="http://provider.invalid").tts("hello")
    assert next(stream) == b"first"
    stream.close()
    assert closed == [True]


def test_tts_closes_error_response(monkeypatch: pytest.MonkeyPatch) -> None:
    response = requests.Response()
    response.status_code = 503
    response._content = b"provider unavailable"
    closed: list[bool] = []
    monkeypatch.setattr(response, "close", lambda: closed.append(True))
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: response)
    with pytest.raises(Exception, match="503, provider unavailable"):
        HTTPBasedTTS("x", "model", "http://provider.invalid")._send_request("/speech", {})
    assert closed == [True]


def test_fish_audio_timeout_and_context_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    closed: list[bool] = []

    class Client:
        def __enter__(self) -> "Client":
            return self

        def __exit__(self, *args: Any) -> None:
            closed.append(True)

        def stream(self, **kwargs: Any) -> Any:
            assert kwargs["timeout"] == 60
            raise httpx.ReadTimeout("controlled timeout")

    monkeypatch.setattr(httpx, "Client", Client)
    stream = TTSModel["Fish Audio"]('{"fish_audio_ak":"x","fish_audio_refid":"reference"}', "model").tts("hello")
    with pytest.raises(httpx.ReadTimeout, match="controlled timeout"):
        next(stream)
    assert closed == [True]


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan"), True, "30"])
def test_volcengine_rejects_unbounded_or_invalid_timeout(timeout: Any) -> None:
    with pytest.raises(ValueError, match="finite positive"):
        EmbeddingModel["VolcEngine"]('{"ark_api_key":"x"}', "model", timeout=timeout)


def test_volcengine_none_timeout_uses_bounded_default() -> None:
    model = EmbeddingModel["VolcEngine"]('{"ark_api_key":"x"}', "model", timeout=None)
    try:
        assert model.timeout == 30
    finally:
        model.session.close()
