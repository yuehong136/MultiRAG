"""Tongyi's registered factory uses the pinned DashScope SDK and a controlled HTTP transport."""

import copy
import json
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import dashscope
import numpy as np
import pytest
import requests
from sqlalchemy.orm import Session

from api.db.services.tenant_llm_service import TenantLLMService
from common.constants import LLMType
from common.exceptions import ModelException
from common.token_utils import num_tokens_from_string
from core.llm import EmbeddingModel, embedding

CN = "https://dashscope.aliyuncs.com"
INTL = "https://dashscope-intl.aliyuncs.com"
SERVICE = "/services/embeddings/text-embedding/text-embedding"
FAKE_KEY = "qwen-unit-test-key"
Reply = tuple[int, dict[str, Any]]


def make_model(db: Session, base_url: str | None) -> embedding.QWenEmbed:
    assert EmbeddingModel["Tongyi-Qianwen"] is embedding.QWenEmbed
    model = TenantLLMService.model_instance(
        db,
        "qwen-unit-tenant",
        {
            "llm_factory": "Tongyi-Qianwen",
            "llm_name": "text-embedding-v4",
            "mdl_type": LLMType.EMBEDDING.value,
            "api_key": FAKE_KEY,
            "api_base": base_url,
        },
    )
    assert isinstance(model, embedding.QWenEmbed)
    return model


def success(texts: list[str], tokens: Any = 0) -> dict[str, Any]:
    return {
        "output": {"embeddings": [{"text_index": i, "embedding": [float(text) if text.isdigit() else 0.5, 0.0]} for i, text in reversed(list(enumerate(texts)))]},
        "usage": {"total_tokens": tokens},
    }


@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch) -> tuple[list[dict[str, Any]], list[Reply]]:
    calls: list[dict[str, Any]] = []
    replies: list[Reply] = []

    def send(session: requests.Session, request: requests.PreparedRequest, **kwargs: Any) -> requests.Response:
        body = json.loads(request.body)
        calls.append({"url": request.url, "body": body, "authorization": request.headers["Authorization"], "sdk_default": dashscope.base_http_api_url})
        status, payload = replies.pop(0) if replies else (200, success(body["input"]["texts"], tokens=len(body["input"]["texts"])))
        response = requests.Response()
        response.status_code = status
        response.headers["Content-Type"] = "application/json"
        response._content = json.dumps(payload).encode()
        response.request = request
        return response

    monkeypatch.setattr(requests.Session, "send", send)
    return calls, replies


@pytest.mark.parametrize(
    ("configured", "native"),
    [
        (None, None),
        ("", None),
        (" \t ", None),
        (CN, f"{CN}/api/v1"),
        (f"{CN}/compatible-mode/v1/", f"{CN}/api/v1"),
        (INTL, f"{INTL}/api/v1"),
        (f" {INTL}/compatible-mode/v1/ ", f"{INTL}/api/v1"),
        (f"{CN}/api/v1/", f"{CN}/api/v1"),
        (f"{INTL}/api/v1", f"{INTL}/api/v1"),
        ("https://DASHSCOPE-INTL.ALIYUNCS.COM/compatible-mode/v1", f"{INTL}/api/v1"),
        ("http://localhost:8765/proxy/api/v1/", "http://localhost:8765/proxy/api/v1"),
        ("https://dashscope-us.aliyuncs.com/api/v1", "https://dashscope-us.aliyuncs.com/api/v1"),
        ("https://dashscope-intl.aliyuncs.com.example/api/v1", "https://dashscope-intl.aliyuncs.com.example/api/v1"),
    ],
)
@pytest.mark.parametrize("query", [False, True])
def test_registered_factory_routes_native_endpoint(db: Session, transport: tuple[list[dict[str, Any]], list[Reply]], configured: str | None, native: str | None, query: bool) -> None:
    calls, _ = transport
    previous = dashscope.base_http_api_url
    model = make_model(db, configured)
    vectors, tokens = model.encode_queries("7") if query else model.encode(["7"])
    np.testing.assert_array_equal(vectors, [7.0, 0.0] if query else [[7.0, 0.0]])
    assert tokens == 1
    assert calls[0]["url"] == f"{(native or previous).rstrip('/')}{SERVICE}"
    assert calls[0]["body"] == {"model": "text-embedding-v4", "input": {"texts": ["7"]}, "parameters": {"text_type": "query" if query else "document"}}
    assert calls[0]["authorization"] == f"Bearer {FAKE_KEY}"
    assert calls[0]["sdk_default"] == previous
    assert dashscope.base_http_api_url == previous


