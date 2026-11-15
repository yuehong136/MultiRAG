"""Data plane behaviour against a controlled upstream and an in-memory store."""

import asyncio
import json
import time
from datetime import UTC, datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from api.model_gateway import errors
from api.model_gateway.app import create_app
from api.model_gateway.catalog import UpstreamTarget
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


class FakeStore:
    def __init__(self) -> None:
        self.caller = Caller(key_id="k" * 32, tenant_id="tenant-1", key_name="eval-runner", allow_all_models=False, allowed_model_ids=frozenset({"gm-chat", "gm-embed"}), tags={"purpose": "eval"})
        self.models = {
            "chat-default": ResolvedModel("gm-chat", "chat-default", "chat", UpstreamTarget(11, "VLLM", "qwen-upstream", "http://upstream.test/v1", "upstream-key")),
            "embed-default": ResolvedModel("gm-embed", "embed-default", "embedding", UpstreamTarget(12, "Xinference", "bge-upstream", "http://upstream.test/v1", "x")),
            "restricted": ResolvedModel("gm-other", "restricted", "chat", UpstreamTarget(13, "VLLM", "other", "http://upstream.test/v1", "upstream-key")),
        }
        self.records: list[RequestLogEntry] = []
        self.fail_record = False

    async def authenticate(self, authorization: str | None) -> Caller:
        if authorization != CALLER_KEY:
            raise errors.invalid_api_key()
        return self.caller

    async def visible_models(self, caller: Caller) -> list[PublicModel]:
        return [PublicModel(model.gateway_model_id, model.name, model.model_type, 1700000000) for model in self.models.values() if caller.may_use(model.gateway_model_id)]

    async def resolve_model(self, caller: Caller, name: str, model_type: str) -> ResolvedModel:
        model = self.models.get(name)
        if model is None:
            raise errors.model_not_found(name)
        if not caller.may_use(model.gateway_model_id):
            raise errors.model_not_allowed(name)
        if model.model_type != model_type:
            raise errors.invalid_request("wrong endpoint", param="model", code="model_endpoint_mismatch")
        return model

    async def record(self, entry: RequestLogEntry) -> None:
        if self.fail_record:
            raise RuntimeError("log store is down")
        self.records.append(entry)


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

    def __init__(self, handler, **settings) -> None:
        self.store = FakeStore()
        self.upstream_requests: list[httpx.Request] = []

        async def recording(request: httpx.Request) -> httpx.Response:
            self.upstream_requests.append(request)
            result = handler(request)
            return await result if asyncio.iscoroutine(result) else result

        self.app = create_app(store=self.store, transport=httpx.MockTransport(recording), config=ModelGatewayConfig(**settings))

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
    assert (entry.model, entry.gateway_model_id, entry.upstream_model_id, entry.upstream_model_name) == ("chat-default", "gm-chat", 11, "qwen-upstream")
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
        relay = Relay(client, store, ModelGatewayConfig())
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
