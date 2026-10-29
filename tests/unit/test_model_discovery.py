"""Remote inventories are complete, bounded and separate from callable models."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest

import core.llm as llm
from core.llm import model_meta
from core.llm.model_meta import Base, DiscoveryError, FishAudio, Ollama, endpoint, fish_access_token


def transport(monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], Awaitable[httpx.Response] | httpx.Response]) -> list[httpx.AsyncClient]:
    clients: list[httpx.AsyncClient] = []

    def client(_self: Base) -> httpx.AsyncClient:
        result = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False, trust_env=False)
        clients.append(result)
        return result

    monkeypatch.setattr(Base, "_client", client)
    return clients


def detail(*, context: Any = 0, capabilities: Any = None) -> dict[str, Any]:
    return {
        "capabilities": capabilities if capabilities is not None else ["completion", "vision", "embedding", "tools", "thinking"],
        "model_info": {"general.architecture": "qwen", "qwen.context_length": context},
        "details": {"family": "ignored-fallback"},
    }


def voice(index: int, *, width: int = 24, title: str = "Same display title") -> dict[str, str]:
    return {"_id": f"{index:0{width}x}", "title": title}


@pytest.mark.parametrize("base,path", [("http://127.0.0.1:11434", ""), ("http://127.0.0.1:11434/v1/", ""), ("http://127.0.0.1:11434/mount/v1", "/mount")])
async def test_ollama_actual_paths_features_and_zero_context(monkeypatch: pytest.MonkeyPatch, base: str, path: str) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["Authorization"] == "Bearer fixture-token"
        if request.method == "GET":
            assert request.url.path == path + "/api/tags"
            return httpx.Response(200, json={"models": [{"name": "qwen:latest"}]})
        assert request.method == "POST" and request.url.path == path + "/api/show"
        assert json.loads(request.content) == {"model": "qwen:latest"}
        return httpx.Response(200, json=detail())

    clients = transport(monkeypatch, handler)
    models = await Ollama("fixture-token", base).get_model_list()
    assert len(requests) == 2 and len(models) == 1
    assert models[0].name == models[0].display_name == "qwen:latest"
    assert models[0].model_types == ["chat", "image2text", "embedding"]
    assert models[0].features == ["is_tools", "thinking"] and models[0].max_tokens == 0
    assert all(client.is_closed for client in clients)


async def test_ollama_parallelism_is_bounded_at_four(monkeypatch: pytest.MonkeyPatch) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    active = 0
    maximum = 0
    finished: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active, maximum
        if request.method == "GET":
            return httpx.Response(200, json={"models": [{"name": f"model-{index}"} for index in range(8)]})
        name = json.loads(request.content)["model"]
        active += 1
        maximum = max(maximum, active)
        if active == 4:
            started.set()
        try:
            await release.wait()
            return httpx.Response(200, json=detail(capabilities=["completion"]))
        finally:
            active -= 1
            finished.append(name)

    clients = transport(monkeypatch, handler)
    task = asyncio.create_task(Ollama("", "http://ollama.invalid").get_model_list())
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        assert maximum == 4
        release.set()
        models = await task
        assert [model.name for model in models] == [f"model-{index}" for index in range(8)]
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert maximum == 4 and active == 0 and len(finished) == 8
    assert all(client.is_closed for client in clients)


async def test_ollama_cancellation_drains_started_detail_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    ready = asyncio.Event()
    never = asyncio.Event()
    started: list[asyncio.Task[Any]] = []
    finished: list[asyncio.Task[Any]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"models": [{"name": f"model-{index}"} for index in range(8)]})
        task = asyncio.current_task()
        assert task is not None
        started.append(task)
        if len(started) == 4:
            ready.set()
        try:
            await never.wait()
            return httpx.Response(200, json=detail())
        finally:
            finished.append(task)

    clients = transport(monkeypatch, handler)
    task = asyncio.create_task(Ollama("", "http://ollama.invalid").get_model_list())
    try:
        await asyncio.wait_for(ready.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert len(started) == 4 and set(finished) == set(started)
    assert all(child.done() for child in started)
    assert all(client.is_closed for client in clients)


@pytest.mark.parametrize("provider", ["ollama", "fish"])
async def test_explicit_empty_inventory_is_success(monkeypatch: pytest.MonkeyPatch, provider: str) -> None:
    payload = {"models": []} if provider == "ollama" else {"items": [], "total": 0}
    transport(monkeypatch, lambda _request: httpx.Response(200, json=payload))
    adapter = Ollama("", "http://ollama.invalid") if provider == "ollama" else FishAudio("fixture-token")
    assert await adapter.get_model_list() == []


async def test_ollama_keyless_request_family_fallback_and_deduplicated_capabilities(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "Authorization" not in request.headers
        if request.method == "GET":
            return httpx.Response(200, json={"models": [{"name": "model"}]})
        return httpx.Response(200, json={"capabilities": ["completion", "completion", "tools"], "model_info": {"llama.context_length": 4096}, "details": {"family": "llama"}})

    transport(monkeypatch, handler)
    models = await Ollama("", "http://ollama.invalid").get_model_list()
    assert models[0].model_types == ["chat"] and models[0].max_tokens == 4096
    assert models[0].features == ["is_tools"]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"models": None},
        {"models": {}},
        {"models": ["model"]},
        {"models": [{"name": " padded "}]},
        {"models": [{"name": "same"}, {"name": "same"}]},
        {"models": [{"name": str(index)} for index in range(101)]},
    ],
)
async def test_ollama_rejects_invalid_duplicate_and_excess_inventory(monkeypatch: pytest.MonkeyPatch, payload: Any) -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.method)
        return httpx.Response(200, json=payload)

    transport(monkeypatch, handler)
    with pytest.raises(DiscoveryError):
        await Ollama("", "http://ollama.invalid").get_model_list()
    assert requests == ["GET"]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"capabilities": "completion", "model_info": {}},
        {"capabilities": [1], "model_info": {}},
        {"capabilities": ["completion"], "model_info": []},
        {"capabilities": ["tools"], "model_info": {}},
        {**detail(), "details": []},
        *[detail(context=value) for value in (True, -1, 1.5, "0", None, 2147483648)],
    ],
)
async def test_ollama_rejects_bad_detail_shape_and_context(monkeypatch: pytest.MonkeyPatch, payload: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"models": [{"name": "model"}]} if request.method == "GET" else payload)

    transport(monkeypatch, handler)
    with pytest.raises(DiscoveryError):
        await Ollama("", "http://ollama.invalid").get_model_list()


@pytest.mark.parametrize("status", [401, 500, 302, 307])
@pytest.mark.parametrize("provider", ["ollama", "fish"])
async def test_remote_status_and_redirects_are_errors_without_following(monkeypatch: pytest.MonkeyPatch, status: int, provider: str) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status, headers={"Location": "https://secret-destination.invalid"}, json={"token": "secret-response"})

    clients = transport(monkeypatch, handler)
    adapter = Ollama("", "http://ollama.invalid") if provider == "ollama" else FishAudio("fixture-token")
    with pytest.raises(DiscoveryError) as caught:
        await adapter.get_model_list()
    assert len(requests) == 1 and "secret" not in str(caught.value)
    assert all(client.is_closed for client in clients)


@pytest.mark.parametrize("content", [b"[]", b"not-json", b"x" * 65])
async def test_response_shape_json_and_body_limit_are_visible(monkeypatch: pytest.MonkeyPatch, content: bytes) -> None:
    monkeypatch.setattr(model_meta, "MAX_BODY_BYTES", 64)
    transport(monkeypatch, lambda _request: httpx.Response(200, content=content))
    with pytest.raises(DiscoveryError):
        await Ollama("", "http://ollama.invalid").get_model_list()


@pytest.mark.parametrize("provider", ["ollama", "fish"])
async def test_total_discovery_timeout_closes_clients(monkeypatch: pytest.MonkeyPatch, provider: str) -> None:
    monkeypatch.setattr(model_meta, "DISCOVERY_SECONDS", 0.01)
    finished: list[bool] = []

    async def handler(_request: httpx.Request) -> httpx.Response:
        try:
            await asyncio.Event().wait()
            return httpx.Response(200, json={})
        finally:
            finished.append(True)

    clients = transport(monkeypatch, handler)
    adapter = Ollama("", "http://ollama.invalid") if provider == "ollama" else FishAudio("fixture-token")
    with pytest.raises(DiscoveryError, match="timed out"):
        await adapter.get_model_list()
    assert finished == [True] and all(client.is_closed for client in clients)


async def test_transport_timeout_is_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("sensitive-url-and-token")

    transport(monkeypatch, handler)
    with pytest.raises(DiscoveryError, match="transport or response failed") as caught:
        await Ollama("", "http://ollama.invalid").get_model_list()
    assert "sensitive" not in str(caught.value)


@pytest.mark.parametrize("base", ["https://arbitrary.invalid", "http://localhost:11434", "http://10.0.0.1:11434", "http://127.0.0.1.example.invalid:11434"])
async def test_ollama_credentials_reject_nonliteral_loopback_before_http(monkeypatch: pytest.MonkeyPatch, base: str) -> None:
    clients = transport(monkeypatch, lambda _request: pytest.fail("credential must not be sent"))
    with pytest.raises(ValueError, match="literal loopback"):
        await Ollama("fixture-token", base).get_model_list()
    assert clients == []


@pytest.mark.parametrize("base", ["", "https://api.fish.audio", "https://api.fish.audio:443/v1", "https://api.fish.audio/v1/tts"])
async def test_fish_complete_pagination_and_stable_ids(monkeypatch: pytest.MonkeyPatch, base: str) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "GET" and request.url.host == "api.fish.audio" and request.url.path == "/model"
        assert request.headers["Authorization"] == "Bearer fixture-token"
        assert request.url.params["self"] == "true" and request.url.params["page_size"] == "10"
        number = int(request.url.params["page_number"])
        assert number in {1, 2}
        items = [voice(index) for index in range(1, 11)] if number == 1 else [voice(11, width=32), voice(12)]
        return httpx.Response(200, json={"items": items, "total": 12})

    clients = transport(monkeypatch, handler)
    models = await FishAudio(json.dumps({"fish_audio_ak": "fixture-token"}), base).get_model_list()
    assert len(requests) == 2 and len(models) == 12 and len({model.name for model in models}) == 12
    assert {model.display_name for model in models} == {"Same display title"}
    assert models[10].name == voice(11, width=32)["_id"]
    assert all(model.model_types == ["tts"] and model.features == [] for model in models)
    assert all(client.is_closed for client in clients)


@pytest.mark.parametrize(
    "key", ["", " padded ", "{", "{}", "[]", '"token"', '{"api_key":null}', '{"api_key":12}', '{"api_key":""}', '{"api_key":"one","fish_audio_ak":"two"}', '{"access_token":"padded "}']
)
async def test_fish_malformed_credentials_fail_before_http(monkeypatch: pytest.MonkeyPatch, key: str) -> None:
    clients = transport(monkeypatch, lambda _request: pytest.fail("invalid credential must not be sent"))
    with pytest.raises(ValueError, match="credential"):
        await FishAudio(key).get_model_list()
    assert clients == []


def test_fish_consistent_structured_key_aliases() -> None:
    assert fish_access_token(json.dumps({"api_key": "same", "fish_audio_ak": "same", "access_token": "same"})) == "same"


@pytest.mark.parametrize("base", ["http://api.fish.audio", "https://api.fish.audio.evil.invalid", "https://evil.invalid", "https://api.fish.audio:444", "https://api.fish.audio/other"])
async def test_fish_rejects_nonofficial_origin_before_http(monkeypatch: pytest.MonkeyPatch, base: str) -> None:
    clients = transport(monkeypatch, lambda _request: pytest.fail("credential must not be sent"))
    with pytest.raises(ValueError, match="official HTTPS origin"):
        await FishAudio("fixture-token", base).get_model_list()
    assert clients == []


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"items": [], "total": True},
        {"items": None, "total": 0},
        {"items": [], "total": 101},
        {"items": [voice(index) for index in range(11)], "total": 11},
        {"items": [voice(1), voice(1)], "total": 2},
        {"items": [{"_id": "bad", "title": "voice"}], "total": 1},
        {"items": [{"_id": "z" * 24, "title": "voice"}], "total": 1},
        {"items": [{"_id": voice(1)["_id"], "title": ""}], "total": 1},
        {"items": [None], "total": 1},
    ],
)
async def test_fish_rejects_bad_inventory_shape_identity_and_limits(monkeypatch: pytest.MonkeyPatch, payload: Any) -> None:
    transport(monkeypatch, lambda _request: httpx.Response(200, json=payload))
    with pytest.raises(DiscoveryError):
        await FishAudio("fixture-token").get_model_list()


@pytest.mark.parametrize("scenario", ["empty", "over-total", "duplicate-across-pages", "total-drift", "page-limit"])
async def test_fish_rejects_incomplete_or_inconsistent_pagination(monkeypatch: pytest.MonkeyPatch, scenario: str) -> None:
    pages: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        number = int(request.url.params["page_number"])
        pages.append(number)
        if scenario == "page-limit":
            return httpx.Response(200, json={"items": [voice(number)], "total": 100})
        if number == 1:
            return httpx.Response(200, json={"items": [voice(index) for index in range(1, 11)], "total": 12})
        payload = {
            "empty": {"items": [], "total": 12},
            "over-total": {"items": [voice(index) for index in range(11, 14)], "total": 12},
            "duplicate-across-pages": {"items": [voice(1), voice(11)], "total": 12},
            "total-drift": {"items": [voice(11)], "total": 11},
        }[scenario]
        return httpx.Response(200, json=payload)

    clients = transport(monkeypatch, handler)
    with pytest.raises(DiscoveryError):
        await FishAudio("fixture-token").get_model_list()
    assert pages == list(range(1, len(pages) + 1)) and len(pages) <= 11
    assert all(client.is_closed for client in clients)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "ftp://host",
        "http://user:secret@host",
        "http://host/path?token=secret",
        "http://host/path#fragment",
        "http://host:0",
        "http://host:65536",
        "http://host/../api",
        "http://host/%2e%2e/api",
        "http://host/path\\other",
        "http://169.254.169.254",
        "http://224.0.0.1",
        "http://0.0.0.0",
        " http://host",
        "http://host/\napi",
    ],
)
def test_discovery_endpoint_rejects_ambiguous_or_unsafe_urls(value: str) -> None:
    with pytest.raises(ValueError):
        endpoint(value)


def test_modelmeta_registry_loads_only_metadata_module_and_surfaces_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    imported: list[str] = []
    monkeypatch.setitem(llm.__dict__, "ModelMeta", None)
    monkeypatch.delitem(llm.__dict__, "ModelMeta", raising=False)

    def fail(name: str) -> Any:
        imported.append(name)
        raise ImportError("controlled metadata load failure")

    monkeypatch.setattr(llm.importlib, "import_module", fail)
    with pytest.raises(ImportError, match="controlled metadata load failure"):
        llm.__getattr__("ModelMeta")
    assert "ModelMeta" not in llm.__dict__

    def load(name: str) -> Any:
        imported.append(name)
        assert name == "core.llm.model_meta"
        return model_meta

    monkeypatch.setattr(llm.importlib, "import_module", load)
    registry = llm.__getattr__("ModelMeta")
    assert registry == {"Ollama": Ollama, "Fish Audio": FishAudio}
    assert llm.ModelMeta is registry
    assert imported == ["core.llm.model_meta", "core.llm.model_meta"]