@pytest.mark.parametrize(
    "configured",
    [
        "https://dashscope-intl.aliyuncs.com.example/compatible-mode/v1",
        "https://example.com/dashscope-intl.aliyuncs.com/compatible-mode/v1",
        "https://example.com/compatible-mode/v1?host=dashscope-intl.aliyuncs.com",
        "https://example.com/compatible-mode/v1",
        "dashscope-intl.aliyuncs.com/compatible-mode/v1",
        "ftp://dashscope-intl.aliyuncs.com/api/v1",
        "https://user:url-password@dashscope-intl.aliyuncs.com/api/v1",
        "https://dashscope-intl.aliyuncs.com/compatible-mode/v1?key=url-secret",
        "https://dashscope-intl.aliyuncs.com/api/v1#url-secret",
        "https://dashscope-intl.aliyuncs.com/unexpected",
        "https://dashscope-intl.aliyuncs.com:bad/api/v1",
        "https://[broken/api/v1",
        "https://dashscope-intl.aliyuncs.com\n/api/v1",
    ],
)
def test_invalid_explicit_urls_fail_without_transport_or_secrets(db: Session, transport: tuple[list[dict[str, Any]], list[Reply]], caplog: pytest.LogCaptureFixture, configured: str) -> None:
    calls, _ = transport
    with pytest.raises(ValueError, match="base_url") as error:
        make_model(db, configured)
    assert calls == []
    for secret in [FAKE_KEY, "url-password", "url-secret", configured]:
        assert secret not in str(error.value)
        assert secret not in caplog.text


def test_document_batches_sort_vectors_and_accumulate_usage(db: Session, transport: tuple[list[dict[str, Any]], list[Reply]]) -> None:
    calls, replies = transport
    replies.extend([(200, success([str(i) for i in range(start, min(start + 4, 9))], tokens=tokens)) for start, tokens in [(0, 0), (4, 7), (8, 2)]])
    vectors, tokens = make_model(db, f"{INTL}/compatible-mode/v1").encode([str(i) for i in range(9)])
    np.testing.assert_array_equal(vectors, [[float(i), 0.0] for i in range(9)])
    assert tokens == 9
    assert [len(call["body"]["input"]["texts"]) for call in calls] == [4, 4, 1]
    assert all(call["url"] == f"{INTL}/api/v1{SERVICE}" for call in calls)
    assert not replies


@pytest.mark.parametrize("status", [408, 429, 500, 503])
def test_document_retry_keeps_endpoint_and_counts_only_success(db: Session, monkeypatch: pytest.MonkeyPatch, transport: tuple[list[dict[str, Any]], list[Reply]], status: int) -> None:
    calls, replies = transport
    waits: list[int] = []
    monkeypatch.setattr("time.sleep", waits.append)
    replies.extend([(status, {"code": "Busy", "message": "try later", "usage": {"total_tokens": 1000}}), (200, success(["4"], tokens=0))])
    vectors, tokens = make_model(db, f"{INTL}/compatible-mode/v1").encode(["4"])
    np.testing.assert_array_equal(vectors, [[4.0, 0.0]])
    assert tokens == 0
    assert waits == [10]
    assert len(calls) == 2
    assert all(call["url"] == f"{INTL}/api/v1{SERVICE}" for call in calls)


