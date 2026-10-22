import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Any

import dashscope
import httpx
import numpy as np
import pytest
import requests

from core.llm import RerankModel
from core.llm.rerank import Base

HTTP_FACTORIES = ["Jina", "Xinference", "LocalAI", "NVIDIA", "OpenAI-API-Compatible", "SILICONFLOW", "HuggingFace", "GPUStack", "302.AI", "RAGcon", "NovitaAI", "GiteeAI", "Jiekou.AI", "FuturMix"]


def response(payload: Any, status: int = 200) -> requests.Response:
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(payload).encode()
    result._content_consumed = True
    return result


def payload_for(factory: str, results: Any) -> Any:
    return results if factory == "HuggingFace" else {"rankings" if factory == "NVIDIA" else "results": results, "usage": {"total_tokens": 7}}


def model_for(factory: str, url: str = "http://provider.invalid/v1/rerank") -> Any:
    return RerankModel[factory]("x", "nvidia/model" if factory == "NVIDIA" else "model", base_url=url)


@pytest.mark.parametrize("factory", HTTP_FACTORIES)
def test_http_rerank_reconstructs_input_order_and_preserves_zero(monkeypatch: pytest.MonkeyPatch, factory: str) -> None:
    score = "score" if factory == "HuggingFace" else "logit" if factory == "NVIDIA" else "relevance_score"
    calls: list[dict[str, Any]] = []

    def post(*args: Any, **kwargs: Any) -> requests.Response:
        calls.append(kwargs)
        return response(payload_for(factory, [{"index": 1, score: 0.9}, {"index": 0, score: 0.0}]))

    monkeypatch.setattr(requests, "post", post)
    texts = ["zero", "high"]
    scores, tokens = model_for(factory).similarity("query", texts)
    np.testing.assert_array_equal(scores, [0.0, 0.9])
    assert tokens >= 0
    assert calls[0]["timeout"] == 30
    assert texts == ["zero", "high"]


@pytest.mark.parametrize("factory", HTTP_FACTORIES)
@pytest.mark.parametrize(
    "results",
    [
        None,
        [],
        [{"index": 0}],
        [{"index": 0, "value": None}],
        [{"index": 0, "value": True}],
        [{"index": 0, "value": "0"}],
        [{"index": 0, "value": float("nan")}],
        [{"index": 0, "value": float("inf")}],
        [{"index": -1, "value": 0}],
        [{"index": 2, "value": 0}],
        [{"index": True, "value": 0}],
        [{"index": 0, "value": 0}, {"index": 0, "value": 0.9}],
    ],
)
def test_malformed_rerank_results_fail_instead_of_zero_scores(monkeypatch: pytest.MonkeyPatch, factory: str, results: Any) -> None:
    score = "score" if factory == "HuggingFace" else "logit" if factory == "NVIDIA" else "relevance_score"
    converted = [{("index" if k == "index" else score): v for k, v in item.items()} for item in results] if results is not None else None
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: response(payload_for(factory, converted)))
    with pytest.raises(ValueError, match="Rerank response"):
        model_for(factory).similarity("query", ["document"] * (len(results) if results else 2))


@pytest.mark.parametrize("factory", HTTP_FACTORIES)
def test_provider_status_is_checked_before_success_payload(monkeypatch: pytest.MonkeyPatch, factory: str) -> None:
    score = "score" if factory == "HuggingFace" else "logit" if factory == "NVIDIA" else "relevance_score"
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: response(payload_for(factory, [{"index": 0, score: 0.9}]), 503))
    with pytest.raises((requests.HTTPError, ValueError), match="503"):
        model_for(factory).similarity("query", ["doc"])


@pytest.mark.parametrize("factory", HTTP_FACTORIES + ["Cohere", "VLLM", "Voyage AI", "Tongyi-Qianwen", "BAAI", "Youdao"])
@pytest.mark.parametrize(("query", "texts"), [("", ["doc"]), ("query", []), ("", [])])
def test_empty_rerank_does_no_provider_or_model_work(factory: str, query: str, texts: list[str]) -> None:
    provider = RerankModel[factory]
    # No credentials, network client or in-process model is initialized.
    scores, tokens = provider.__new__(provider).similarity(query, texts)
    np.testing.assert_array_equal(scores, np.zeros(len(texts)))
    assert tokens == 0


@pytest.mark.parametrize("factory", ["LocalAI", "OpenAI-API-Compatible", "RAGcon"])
def test_single_calibrated_score_is_preserved(monkeypatch: pytest.MonkeyPatch, factory: str) -> None:
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: response({"results": [{"index": 0, "relevance_score": 0.9}]}))
    scores, _ = model_for(factory).similarity("query", ["doc"])
    np.testing.assert_array_equal(scores, [0.9])


def test_normalize_empty_and_nonfinite() -> None:
    assert Base._normalize_rank(np.array([])).size == 0
    with pytest.raises(ValueError, match="non-finite"):
        Base._normalize_rank(np.array([float("nan")]))


