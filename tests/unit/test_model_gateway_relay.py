"""Data plane behaviour against a controlled upstream and an in-memory store."""

import asyncio
import dataclasses
import json
import time
from datetime import UTC, datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from api.model_gateway import errors
from api.model_gateway.app import create_app
from api.model_gateway.catalog import UpstreamTarget
from api.model_gateway.limits import Limits, MemoryLimiter, Quota
from api.model_gateway.relay import Relay
from api.model_gateway.store import Caller, PublicModel, RequestLogEntry, ResolvedModel
from api.model_gateway.usage import UNAVAILABLE, SSEUsageTap
from common.app_config import ModelGatewayConfig

CALLER_KEY = "Bearer caller-key"
AUTH = {"authorization": CALLER_KEY}
CHAT = "/v1/chat/completions"
_DELTA = b'data: {"choices":[{"index":0,"delta":{"content":"hi"}}],"usage":null}\n\n'
_USAGE_ONLY = b'data: {"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":2,"total_tokens":5}}\n\n'
_DONE = b"data: [DONE]\n\n"
_SSE = {"content-type": "text/event-stream"}


CHAT_ID = "a" * 32
EMBEDDING_ID = "b" * 32
RESTRICTED_ID = "c" * 32


class FakeStore:
    def __init__(self) -> None:
        self.caller = Caller(key_id="k" * 32, tenant_id="tenant-1", key_name="eval-runner", allow_all_models=False, allowed_model_ids=frozenset({CHAT_ID, EMBEDDING_ID}), tags={"purpose": "eval"})
        self.models = {
            "chat-default": ResolvedModel(CHAT_ID, "chat-default", "chat", UpstreamTarget(CHAT_ID, "VLLM", "qwen-upstream", "http://upstream.test/v1", "upstream-key")),
            "embed-default": ResolvedModel(EMBEDDING_ID, "embed-default", "embedding", UpstreamTarget(EMBEDDING_ID, "Xinference", "bge-upstream", "http://upstream.test/v1", "x")),
            "restricted": ResolvedModel(RESTRICTED_ID, "restricted", "chat", UpstreamTarget(RESTRICTED_ID, "VLLM", "other", "http://upstream.test/v1", "upstream-key")),
        }
        self.records: list[RequestLogEntry] = []
        self.fail_record = False
        self.used_tokens = 0

    async def authenticate(self, authorization: str | None) -> Caller:
        if authorization != CALLER_KEY:
            raise errors.invalid_api_key()
        return self.caller

    async def visible_models(self, caller: Caller) -> list[PublicModel]:
        return [PublicModel(model.name, 1700000000) for model in self.models.values() if not isinstance(model, list) and caller.may_use(model.model_id, "instance-1")]

    async def resolve_deployments(self, caller: Caller, name: str, model_type: str) -> list[ResolvedModel]:
        found = self.models.get(name)
        if isinstance(found, list):
            return list(found)
        model = found
        if model is None:
            raise errors.model_not_found(name)
        if not caller.may_use(model.model_id, "instance-1"):
            raise errors.model_not_allowed(name)
        if model.model_type != model_type:
            raise errors.invalid_request("wrong endpoint", param="model", code="model_endpoint_mismatch")
        return [model]

    async def record(self, entry: RequestLogEntry) -> None:
        if self.fail_record:
            raise RuntimeError("log store is down")
        self.records.append(entry)

    async def tokens_used_since(self, key_id: str, since: datetime) -> int:
        return self.used_tokens


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes], delay: float = 0.0) -> None:
        self._chunks, self._delay = chunks, delay

    async def __aiter__(self):
        for chunk in self._chunks:
            if self._delay:
                await asyncio.sleep(self._delay)
            yield chunk


class Gateway:
    """The data plane wired to one upstream handler; logs are read after shutdown."""

    def __init__(self, handler, *, limiter=None, **settings) -> None:
        self.store = FakeStore()
        self.upstream_requests: list[httpx.Request] = []

        async def recording(request: httpx.Request) -> httpx.Response:
            self.upstream_requests.append(request)
            result = handler(request)
            return await result if asyncio.iscoroutine(result) else result

        self.app = create_app(store=self.store, transport=httpx.MockTransport(recording), config=ModelGatewayConfig(**settings), limiter=limiter)

    def call(self, method: str, url: str, **kwargs) -> httpx.Response:
        # Leaving the context runs shutdown, which waits for stream log writes.
        with TestClient(self.app) as client:
            return client.request(method, url, **kwargs)

    def upstream_json(self) -> dict:
        return json.loads(self.upstream_requests[-1].content)

    def only_record(self) -> RequestLogEntry:
        assert len(self.store.records) == 1
        return self.store.records[0]