@pytest.mark.parametrize(("status", "query", "attempts"), [(401, False, 1), (400, False, 1), (503, False, 6), (429, True, 1)])
def test_failures_are_explicit_bounded_and_do_not_log_response_secrets(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
    transport: tuple[list[dict[str, Any]], list[Reply]],
    caplog: pytest.LogCaptureFixture,
    status: int,
    query: bool,
    attempts: int,
) -> None:
    calls, replies = transport
    previous = dashscope.base_http_api_url
    waits: list[int] = []
    monkeypatch.setattr("time.sleep", waits.append)
    replies.extend([(status, {"code": "ProviderFailure", "message": f"response-secret {FAKE_KEY}"})] * attempts)
    model = make_model(db, f"{INTL}/api/v1")
    with pytest.raises(ModelException, match=f"HTTP {status}") as error:
        model.encode_queries("4") if query else model.encode(["4"])
    assert len(calls) == attempts
    assert waits == [10] * (attempts - 1)
    assert dashscope.base_http_api_url == previous
    assert "response-secret" not in str(error.value) + caplog.text
    assert FAKE_KEY not in str(error.value) + caplog.text


def malformed_payloads() -> list[dict[str, Any]]:
    good = success(["1", "2"])
    payloads: list[dict[str, Any]] = [{}, {"output": None}, {"output": {"embeddings": None}}, {"output": {"embeddings": []}}]
    for entries in [
        [{"text_index": 0, "embedding": [1.0]}],
        [{"text_index": 0, "embedding": [1.0]}, {"text_index": 0, "embedding": [2.0]}],
        [{"text_index": -1, "embedding": [1.0]}, {"text_index": 0, "embedding": [2.0]}],
        [{"text_index": 0, "embedding": [1.0]}, {"text_index": 2, "embedding": [2.0]}],
        [{"text_index": True, "embedding": [1.0]}, {"text_index": 0, "embedding": [2.0]}],
        [{"text_index": "1", "embedding": [1.0]}, {"text_index": 0, "embedding": [2.0]}],
        [{"embedding": [1.0]}, {"text_index": 0, "embedding": [2.0]}],
        [{"text_index": 0, "embedding": []}, {"text_index": 1, "embedding": []}],
        [{"text_index": 0, "embedding": [1.0]}, {"text_index": 1, "embedding": [2.0, 3.0]}],
        [{"text_index": 0}, {"text_index": 1, "embedding": [2.0]}],
        [{"text_index": 0, "embedding": ["1.0"]}, {"text_index": 1, "embedding": [2.0]}],
        [{"text_index": 0, "embedding": [True]}, {"text_index": 1, "embedding": [2.0]}],
        [{"text_index": 0, "embedding": [float("nan")]}, {"text_index": 1, "embedding": [2.0]}],
        [{"text_index": 0, "embedding": [float("inf")]}, {"text_index": 1, "embedding": [2.0]}],
    ]:
        payload = copy.deepcopy(good)
        payload["output"]["embeddings"] = entries
        payloads.append(payload)
    for usage in [None, {}, {"total_tokens": None}, {"total_tokens": -1}, {"total_tokens": True}, {"total_tokens": "3"}, {"total_tokens": 0.5}, {"total_tokens": float("inf")}]:
        payload = copy.deepcopy(good)
        payload["usage"] = usage
        payloads.append(payload)
    return payloads


@pytest.mark.parametrize("payload", malformed_payloads())
def test_malformed_success_never_returns_partial_or_fabricated_vectors(db: Session, transport: tuple[list[dict[str, Any]], list[Reply]], payload: dict[str, Any]) -> None:
    calls, replies = transport
    replies.append((200, payload))
    with pytest.raises(ValueError, match="Tongyi-Qianwen"):
        make_model(db, f"{INTL}/api/v1").encode(["1", "2"])
    assert len(calls) == 1


def test_query_validates_index_vector_and_zero_usage(db: Session, transport: tuple[list[dict[str, Any]], list[Reply]]) -> None:
    _, replies = transport
    replies.append((200, success(["0"], tokens=0)))
    model = make_model(db, INTL)
    vector, tokens = model.encode_queries("0")
    np.testing.assert_array_equal(vector, [0.0, 0.0])
    assert tokens == 0
    replies.append((200, {"output": {"embeddings": [{"text_index": 1, "embedding": [2.0]}]}, "usage": {"total_tokens": 0}}))
    with pytest.raises(ValueError, match="Tongyi-Qianwen"):
        model.encode_queries("0")


