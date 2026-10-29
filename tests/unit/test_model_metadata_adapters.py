"""Metadata declarations use callable identities and explicit protocol failures."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from api.db.model_contract import DiscoveryInput, InstanceInput
from api.db.services import model_provider_service
from common import settings
from core.llm import model_meta
from core.llm.model_meta import VLLM, Base, DiscoveryError, LMStudio, LocalAI, Ollama, OpenAIAPICompatible, OpenRouter, Xinference

ADAPTERS = [OpenAIAPICompatible, VLLM, LMStudio, Xinference, LocalAI, OpenRouter]


def transport(monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], Awaitable[httpx.Response] | httpx.Response]) -> list[httpx.AsyncClient]:
    clients: list[httpx.AsyncClient] = []

    def client(_self: Base) -> httpx.AsyncClient:
        result = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False, trust_env=False)
        clients.append(result)
        return result

    monkeypatch.setattr(Base, "_client", client)
    return clients


@pytest.mark.parametrize("adapter", [OpenAIAPICompatible, VLLM, LMStudio])
@pytest.mark.parametrize("base", ["https://fixture.invalid/mount", "https://fixture.invalid/mount/v1/"])
async def test_compatible_names_capabilities_context_and_key_envelope(monkeypatch: pytest.MonkeyPatch, adapter: type[OpenAIAPICompatible], base: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/mount/v1/models" and request.method == "GET"
        assert request.headers["Authorization"] == "Bearer fixture-token" and not request.url.query
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "BAAI/bge-reranker", "max_tokens": 0},
                    {"id": "BAAI/bge-m3", "context_length": 4096},
                    {"id": "qwen-vl", "max_model_len": 32768},
                    {"id": "whisper-asr"},
                    {"id": "voice-tts"},
                ]
            },
        )

    clients = transport(monkeypatch, handler)
    result = await adapter('{"api_key":"fixture-token","is_tools":false}', base).get_model_list()
    assert {item.name for item in result} == ({"BAAI/bge-m3", "qwen-vl"} if adapter is LMStudio else {"BAAI/bge-reranker", "BAAI/bge-m3", "qwen-vl"})
    by_name = {item.name: item for item in result}
    assert by_name["BAAI/bge-m3"].model_types == ["embedding"] and by_name["BAAI/bge-m3"].max_tokens == 4096
    assert by_name["qwen-vl"].model_types == ["chat", "image2text"] and by_name["qwen-vl"].max_tokens == 32768
    if adapter is not LMStudio:
        assert by_name["BAAI/bge-reranker"].model_types == ["rerank"] and by_name["BAAI/bge-reranker"].max_tokens == 0
    assert all(item.features == [] for item in result) and all(client.is_closed for client in clients)


async def test_xinference_explicit_types_abilities_and_zero_context(monkeypatch: pytest.MonkeyPatch) -> None:
    transport(
        monkeypatch,
        lambda _request: httpx.Response(
            200,
            json={
                "data": [
                    {"id": "multimodal", "model_type": "LLM", "model_ability": ["chat", "vision", "tools", "reasoning"], "context_length": 0},
                    *[{"id": kind, "model_type": kind} for kind in ("embedding", "rerank", "image", "video")],
                    {"id": "voice", "model_type": "audio", "model_ability": ["text2audio", "audio2text"]},
                ]
            },
        ),
    )
    result = await Xinference("", "http://127.0.0.1:1234/v1").get_model_list()
    assert result[0].model_types == ["chat", "image2text"] and result[0].max_tokens == 0 and result[0].features == ["is_tools", "thinking"]
    assert [item.model_types for item in result[1:]] == [["embedding"], ["rerank"], ["tts", "speech2text"]]


async def test_xinference_excludes_generation_and_unknown_audio_abilities(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [
        {"id": "stable-diffusion", "model_type": "image", "model_ability": None, "context_length": "not-text"},
        {"id": "video", "model_type": "video", "model_ability": None},
        {"id": "text-generation", "model_type": "LLM", "model_ability": ["generate"]},
        {"id": "unknown-audio", "model_type": "audio", "model_ability": None},
        {"id": "whisper", "model_type": "audio", "model_ability": ["audio2text"]},
        {"id": "ChatTTS", "model_type": "audio", "model_ability": ["text2audio", "text2audio_zero_shot"]},
    ]
    transport(monkeypatch, lambda _request: httpx.Response(200, json={"data": rows}))
    result = await Xinference("", "http://127.0.0.1").get_model_list()
    assert [(item.name, item.model_types) for item in result] == [("whisper", ["speech2text"]), ("ChatTTS", ["tts"])]


def router_model(**values: Any) -> dict[str, Any]:
    return {
        "id": "vendor/model",
        "architecture": {"input_modalities": ["text", "image", "audio"], "output_modalities": ["text", "audio"]},
        "supported_parameters": ["tools", "reasoning"],
        "top_provider": {"max_completion_tokens": 0},
        "context_length": 8192,
        **values,
    }


@pytest.mark.parametrize("base", ["", "https://openrouter.ai", "https://openrouter.ai/v1", "https://openrouter.ai/api/v1/"])
async def test_openrouter_official_path_features_zero_and_supported_modalities(monkeypatch: pytest.MonkeyPatch, base: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://openrouter.ai/api/v1/models?output_modalities=all"
        assert request.headers["Authorization"] == "Bearer fixture-token"
        return httpx.Response(200, json={"data": [router_model(), router_model(id="unsupported-embed", architecture={"input_modalities": ["text"], "output_modalities": ["embeddings"]})]})

    clients = transport(monkeypatch, handler)
    result = await OpenRouter('{"api_key":"fixture-token","provider_order":["fixture"]}', base).get_model_list()
    assert len(result) == 1 and result[0].model_types == ["chat", "image2text"] and result[0].max_tokens == 0
    assert result[0].features == ["is_tools", "thinking"] and all(client.is_closed for client in clients)


@pytest.mark.parametrize("inputs,expected", [(["audio"], []), (["image"], ["image2text"]), ([], []), (["text"], ["chat"]), (["text", "image"], ["chat", "image2text"])])
async def test_openrouter_text_output_requires_corresponding_supported_inputs(monkeypatch: pytest.MonkeyPatch, inputs: list[str], expected: list[str]) -> None:
    model = router_model(architecture={"input_modalities": inputs, "output_modalities": ["text"]})
    clients = transport(monkeypatch, lambda _request: httpx.Response(200, json={"data": [model]}))
    result = await OpenRouter("").get_model_list()
    assert [item.model_types for item in result] == ([expected] if expected else [])
    assert all(client.is_closed for client in clients)


@pytest.mark.parametrize("base", ["http://openrouter.ai", "https://openrouter.ai.evil.invalid", "https://openrouter.ai:444", "https://openrouter.ai/other"])
async def test_openrouter_rejects_other_origins_before_credentials(monkeypatch: pytest.MonkeyPatch, base: str) -> None:
    clients = transport(monkeypatch, lambda _request: pytest.fail("must not send credentials"))
    with pytest.raises(ValueError, match="official HTTPS"):
        await OpenRouter("fixture-token", base).get_model_list()
    assert clients == []


@pytest.mark.parametrize("tag,callable_name", [("model:latest", "model"), ("model:latest", "model:latest"), ("model:custom", "model:custom"), ("host:5000/model:latest", "host:5000/model")])
async def test_localai_joins_tags_to_real_callable_ids(monkeypatch: pytest.MonkeyPatch, tag: str, callable_name: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer fixture-token"
        if request.url.path == "/mount/api/tags":
            return httpx.Response(200, json={"models": [{"name": tag}]})
        if request.url.path == "/mount/v1/models":
            return httpx.Response(200, json={"data": [{"id": callable_name}]})
        assert request.method == "POST" and request.url.path == "/mount/api/show"
        assert json.loads(request.content) == {"model": tag}
        return httpx.Response(200, json={"capabilities": ["completion", "vision", "embedding", "tools", "thinking"], "model_info": {"general.context_length": 0}})

    clients = transport(monkeypatch, handler)
    result = await LocalAI('{"api_key":"fixture-token"}', "https://fixture.invalid/mount/v1").get_model_list()
    assert result[0].name == callable_name and result[0].max_tokens == 0
    assert result[0].model_types == ["chat", "image2text", "embedding"] and result[0].features == ["is_tools", "thinking"]
    assert all(client.is_closed for client in clients)


@pytest.mark.parametrize("tags,ids", [(["same", "same:latest"], ["same"]), (["model:custom"], ["model"]), (["model:latest"], ["other"]), (["model"], ["model", "extra"])])
async def test_localai_rejects_missing_or_colliding_callable_identity(monkeypatch: pytest.MonkeyPatch, tags: list[str], ids: list[str]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return httpx.Response(200, json={"models": [{"name": name} for name in tags]} if request.url.path.endswith("/tags") else {"data": [{"id": name} for name in ids]})

    transport(monkeypatch, handler)
    with pytest.raises(DiscoveryError):
        await LocalAI("", "http://127.0.0.1").get_model_list()


@pytest.mark.parametrize("adapter", ADAPTERS)
@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"data": None},
        {"data": [None]},
        {"data": [{"id": "same"}, {"id": "same"}]},
        {"data": [{"id": " bad "}]},
        {"data": [{"id": str(index)} for index in range(2001)]},
        {"data": [], "has_more": True},
        {"data": [], "next": "/secret"},
        {"data": [], "total": 1},
    ],
)
async def test_invalid_or_partial_inventories_fail(monkeypatch: pytest.MonkeyPatch, adapter: type[Base], payload: dict[str, Any]) -> None:
    clients = transport(monkeypatch, lambda _request: httpx.Response(200, json=payload))
    with pytest.raises(DiscoveryError):
        await adapter("", "" if adapter is OpenRouter else "http://127.0.0.1").get_model_list()
    assert all(client.is_closed for client in clients)


@pytest.mark.parametrize("adapter", ADAPTERS)
@pytest.mark.parametrize("status", [401, 500, 302, 307])
async def test_remote_failures_and_redirects_are_redacted(monkeypatch: pytest.MonkeyPatch, adapter: type[Base], status: int) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json={"secret": "fixture-token"}, headers={"Location": "https://secret.invalid"})

    clients = transport(monkeypatch, handler)
    with pytest.raises(DiscoveryError) as caught:
        await adapter("fixture-token", "" if adapter is OpenRouter else "https://fixture.invalid").get_model_list()
    assert len(seen) == 1 and "fixture-token" not in str(caught.value) and "secret" not in str(caught.value)
    assert all(client.is_closed for client in clients)


@pytest.mark.parametrize("adapter", ADAPTERS)
@pytest.mark.parametrize("key", [" padded ", "{}", "[]", '"token"', "{", '{"api_key":12}', '{"api_key":null}', '{"api_key":"secret","unknown":true}', '{"api_key":"secret","is_tools":1}'])
async def test_invalid_credentials_fail_before_network(monkeypatch: pytest.MonkeyPatch, adapter: type[Base], key: str) -> None:
    clients = transport(monkeypatch, lambda _request: pytest.fail("must not send invalid credential"))
    with pytest.raises(ValueError, match="credential"):
        await adapter(key, "" if adapter is OpenRouter else "https://fixture.invalid").get_model_list()
    assert clients == []


@pytest.mark.parametrize("adapter", [OpenAIAPICompatible, VLLM, LMStudio, Xinference, LocalAI])
async def test_remote_http_credentials_fail_before_network(monkeypatch: pytest.MonkeyPatch, adapter: type[Base]) -> None:
    clients = transport(monkeypatch, lambda _request: pytest.fail("must not send credential"))
    with pytest.raises(ValueError, match="HTTPS or a literal loopback"):
        await adapter("secret", "http://localhost").get_model_list()
    assert clients == []


@pytest.mark.parametrize("adapter", ADAPTERS)
@pytest.mark.parametrize("cancel", [False, True])
async def test_timeout_and_cancellation_close_clients(monkeypatch: pytest.MonkeyPatch, adapter: type[Base], cancel: bool) -> None:
    started = asyncio.Event()
    finished: list[bool] = []

    async def handler(_request: httpx.Request) -> httpx.Response:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            finished.append(True)
        raise AssertionError("unreachable")

    clients = transport(monkeypatch, handler)
    monkeypatch.setattr(model_meta, "DISCOVERY_SECONDS", 2 if cancel else 0.01)
    task = asyncio.create_task(adapter("", "" if adapter is OpenRouter else "http://127.0.0.1").get_model_list())
    await asyncio.wait_for(started.wait(), 1)
    if cancel:
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else DiscoveryError):
        await task
    assert finished == [True] and all(client.is_closed for client in clients)


@pytest.mark.parametrize("value", [True, -1, "0", 1.5, 2147483648])
@pytest.mark.parametrize("adapter", [OpenAIAPICompatible, Xinference, OpenRouter])
async def test_context_type_and_bounds(monkeypatch: pytest.MonkeyPatch, adapter: type[OpenAIAPICompatible], value: Any) -> None:
    model = router_model(top_provider={"max_completion_tokens": value}) if adapter is OpenRouter else {"id": "model", "context_length": value}
    transport(monkeypatch, lambda _request: httpx.Response(200, json={"data": [model]}))
    with pytest.raises(DiscoveryError):
        await adapter("", "" if adapter is OpenRouter else "http://127.0.0.1").get_model_list()


async def test_existing_ollama_structured_key_resolves_without_auth_downgrade(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer secret"
        return httpx.Response(200, json={"models": []})

    transport(monkeypatch, handler)
    assert await Ollama('{"api_key":"secret"}', "http://127.0.0.1").get_model_list() == []
    with pytest.raises(ValueError, match="credential"):
        await Ollama('{"is_tools":false}', "http://127.0.0.1").get_model_list()


async def test_service_preserves_static_baidu_and_remote_descriptors_without_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    root = Path(__file__).resolve().parents[2]
    factories = json.loads((root / "configs/llm_factories.json").read_text())["factory_llm_infos"]
    monkeypatch.setattr(settings, "FACTORY_LLM_INFOS", factories)
    clients = transport(monkeypatch, lambda _request: httpx.Response(200, json={"data": [{"id": "fixture-vl", "max_tokens": 0}]}))
    result = await model_provider_service.discover_models("VLLM", DiscoveryInput(base_url="http://127.0.0.1"))
    assert result[0].model_dump() == {
        "model_name": "fixture-vl",
        "model_types": ["chat", "image2text"],
        "max_tokens": 0,
        "is_tools": False,
        "features": [],
        "display_name": "fixture-vl",
        "source": "remote",
    }
    assert len(clients) == 1
    baidu = await model_provider_service.discover_models("BaiduYiyan", DiscoveryInput())
    assert len(baidu) == 5 and all(item.source == "static" and set(item.model_types) <= {"chat", "image2text"} for item in baidu)
    assert len(clients) == 1
    with pytest.raises(ValueError, match="unavailable"):
        await model_provider_service.discover_models("Tencent Cloud", DiscoveryInput())


@pytest.mark.parametrize("adapter", [OpenAIAPICompatible, VLLM, LMStudio, Xinference, OpenRouter])
async def test_complete_inventory_limit_is_independent_of_api_page_and_create_limit(monkeypatch: pytest.MonkeyPatch, adapter: type[Base]) -> None:
    models = [router_model(id=f"model-{index:04}") if adapter is OpenRouter else {"id": f"model-{index:04}"} for index in range(2000)]
    transport(monkeypatch, lambda _request: httpx.Response(200, json={"data": models}))
    result = await adapter("", "" if adapter is OpenRouter else "http://127.0.0.1").get_model_list()
    assert len(result) == 2000 and result[0].name == "model-0000" and result[-1].name == "model-1999"
    with pytest.raises(ValidationError):
        InstanceInput(
            instance_name="fixture", api_key="", model_ids=[], custom_models=[{"model_name": item.name, "model_types": item.model_types, "max_tokens": item.max_tokens} for item in result[:101]]
        )


def test_actual_discovery_route_paginates_complete_inventory_without_database(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    factories = [{"name": "OpenRouter", "llm": []}]
    monkeypatch.setattr(settings, "FACTORY_LLM_INFOS", factories)
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"data": [router_model(id=f"model-{index:04}") for index in range(125)]})

    transport(monkeypatch, handler)
    url = "/api/v1/providers/OpenRouter/models/discovery"
    for page, size in ((1, 100), (2, 25)):
        response = client.post(url, params={"page": page, "page_size": 100}, json={"api_key": "fixture"})
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        data = response.json()["data"]
        assert data["total"] == 125 and data["page"] == page and len(data["items"]) == size
        assert all("id" not in item and item["source"] == "remote" for item in data["items"])
        assert data["items"][0]["model_name"] == ("model-0000" if page == 1 else "model-0100")
    assert len(calls) == 2
    for query in ({"page_size": 101}, {"api_key": "secret"}):
        response = client.post(url, params=query, json={})
        assert response.status_code == 422 and len(calls) == 2


@pytest.mark.parametrize("cancel", [False, True])
async def test_localai_detail_failure_and_cancellation_drain_bounded_tasks(monkeypatch: pytest.MonkeyPatch, cancel: bool) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    active: list[asyncio.Task[Any]] = []
    finished: list[asyncio.Task[Any]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tags"):
            return httpx.Response(200, json={"models": [{"name": str(index)} for index in range(8)]})
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": str(index)} for index in range(8)]})
        task = asyncio.current_task()
        assert task is not None
        active.append(task)
        if len(active) == 4:
            started.set()
        try:
            await release.wait()
            return httpx.Response(500)
        finally:
            finished.append(task)

    clients = transport(monkeypatch, handler)
    task = asyncio.create_task(LocalAI("", "http://127.0.0.1").get_model_list())
    try:
        await asyncio.wait_for(started.wait(), 1)
        assert len(active) == 4
        if cancel:
            task.cancel()
        else:
            release.set()
        with pytest.raises(asyncio.CancelledError if cancel else DiscoveryError):
            await task
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert set(active) == set(finished) and all(child.done() for child in active)
    assert all(client.is_closed for client in clients)


@pytest.mark.parametrize("adapter", ADAPTERS)
@pytest.mark.parametrize("content", [b"[]", b"invalid-json", b"x" * 65])
async def test_new_adapters_enforce_json_object_and_body_limit(monkeypatch: pytest.MonkeyPatch, adapter: type[Base], content: bytes) -> None:
    monkeypatch.setattr(model_meta, "MAX_BODY_BYTES", 64)
    clients = transport(monkeypatch, lambda _request: httpx.Response(200, content=content))
    with pytest.raises(DiscoveryError):
        await adapter("", "" if adapter is OpenRouter else "http://127.0.0.1").get_model_list()
    assert all(client.is_closed for client in clients)


@pytest.mark.parametrize("adapter", ADAPTERS)
async def test_new_adapters_explicit_empty_inventory_and_keyless_headers(monkeypatch: pytest.MonkeyPatch, adapter: type[Base]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "Authorization" not in request.headers
        return httpx.Response(200, json={"models": []} if request.url.path.endswith("/tags") else {"data": []})

    clients = transport(monkeypatch, handler)
    assert await adapter("", "" if adapter is OpenRouter else "http://127.0.0.1").get_model_list() == []
    assert all(client.is_closed for client in clients)


@pytest.mark.parametrize("model", [{"id": "model", "model_type": True}, {"id": "model", "model_ability": "chat"}, {"id": "model", "model_ability": [1]}])
async def test_xinference_rejects_malformed_type_and_ability_fields(monkeypatch: pytest.MonkeyPatch, model: dict[str, Any]) -> None:
    transport(monkeypatch, lambda _request: httpx.Response(200, json={"data": [model]}))
    with pytest.raises(DiscoveryError):
        await Xinference("", "http://127.0.0.1").get_model_list()


@pytest.mark.parametrize(
    "model",
    [router_model(architecture=None), router_model(top_provider=[]), router_model(architecture={"input_modalities": "text", "output_modalities": ["text"]}), router_model(supported_parameters=[True])],
)
async def test_openrouter_rejects_malformed_modality_and_parameter_fields(monkeypatch: pytest.MonkeyPatch, model: dict[str, Any]) -> None:
    transport(monkeypatch, lambda _request: httpx.Response(200, json={"data": [model]}))
    with pytest.raises(DiscoveryError):
        await OpenRouter("").get_model_list()


@pytest.mark.parametrize(
    "detail",
    [
        {},
        {"capabilities": "completion", "model_info": {}},
        {"capabilities": ["tools"], "model_info": {}},
        {"capabilities": ["completion"], "model_info": []},
        {"capabilities": ["completion"], "model_info": {"general.context_length": True}},
    ],
)
async def test_localai_rejects_malformed_detail_and_context(monkeypatch: pytest.MonkeyPatch, detail: dict[str, Any]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tags"):
            return httpx.Response(200, json={"models": [{"name": "model"}]})
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "model"}]})
        return httpx.Response(200, json=detail)

    clients = transport(monkeypatch, handler)
    with pytest.raises(DiscoveryError):
        await LocalAI("", "http://127.0.0.1").get_model_list()
    assert all(client.is_closed for client in clients)