def _completion(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"id": "c1", "model": "qwen-upstream", "choices": [{"message": {"content": "hi"}}], "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}})


def _stream(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, headers=_SSE, stream=Chunks([_DELTA, _USAGE_ONLY, _DONE]))


def _error_code(response: httpx.Response) -> str:
    return response.json()["error"]["code"]


@pytest.mark.parametrize("headers", [{}, {"authorization": "Bearer wrong"}, {"authorization": "Basic caller-key"}])
def test_requests_without_a_valid_key_are_rejected_and_not_logged(headers):
    gateway = Gateway(_completion)

    response = gateway.call("POST", CHAT, headers=headers, json={"model": "chat-default", "messages": []})

    assert response.status_code == 401
    assert response.json()["error"] == {"message": "Incorrect API key provided.", "type": "invalid_request_error", "param": None, "code": "invalid_api_key"}
    assert response.headers["x-request-id"]
    assert gateway.upstream_requests == [] and gateway.store.records == []


def test_models_endpoint_lists_only_models_the_key_may_use():
    gateway = Gateway(_completion)

    response = gateway.call("GET", "/v1/models", headers=AUTH)

    assert response.status_code == 200
    assert response.json() == {
        "object": "list",
        "data": [
            {"id": "chat-default", "object": "model", "created": 1700000000, "owned_by": "multirag"},
            {"id": "embed-default", "object": "model", "created": 1700000000, "owned_by": "multirag"},
        ],
    }


def test_chat_request_is_forwarded_with_upstream_model_and_credential():
    gateway = Gateway(_completion)
    body = {"model": "chat-default", "messages": [{"role": "user", "content": "你好"}], "temperature": 0.2, "tools": [{"type": "function", "function": {"name": "f"}}], "user": "job-7"}

    response = gateway.call("POST", CHAT, headers=AUTH, json=body)

    upstream = gateway.upstream_requests[0]
    assert str(upstream.url) == "http://upstream.test/v1/chat/completions"
    assert upstream.headers["authorization"] == "Bearer upstream-key"
    assert gateway.upstream_json() == {**body, "model": "qwen-upstream"}
    assert response.status_code == 200
    assert response.content == _completion(upstream).content
    assert response.headers["x-request-id"] == upstream.headers["x-request-id"]


def test_completed_call_is_logged_with_reported_usage_and_no_body():
    gateway = Gateway(_completion)

    response = gateway.call("POST", CHAT, headers=AUTH, json={"model": "chat-default", "messages": [{"role": "user", "content": "secret prompt"}], "user": "job-7"})

    entry = gateway.only_record()
    assert entry.id == response.headers["x-request-id"]
    assert (entry.tenant_id, entry.key_id, entry.key_name, entry.tags) == ("tenant-1", "k" * 32, "eval-runner", {"purpose": "eval"})
    assert (entry.model, entry.upstream_model_id, entry.upstream_model_name) == ("chat-default", CHAT_ID, "qwen-upstream")
    assert (entry.endpoint, entry.stream, entry.status_code, entry.error_type, entry.end_user) == ("chat.completions", False, 200, None, "job-7")
    assert (entry.usage.prompt_tokens, entry.usage.completion_tokens, entry.usage.total_tokens, entry.usage.source) == (3, 2, 5, "reported")
    assert entry.started_at.tzinfo is not None and entry.duration_ms >= 0
    assert "secret prompt" not in repr(entry)


def test_response_without_usage_is_logged_as_unknown_not_zero():
    gateway = Gateway(lambda request: httpx.Response(200, json={"choices": []}))

    gateway.call("POST", CHAT, headers=AUTH, json={"model": "chat-default", "messages": []})

    entry = gateway.only_record()
    assert entry.usage is UNAVAILABLE and entry.usage.total_tokens is None


def test_stream_asks_upstream_for_usage_and_hides_the_extra_chunk():
    gateway = Gateway(_stream)

    response = gateway.call("POST", CHAT, headers=AUTH, json={"model": "chat-default", "messages": [], "stream": True})

    assert gateway.upstream_json()["stream_options"] == {"include_usage": True}
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.content == _DELTA + _DONE
    entry = gateway.only_record()
    assert (entry.stream, entry.status_code, entry.error_type) == (True, 200, None)
    assert (entry.usage.total_tokens, entry.usage.source) == (5, "reported")
    assert entry.first_token_ms is not None


def test_stream_keeps_the_usage_chunk_the_caller_asked_for():
    gateway = Gateway(_stream)
    body = {"model": "chat-default", "messages": [], "stream": True, "stream_options": {"include_usage": True, "continuous_usage_stats": False}}

    response = gateway.call("POST", CHAT, headers=AUTH, json=body)

    assert gateway.upstream_json()["stream_options"] == {"include_usage": True, "continuous_usage_stats": False}
    assert response.content == _DELTA + _USAGE_ONLY + _DONE
    assert gateway.only_record().usage.total_tokens == 5


def test_embeddings_are_relayed_byte_for_byte_without_placeholder_credential():
    payload = b'{"object":"list","data":[{"index":0,"embedding":"AACAPwAAAEA="}],"usage":{"prompt_tokens":4,"total_tokens":4}}'
    gateway = Gateway(lambda request: httpx.Response(200, content=payload, headers={"content-type": "application/json"}))

    response = gateway.call("POST", "/v1/embeddings", headers=AUTH, json={"model": "embed-default", "input": ["a"], "encoding_format": "base64"})

    upstream = gateway.upstream_requests[0]
    assert str(upstream.url) == "http://upstream.test/v1/embeddings"
    assert "authorization" not in upstream.headers
    assert gateway.upstream_json() == {"model": "bge-upstream", "input": ["a"], "encoding_format": "base64"}
    assert response.content == payload
    entry = gateway.only_record()
    assert (entry.endpoint, entry.usage.prompt_tokens, entry.usage.completion_tokens, entry.usage.total_tokens) == ("embeddings", 4, None, 4)


@pytest.mark.parametrize(
    ("body", "status", "code"),
    [
        ({"model": "missing", "messages": []}, 404, "model_not_found"),
        ({"model": "restricted", "messages": []}, 403, "model_not_allowed"),
        ({"model": "embed-default", "messages": []}, 400, "model_endpoint_mismatch"),
        ({"messages": []}, 400, "missing_model"),
        ({"model": "  ", "messages": []}, 400, "missing_model"),
    ],
)
def test_rejected_requests_never_reach_upstream_and_are_logged(body, status, code):
    gateway = Gateway(_completion)

    response = gateway.call("POST", CHAT, headers=AUTH, json=body)

    assert (response.status_code, _error_code(response)) == (status, code)
    assert gateway.upstream_requests == []
    entry = gateway.only_record()
    assert (entry.status_code, entry.error_code) == (status, code)
    assert entry.usage is UNAVAILABLE


@pytest.mark.parametrize("content", [b"{not json", b"[1, 2]", b'"text"'])
def test_non_object_bodies_are_invalid_json(content):
    gateway = Gateway(_completion)

    response = gateway.call("POST", CHAT, headers={**AUTH, "content-type": "application/json"}, content=content)

    assert (response.status_code, _error_code(response)) == (400, "invalid_json")


def test_oversized_body_is_refused_before_forwarding():
    gateway = Gateway(_completion, max_request_bytes=64)

    response = gateway.call("POST", CHAT, headers=AUTH, json={"model": "chat-default", "messages": [{"role": "user", "content": "x" * 200}]})

    assert (response.status_code, _error_code(response)) == (413, "request_too_large")
    assert gateway.upstream_requests == []


@pytest.mark.parametrize("upstream_status", [401, 403])
def test_upstream_credential_failure_is_not_blamed_on_the_caller(upstream_status):
    gateway = Gateway(lambda request: httpx.Response(upstream_status, json={"error": {"message": "bad key", "code": "invalid_api_key"}}))

    response = gateway.call("POST", CHAT, headers=AUTH, json={"model": "chat-default", "messages": []})

    assert (response.status_code, _error_code(response)) == (502, "upstream_auth_error")
    assert "bad key" not in response.text
    assert gateway.only_record().error_code == "upstream_auth_error"


def test_upstream_client_error_is_relayed_as_is():
    upstream_body = {"error": {"message": "max_tokens is too large", "type": "invalid_request_error", "param": "max_tokens", "code": "context_length_exceeded"}}
    gateway = Gateway(lambda request: httpx.Response(400, json=upstream_body))

    response = gateway.call("POST", CHAT, headers=AUTH, json={"model": "chat-default", "messages": []})

    assert response.status_code == 400 and response.json() == upstream_body
    entry = gateway.only_record()
    assert (entry.status_code, entry.error_type, entry.error_code) == (400, "upstream_rejected", "context_length_exceeded")


def test_upstream_throttling_keeps_its_retry_hint():
    gateway = Gateway(lambda request: httpx.Response(429, json={"error": {"message": "busy"}}, headers={"retry-after": "3"}))

    response = gateway.call("POST", CHAT, headers=AUTH, json={"model": "chat-default", "messages": []})

    assert response.status_code == 429 and response.headers["retry-after"] == "3"


def test_upstream_server_error_becomes_bad_gateway():
    gateway = Gateway(lambda request: httpx.Response(500, text="Traceback (most recent call last): ..."))

    response = gateway.call("POST", CHAT, headers=AUTH, json={"model": "chat-default", "messages": []})

    assert (response.status_code, _error_code(response)) == (502, "upstream_error")
    assert "Traceback" not in response.text


def test_unreachable_upstream_becomes_bad_gateway():
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    gateway = Gateway(refuse)

    response = gateway.call("POST", CHAT, headers=AUTH, json={"model": "chat-default", "messages": []})

    assert (response.status_code, _error_code(response)) == (502, "upstream_unreachable")
    assert gateway.only_record().status_code == 502


def test_slow_response_head_becomes_gateway_timeout():
    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(1)
        return _completion(request)

    gateway = Gateway(slow, first_response_timeout_seconds=0.05)

    response = gateway.call("POST", CHAT, headers=AUTH, json={"model": "chat-default", "messages": []})

    assert (response.status_code, _error_code(response)) == (504, "upstream_timeout")
    assert gateway.only_record().error_code == "upstream_timeout"


def test_stream_exceeding_the_total_time_ends_with_an_error_event():
    gateway = Gateway(lambda request: httpx.Response(200, headers=_SSE, stream=Chunks([_DELTA, _DELTA, _DELTA, _DONE], delay=0.1)), total_timeout_seconds=0.15)

    response = gateway.call("POST", CHAT, headers=AUTH, json={"model": "chat-default", "messages": [], "stream": True})

    events = [event for event in response.content.split(b"\n\n") if event]
    assert json.loads(events[-1][6:])["error"]["code"] == "upstream_timeout"
    assert _DONE.strip() not in events
    entry = gateway.only_record()
    assert (entry.status_code, entry.error_type, entry.error_code) == (200, "upstream_error", "upstream_timeout")
    assert entry.usage is UNAVAILABLE


def test_failed_log_write_does_not_change_the_response():
    gateway = Gateway(_completion)
    gateway.store.fail_record = True

    response = gateway.call("POST", CHAT, headers=AUTH, json={"model": "chat-default", "messages": []})

    assert response.status_code == 200 and response.json()["id"] == "c1"


def test_unknown_urls_and_methods_use_the_error_envelope():
    gateway = Gateway(_completion)

    assert _error_code(gateway.call("GET", "/v1/unknown", headers=AUTH)) == "not_found"
    assert _error_code(gateway.call("GET", CHAT, headers=AUTH)) == "method_not_allowed"


def test_caller_cannot_redirect_the_request():
    gateway = Gateway(_completion)
    body = {"model": "chat-default", "messages": [], "base_url": "http://attacker.test", "api_key": "stolen", "provider": "other"}

    gateway.call("POST", CHAT, headers={**AUTH, "x-upstream-url": "http://attacker.test"}, json=body)

    upstream = gateway.upstream_requests[0]
    assert upstream.url.host == "upstream.test"
    assert upstream.headers["authorization"] == "Bearer upstream-key"
    assert "x-upstream-url" not in upstream.headers


async def test_client_disconnect_mid_stream_is_logged_as_closed():
    store = FakeStore()
    transport = httpx.MockTransport(lambda request: httpx.Response(200, headers=_SSE, stream=Chunks([_DELTA, _DELTA, _USAGE_ONLY, _DONE])))
    async with httpx.AsyncClient(transport=transport) as client:
        relay = Relay(client, store, ModelGatewayConfig(), MemoryLimiter())
        response = await client.send(client.build_request("POST", "http://upstream.test/v1/chat/completions"), stream=True)
        entry = RequestLogEntry(id="r" * 32, tenant_id="tenant-1", key_id="k" * 32, key_name="eval-runner", endpoint="chat.completions", started_at=datetime.now(UTC), stream=True)
        started = time.monotonic()
        stream = relay._stream(response, SSEUsageTap(hide_usage_only_chunk=True), entry, started, started + 60)

        assert await anext(stream) == _DELTA
        await stream.aclose()
        await relay.drain()

    assert response.is_closed
    assert (entry.status_code, entry.error_type) == (499, "client_closed")
    assert entry.usage is UNAVAILABLE
    assert store.records == [entry]


def _frozen_limiter(store: FakeStore | None = None) -> MemoryLimiter:
    """A limiter whose clock stays inside one minute, so windows never roll mid-test."""
    return MemoryLimiter(store.tokens_used_since if store else None, clock=lambda: datetime(2026, 11, 15, 10, 0, 5, tzinfo=UTC))


def _limited(handler, **key_changes) -> Gateway:
    gateway = Gateway(handler, limiter=_frozen_limiter())
    gateway.store.caller = dataclasses.replace(gateway.store.caller, **key_changes)
    return gateway


_CHAT_BODY = {"model": "chat-default", "messages": []}


def test_request_rate_limit_refuses_without_calling_upstream_and_is_logged():
    gateway = _limited(_completion, limits=Limits(rpm=1))
    assert gateway.call("POST", CHAT, headers=AUTH, json=_CHAT_BODY).status_code == 200

    response = gateway.call("POST", CHAT, headers=AUTH, json=_CHAT_BODY)

    assert response.status_code == 429
    assert response.json()["error"] == {"message": "Rate limit reached for this API key: 1 requests per minute.", "type": "requests", "param": None, "code": "rate_limit_exceeded"}
    assert (response.headers["retry-after"], response.headers["x-ratelimit-limit-requests"], response.headers["x-ratelimit-remaining-requests"]) == ("55", "1", "0")
    assert response.headers["x-request-id"]
    assert len(gateway.upstream_requests) == 1
    assert [(record.status_code, record.error_code) for record in gateway.store.records] == [(200, None), (429, "rate_limit_exceeded")]


@pytest.mark.parametrize("stream", [False, True])
def test_token_rate_limit_counts_usage_reported_when_the_call_ends(stream):
    gateway = _limited(_stream if stream else _completion, limits=Limits(tpm=5))
    body = {**_CHAT_BODY, "stream": stream}
    # The upstream reports 5 tokens: the first call is admitted and uses the whole minute's budget.
    assert gateway.call("POST", CHAT, headers=AUTH, json=body).status_code == 200

    response = gateway.call("POST", CHAT, headers=AUTH, json=body)

    assert (response.status_code, response.json()["error"]["type"], _error_code(response)) == (429, "tokens", "rate_limit_exceeded")
    assert response.headers["x-ratelimit-limit-tokens"] == "5"
    assert len(gateway.upstream_requests) == 1


def _server_error(request: httpx.Request) -> httpx.Response:
    return httpx.Response(500, text="boom")


def _refuse_connection(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused", request=request)


@pytest.mark.parametrize("handler", [_completion, _stream, _server_error, _refuse_connection], ids=["completed", "stream", "upstream-error", "unreachable"])
def test_concurrency_slot_is_released_however_the_call_ends(handler):
    gateway = _limited(handler, limits=Limits(max_parallel=1))

    for _ in range(3):
        gateway.call("POST", CHAT, headers=AUTH, json={**_CHAT_BODY, "stream": handler is _stream})

    assert len(gateway.upstream_requests) == 3
    assert all(record.error_code != "concurrency_limit_exceeded" for record in gateway.store.records)


def test_model_limit_applies_to_every_key_of_that_model():
    gateway = Gateway(_completion, limiter=_frozen_limiter())
    gateway.store.models["chat-default"] = dataclasses.replace(gateway.store.models["chat-default"], limits=Limits(rpm=1))
    assert gateway.call("POST", CHAT, headers=AUTH, json=_CHAT_BODY).status_code == 200

    response = gateway.call("POST", CHAT, headers=AUTH, json=_CHAT_BODY)

    assert response.status_code == 429
    assert response.json()["error"]["message"] == "Rate limit reached for the model 'chat-default': 1 requests per minute."
    # Another model of the same key is unaffected.
    assert gateway.call("POST", "/v1/embeddings", headers=AUTH, json={"model": "embed-default", "input": "a"}).status_code == 200


def test_spent_quota_refuses_with_no_retry_hint():
    gateway = Gateway(_completion)
    gateway.store.used_tokens = 100
    gateway.store.caller = dataclasses.replace(gateway.store.caller, quota=Quota(100, "day"))

    response = gateway.call("POST", CHAT, headers=AUTH, json=_CHAT_BODY)

    assert (response.status_code, response.json()["error"]["type"], _error_code(response)) == (429, "insufficient_quota", "insufficient_quota")
    assert "retry-after" not in response.headers
    assert gateway.upstream_requests == []
    assert gateway.only_record().error_code == "insufficient_quota"


def test_quota_counts_tokens_of_calls_made_through_this_process():
    gateway = Gateway(_completion)
    gateway.store.caller = dataclasses.replace(gateway.store.caller, quota=Quota(8, "day"))

    statuses = [gateway.call("POST", CHAT, headers=AUTH, json=_CHAT_BODY).status_code for _ in range(3)]

    # 5 tokens per call against a quota of 8: the second call crosses it, the third is refused.
    assert statuses == [200, 200, 429]


def test_requests_without_limits_never_touch_the_limiter():
    class Untouchable:
        async def admit(self, request_id, subjects) -> None:
            raise AssertionError("limiter must not be consulted")

        async def settle(self, request_id, subjects, tokens) -> None:
            raise AssertionError("limiter must not be consulted")

    gateway = Gateway(_completion, limiter=Untouchable())

    assert gateway.call("POST", CHAT, headers=AUTH, json=_CHAT_BODY).status_code == 200
    assert gateway.call("POST", CHAT, headers=AUTH, json={"model": "missing", "messages": []}).status_code == 404


def test_failing_limiter_settlement_does_not_change_the_response():
    class BrokenSettle(MemoryLimiter):
        async def settle(self, request_id, subjects, tokens) -> None:
            raise RuntimeError("settlement failed")

    gateway = Gateway(_completion, limiter=BrokenSettle())
    gateway.store.caller = dataclasses.replace(gateway.store.caller, limits=Limits(rpm=10))

    assert gateway.call("POST", CHAT, headers=AUTH, json=_CHAT_BODY).status_code == 200
    assert gateway.only_record().status_code == 200


async def _streamed_entry(chunks: list[bytes], take: int) -> tuple[RequestLogEntry, httpx.Response]:
    """Drive the stream generator, take ``take`` chunks, then hang up like a real client."""
    store = FakeStore()
    transport = httpx.MockTransport(lambda request: httpx.Response(200, headers=_SSE, stream=Chunks(chunks)))
    async with httpx.AsyncClient(transport=transport) as client:
        relay = Relay(client, store, ModelGatewayConfig(), MemoryLimiter())
        response = await client.send(client.build_request("POST", "http://upstream.test/v1/chat/completions"), stream=True)
        entry = RequestLogEntry(id="r" * 32, tenant_id="tenant-1", key_id="k" * 32, key_name="eval-runner", endpoint="chat.completions", started_at=datetime.now(UTC), stream=True)
        started = time.monotonic()
        stream = relay._stream(response, SSEUsageTap(hide_usage_only_chunk=True), entry, started, started + 60)
        for _ in range(take):
            await anext(stream)
        await stream.aclose()
        await relay.drain()
    assert store.records == [entry]
    return entry, response


async def test_caller_hanging_up_right_after_done_is_a_completed_call():
    # SDKs close the connection as soon as they read [DONE]; the upstream here
    # would keep sending, as some servers hold the connection open.
    entry, response = await _streamed_entry([_DELTA, _USAGE_ONLY, _DONE, b": keep-alive\n\n"], take=2)

    assert (entry.status_code, entry.error_type, entry.error_code) == (200, None, None)
    assert (entry.usage.total_tokens, entry.usage.source) == (5, "reported")
    assert response.is_closed


async def test_stream_is_not_read_past_its_terminator():
    served: list[bytes] = []

    class Counting(Chunks):
        async def __aiter__(self):
            async for chunk in super().__aiter__():
                served.append(chunk)
                yield chunk

    store = FakeStore()
    transport = httpx.MockTransport(lambda request: httpx.Response(200, headers=_SSE, stream=Counting([_DELTA, _DONE, _DELTA, _DELTA])))
    async with httpx.AsyncClient(transport=transport) as client:
        relay = Relay(client, store, ModelGatewayConfig(), MemoryLimiter())
        response = await client.send(client.build_request("POST", "http://upstream.test/v1/chat/completions"), stream=True)
        entry = RequestLogEntry(id="r" * 32, tenant_id="tenant-1", key_id="k" * 32, key_name="eval-runner", endpoint="chat.completions", started_at=datetime.now(UTC), stream=True)
        started = time.monotonic()
        forwarded = [chunk async for chunk in relay._stream(response, SSEUsageTap(hide_usage_only_chunk=True), entry, started, started + 60)]
        await relay.drain()

    assert forwarded == [_DELTA, _DONE]
    assert served == [_DELTA, _DONE]
    assert (entry.status_code, entry.error_type) == (200, None)


# --- several deployments of one name ----------------------------------------

BACKUP_ID = "d" * 32
THIRD_ID = "e" * 32


def _deployment(model_id: str, host: str, upstream_name: str = "qwen-upstream", **extra) -> ResolvedModel:
    return ResolvedModel(model_id, "chat-default", "chat", UpstreamTarget(model_id, "VLLM", upstream_name, f"http://{host}/v1", "upstream-key"), **extra)


def _pooled(responses: dict[str, object], *, hosts: tuple[str, ...] = ("primary.test", "backup.test"), limiter=None, **settings) -> Gateway:
    """A gateway whose ``chat-default`` has one deployment per host, tried in the order given."""

    def handler(request: httpx.Request) -> httpx.Response:
        answer = responses[request.url.host]
        if isinstance(answer, Exception):
            raise answer
        return answer(request) if callable(answer) else httpx.Response(answer, json={"error": {"message": "upstream says no", "code": "nope"}})

    gateway = Gateway(handler, limiter=limiter, **settings)
    ids = (CHAT_ID, BACKUP_ID, THIRD_ID)
    gateway.store.caller = dataclasses.replace(gateway.store.caller, allowed_model_ids=frozenset(ids))
    gateway.store.models["chat-default"] = [_deployment(ids[index], host, order=index + 1) for index, host in enumerate(hosts)]
    return gateway


def _chat(gateway: Gateway, **body) -> httpx.Response:
    return gateway.call("POST", CHAT, headers=AUTH, json={"model": "chat-default", "messages": [], **body})


def _hosts(gateway: Gateway) -> list[str]:
    return [request.url.host for request in gateway.upstream_requests]


@pytest.mark.parametrize("failure", [500, 503, 429, 401, 403, 404, 408, httpx.ConnectError("refused")])
def test_a_failed_deployment_hands_the_call_to_the_next_one(failure):
    gateway = _pooled({"primary.test": failure, "backup.test": _completion})

    response = _chat(gateway)

    assert response.status_code == 200 and response.json()["id"] == "c1"
    assert _hosts(gateway) == ["primary.test", "backup.test"]
    record = gateway.only_record()
    # The call is logged, priced and counted under the deployment that answered.
    assert (record.status_code, record.error_type, record.upstream_model_id) == (200, None, BACKUP_ID)
    assert [(attempt.model_id, attempt.status_code < 400) for attempt in record.attempts] == [(CHAT_ID, False), (BACKUP_ID, True)]
    assert record.usage.total_tokens == 5


def test_a_request_the_upstream_rejects_is_not_tried_elsewhere():
    gateway = _pooled({"primary.test": 400, "backup.test": _completion})

    response = _chat(gateway)

    assert response.status_code == 400 and response.json()["error"]["message"] == "upstream says no"
    assert _hosts(gateway) == ["primary.test"]
    assert gateway.only_record().error_type == "upstream_rejected"


def test_when_every_deployment_fails_the_last_answer_is_the_answer():
    gateway = _pooled({"primary.test": 500, "backup.test": 429})

    response = _chat(gateway)

    assert response.status_code == 429 and response.json()["error"]["message"] == "upstream says no"
    record = gateway.only_record()
    assert [attempt.status_code for attempt in record.attempts] == [500, 429]
    assert record.upstream_model_id == BACKUP_ID


def test_a_request_calls_no_more_deployments_than_its_retries_allow():
    hosts = ("primary.test", "backup.test", "third.test")
    answers = {"primary.test": 500, "backup.test": 500, "third.test": _completion}

    assert _chat(_pooled(answers, hosts=hosts)).status_code == 200
    limited = _pooled(answers, hosts=hosts, num_retries=1)
    assert _chat(limited).status_code == 502
    assert _hosts(limited) == ["primary.test", "backup.test"]
    single = _pooled(answers, hosts=hosts, num_retries=0)
    assert _chat(single).status_code == 502
    assert _hosts(single) == ["primary.test"]


def test_a_stream_fails_over_before_anything_was_sent():
    gateway = _pooled({"primary.test": 503, "backup.test": _stream})

    response = _chat(gateway, stream=True)

    assert response.status_code == 200 and response.text.endswith("data: [DONE]\n\n")
    assert '"error"' not in response.text
    record = gateway.only_record()
    assert (record.upstream_model_id, record.usage.total_tokens, len(record.attempts)) == (BACKUP_ID, 5, 2)


def test_each_deployment_is_called_by_its_own_upstream_name_and_address():
    gateway = _pooled({"primary.test": 500, "backup.test": _completion})
    gateway.store.models["chat-default"][1] = _deployment(BACKUP_ID, "backup.test", "qwen-on-backup", order=2)

    _chat(gateway)

    assert [json.loads(request.content)["model"] for request in gateway.upstream_requests] == ["qwen-upstream", "qwen-on-backup"]
    assert gateway.only_record().upstream_model_name == "qwen-on-backup"


def test_a_rate_limited_deployment_rests_and_later_calls_skip_it():
    gateway = _pooled({"primary.test": 429, "backup.test": _completion})

    with TestClient(gateway.app) as client:
        for _ in range(3):
            assert client.post(CHAT, headers=AUTH, json={"model": "chat-default", "messages": []}).status_code == 200

    # Only the first call met the resting deployment.
    assert _hosts(gateway) == ["primary.test", "backup.test", "backup.test", "backup.test"]
    assert [len(record.attempts) for record in gateway.store.records] == [2, 1, 1]


def test_a_deployment_at_its_own_limit_is_passed_over_without_a_call():
    gateway = _pooled({"primary.test": _completion, "backup.test": _completion}, limiter=_frozen_limiter())
    gateway.store.models["chat-default"][0] = _deployment(CHAT_ID, "primary.test", order=1, limits=Limits(rpm=1))

    with TestClient(gateway.app) as client:
        for _ in range(2):
            assert client.post(CHAT, headers=AUTH, json={"model": "chat-default", "messages": []}).status_code == 200

    assert _hosts(gateway) == ["primary.test", "backup.test"]
    second = gateway.store.records[1]
    assert [(attempt.model_id, attempt.status_code, attempt.error_code) for attempt in second.attempts] == [(CHAT_ID, 429, "rate_limit_exceeded"), (BACKUP_ID, 200, None)]


def test_a_failed_deployment_gives_its_concurrency_slot_back():
    gateway = _pooled({"primary.test": 500, "backup.test": _completion}, limiter=_frozen_limiter(), cooldown_time_seconds=0)
    gateway.store.models["chat-default"][0] = _deployment(CHAT_ID, "primary.test", order=1, limits=Limits(max_parallel=1))

    with TestClient(gateway.app) as client:
        for _ in range(3):
            assert client.post(CHAT, headers=AUTH, json={"model": "chat-default", "messages": []}).status_code == 200

    # The slot was free again each time, so the first deployment was tried on every call.
    assert _hosts(gateway) == ["primary.test", "backup.test"] * 3