def test_dimension_change_between_batches_fails(db: Session, transport: tuple[list[dict[str, Any]], list[Reply]]) -> None:
    _, replies = transport
    replies.extend([(200, success(["0", "1", "2", "3"])), (200, {"output": {"embeddings": [{"text_index": 0, "embedding": [1.0]}]}, "usage": {"total_tokens": 0}})])
    with pytest.raises(ValueError, match="Tongyi-Qianwen"):
        make_model(db, INTL).encode([str(i) for i in range(5)])


def test_empty_input_and_existing_truncation_contract(db: Session, transport: tuple[list[dict[str, Any]], list[Reply]]) -> None:
    calls, _ = transport
    model = make_model(db, INTL)
    vectors, tokens = model.encode([])
    assert vectors.size == 0
    assert tokens == 0
    assert not calls
    model.encode(["hello " * 4000])
    assert num_tokens_from_string(calls[0]["body"]["input"]["texts"][0]) == 2048
    model.encode_queries("a" * 3000)
    assert len(calls[1]["body"]["input"]["texts"][0]) == 2048
    assert embedding.QWenEmbed(FAKE_KEY).model_name == "text-embedding-v4"


def test_concurrent_instances_include_sdk_default_without_global_mutation(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    previous = dashscope.base_http_api_url
    urls = [CN, INTL, "http://localhost:8765/proxy/api/v1", None]
    models = [make_model(db, url) for url in urls]
    barrier = threading.Barrier(len(models))
    observed: list[tuple[str, str]] = []
    lock = threading.Lock()

    def send(session: requests.Session, request: requests.PreparedRequest, **kwargs: Any) -> requests.Response:
        barrier.wait(timeout=10)
        with lock:
            observed.append((request.url, dashscope.base_http_api_url))
        body = json.loads(request.body)
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps(success(body["input"]["texts"])).encode()
        return response

    monkeypatch.setattr(requests.Session, "send", send)

    def run(index: int) -> tuple[np.ndarray, int]:
        return models[index].encode_queries(str(index)) if index % 2 else models[index].encode([str(index)])

    with ThreadPoolExecutor(max_workers=len(models)) as executor:
        results = list(executor.map(run, range(len(models))))
    for index, (vectors, tokens) in enumerate(results):
        np.testing.assert_array_equal(vectors.reshape(-1), [float(index), 0.0])
        assert tokens == 0
    assert {url for url, _ in observed} == {f"{CN}/api/v1{SERVICE}", f"{INTL}/api/v1{SERVICE}", f"http://localhost:8765/proxy/api/v1{SERVICE}", f"{previous.rstrip('/')}{SERVICE}"}
    assert all(value == previous for _, value in observed)
    assert dashscope.base_http_api_url == previous


def test_transport_exception_leaves_other_instance_and_sdk_default_usable(db: Session, monkeypatch: pytest.MonkeyPatch, transport: tuple[list[dict[str, Any]], list[Reply]]) -> None:
    calls, _ = transport
    previous = dashscope.base_http_api_url
    original_send: Callable[..., requests.Response] = requests.Session.send

    def send(session: requests.Session, request: requests.PreparedRequest, **kwargs: Any) -> requests.Response:
        if request.url.startswith(INTL):
            raise requests.ConnectionError("controlled transport failure")
        return original_send(session, request, **kwargs)

    monkeypatch.setattr(requests.Session, "send", send)
    with pytest.raises(ModelException, match="Embedding provider request failed"):
        make_model(db, INTL).encode_queries("0")
    vector, tokens = make_model(db, None).encode_queries("2")
    np.testing.assert_array_equal(vector, [2.0, 0.0])
    assert tokens == 1
    assert calls[0]["url"] == f"{previous.rstrip('/')}{SERVICE}"
    assert dashscope.base_http_api_url == previous