@pytest.mark.parametrize("url", ["", " ", "ftp://host/rerank", "http:///rerank"])
def test_huggingface_invalid_url_fails_before_io(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    monkeypatch.setattr(requests, "post", lambda *args, **kwargs: pytest.fail("invalid URL must not send a request"))
    with pytest.raises(ValueError, match="base_url"):
        model_for("HuggingFace", url).similarity("query", ["doc"])


@pytest.mark.parametrize(
    ("factory", "expected"),
    [
        ("NovitaAI", "https://api.novita.ai/v3/openai/rerank"),
        ("GiteeAI", "https://ai.gitee.com/v1/rerank"),
        ("Jiekou.AI", "https://api.jiekou.ai/openai/v1/rerank"),
        ("302.AI", "https://api.302.ai/v1/rerank"),
    ],
)
def test_jina_subclass_endpoints_are_honored(factory: str, expected: str) -> None:
    assert RerankModel[factory]("x", "m").base_url == expected
    assert model_for(factory).base_url == "http://provider.invalid/v1/rerank"


def test_qwen_error_without_text_and_partial_success_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dashscope.TextReRank, "call", lambda **kwargs: dashscope.api_entities.dashscope_response.ReRankResponse(status_code=400, code="InvalidParameter", message="bad request"))
    with pytest.raises(ValueError, match="400 - bad request"):
        model_for("Tongyi-Qianwen").similarity("query", ["doc"])
    monkeypatch.setattr(dashscope.TextReRank, "call", lambda **kwargs: SimpleNamespace(status_code=200, output=SimpleNamespace(results=[SimpleNamespace(index=0, relevance_score=0)])))
    with pytest.raises(ValueError, match="one result per input"):
        model_for("Tongyi-Qianwen").similarity("query", ["doc", "missing"])


@pytest.mark.parametrize(("factory", "module"), [("Cohere", "cohere"), ("Voyage AI", "voyageai")])
def test_rerank_sdk_clients_have_timeout_and_strict_indexed_results(monkeypatch: pytest.MonkeyPatch, factory: str, module: str) -> None:
    import importlib

    calls: list[dict[str, Any]] = []
    malformed = [False]

    def rerank(**kwargs: Any) -> SimpleNamespace:
        results = [SimpleNamespace(index=1, relevance_score=0.9), SimpleNamespace(index=0, relevance_score=0.0)]
        return SimpleNamespace(results=results[:1] if malformed[0] else results, total_tokens=7)

    def client(**kwargs: Any) -> SimpleNamespace:
        calls.append(kwargs)
        return SimpleNamespace(rerank=rerank)

    monkeypatch.setattr(importlib.import_module(module), "Client", client)
    model = model_for(factory)
    assert calls[0]["timeout"] == 30
    scores, _ = model.similarity("query", ["zero", "high"])
    np.testing.assert_array_equal(scores, [0.0, 0.9])
    malformed[0] = True
    with pytest.raises(ValueError, match="one result per input"):
        model.similarity("query", ["zero", "high"])


def test_huggingface_later_batch_failure_stops_without_partial_success(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    def post(*args: Any, **kwargs: Any) -> requests.Response:
        calls.append(len(kwargs["json"]["texts"]))
        if len(calls) == 2:
            raise requests.Timeout("second batch failed")
        return response([{"index": i, "score": 0.0} for i in range(len(kwargs["json"]["texts"]))])

    monkeypatch.setattr(requests, "post", post)
    with pytest.raises(requests.Timeout, match="second batch failed"):
        model_for("HuggingFace").similarity("query", ["doc"] * 17)
    assert calls == [8, 8]


@pytest.mark.parametrize("invalid", [{"index": True, "relevance_score": 0.9}, {"index": "1", "relevance_score": 0.9}, {"index": 1, "relevance_score": False}, {"index": 1, "relevance_score": "0.9"}])
def test_real_cohere_sdk_does_not_coerce_invalid_wire_values(monkeypatch: pytest.MonkeyPatch, invalid: dict[str, Any]) -> None:
    requests_seen: list[httpx.Request] = []
    payload = [{"index": 1, "relevance_score": 0.9}, {"index": 0, "relevance_score": 0.0}]

    def reply(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        return httpx.Response(200, json={"id": "fixture", "results": payload, "meta": {"api_version": {"version": "1"}, "billed_units": {"search_units": 1}}})

    clients: list[httpx.Client] = []
    real_client = httpx.Client

    def client(**kwargs: Any) -> httpx.Client:
        created = real_client(transport=httpx.MockTransport(reply), **kwargs)
        clients.append(created)
        return created

    monkeypatch.setattr(httpx, "Client", client)
    try:
        model = model_for("Cohere", "http://provider.invalid/v1")
        scores, _ = model.similarity("query", ["zero", "high"])
        np.testing.assert_array_equal(scores, [0.0, 0.9])
        assert requests_seen[0].url.path == "/v1/rerank"
        assert requests_seen[0].extensions["timeout"]["read"] == 30
        payload[0] = invalid
        with pytest.raises(ValueError, match="Rerank response"):
            model.similarity("query", ["zero", "high"])
        assert len(requests_seen) == 2
    finally:
        for created in clients:
            created.close()


@pytest.mark.enable_socket
def test_registered_rerank_real_http_batches_errors_and_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    requests_seen: list[str] = []
    release = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            requests_seen.append(self.path)
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path.startswith("/slow"):
                release.wait(1)
                return
            status = 503 if self.path == "/unavailable" else 200
            body = json.dumps([{"index": i, "score": float(text)} for i, text in reversed(list(enumerate(data.get("texts", []))))]).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        scores, _ = model_for("HuggingFace", url + "/gateway/rerank?tenant=a").similarity("query", [str(i) for i in range(17)])
        np.testing.assert_array_equal(scores, np.arange(17))
        assert requests_seen == ["/gateway/rerank?tenant=a"] * 3
        with pytest.raises(requests.HTTPError, match="503"):
            model_for("Jina", url + "/unavailable").similarity("query", ["doc"])
        real_post = requests.post

        def short_post(*args: Any, **kwargs: Any) -> requests.Response:
            assert kwargs["timeout"] == 30
            kwargs["timeout"] = 0.05
            return real_post(*args, **kwargs)

        monkeypatch.setattr(requests, "post", short_post)
        with pytest.raises(requests.Timeout):
            model_for("HuggingFace", url + "/slow").similarity("query", ["doc"])
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
    assert not worker.is_alive()
