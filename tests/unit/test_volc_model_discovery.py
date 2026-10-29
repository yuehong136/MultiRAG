"""Official Ark inventory retains only usable runtime declarations."""

import asyncio
import json
from typing import Any

import httpx
import pytest

from api.db.model_contract import DiscoveryInput
from api.db.services import model_provider_service
from common import settings
from core.llm import ModelMeta
from core.llm.model_meta import Base, DiscoveryError, VolcEngine
from tests.unit.test_model_metadata_adapters import transport


@pytest.mark.parametrize("key", ["fixture-ark", '{"ark_api_key":"fixture-ark"}', '{"ark_api_key":"fixture-ark","ep_id":"ep-unrelated"}'])
async def test_official_inventory_and_readonly_service(monkeypatch: pytest.MonkeyPatch, key: str) -> None:
    rows = [
        {
            "id": "vision",
            "modalities": {"input_modalities": ["text", "image"], "output_modalities": ["text"]},
            "features": {"tools": {"function_calling": True}},
            "token_limits": {"max_input_token_length": 0, "max_reasoning_token_length": 10},
        },
        {"id": "embedding", "domain": "Embedding"},
        {"id": "embedding-task", "task_type": ["ImageEmbedding"]},
        {"id": "embedding-output", "modalities": {"output_modalities": ["embeddings"]}},
        {"id": "voice", "modalities": {"output_modalities": ["audio"]}},
        {"id": "shutdown", "status": "Shutdown"},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://ark.cn-beijing.volces.com/api/v3/models"
        assert request.method == "GET" and request.headers["Authorization"] == "Bearer fixture-ark"
        return httpx.Response(200, json={"data": rows})

    clients = transport(monkeypatch, handler)
    monkeypatch.setattr(settings, "FACTORY_LLM_INFOS", [{"name": "VolcEngine", "llm": []}])
    assert ModelMeta["VolcEngine"] is VolcEngine
    items = await model_provider_service.discover_models("VolcEngine", DiscoveryInput(api_key=key))
    by_name = {item.model_name: item for item in items}
    assert set(by_name) == {"vision", "embedding", "embedding-task", "embedding-output"}
    assert by_name["vision"].model_types == ["chat", "image2text"]
    assert by_name["vision"].max_tokens == 0 and by_name["vision"].features == ["is_tools", "thinking"]
    assert all(by_name[name].model_types == ["embedding"] for name in by_name if name != "vision")
    assert all(item.source == "remote" for item in items) and all(client.is_closed for client in clients)


@pytest.mark.parametrize(
    "row",
    [
        {"id": "x", "task_type": None},
        {"id": "x", "modalities": []},
        {"id": "x", "token_limits": {"max_input_token_length": True}},
        {"id": "x", "token_limits": {"max_reasoning_token_length": float("inf")}},
        {"id": "x", "features": {"tools": {"function_calling": "true"}}},
        {"id": "x", "status": False},
        {"id": "x", "token_limits": {"max_reasoning_token_length": None}},
    ],
)
async def test_bad_schema_fails_closed(monkeypatch: pytest.MonkeyPatch, row: dict[str, Any]) -> None:
    clients = transport(monkeypatch, lambda _: httpx.Response(200, content=json.dumps({"data": [row]})))
    with pytest.raises(DiscoveryError):
        await VolcEngine("fixture").get_model_list()
    assert all(client.is_closed for client in clients)


@pytest.mark.parametrize("payload", [{"data": [{"id": "x"}, {"id": "x"}]}, {"data": [], "has_more": True}, {"data": [], "total": 1}, {"data": [{"id": str(i)} for i in range(2001)]}, {"data": "bad"}])
async def test_inventory_limits_and_completeness(monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]) -> None:
    clients = transport(monkeypatch, lambda _: httpx.Response(200, json=payload))
    with pytest.raises(DiscoveryError):
        await VolcEngine("fixture").get_model_list()
    assert all(client.is_closed for client in clients)


@pytest.mark.parametrize("status", [401, 429, 500, 302])
async def test_http_failures_and_body_limit(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    clients = transport(monkeypatch, lambda _: httpx.Response(status, text="secret"))
    with pytest.raises(DiscoveryError) as error:
        await VolcEngine("fixture").get_model_list()
    assert "secret" not in str(error.value) and all(client.is_closed for client in clients)
    transport(monkeypatch, lambda _: httpx.Response(200, content=b" " * (1024 * 1024 + 1)))
    with pytest.raises(DiscoveryError, match="size limit"):
        await VolcEngine("fixture").get_model_list()


@pytest.mark.parametrize("key", ["{}", "[]", '{"ark_api_key":', '{"ark_api_key":"fixture","ark_api_key":"duplicate"}', '{"ark_api_key":"fixture","ep_id":"a","endpoint_id":"b"}'])
async def test_credentials_rejected_before_client(monkeypatch: pytest.MonkeyPatch, key: str) -> None:
    def forbidden(_: Base) -> httpx.AsyncClient:
        pytest.fail("no client for invalid credentials")

    monkeypatch.setattr(Base, "_client", forbidden)
    with pytest.raises(ValueError):
        await VolcEngine(key).get_model_list()


@pytest.mark.parametrize("cancel", [False, True])
async def test_timeout_cancel_closes_owned_client(monkeypatch: pytest.MonkeyPatch, cancel: bool) -> None:
    from core.llm import model_meta

    started = asyncio.Event()

    async def handler(_: httpx.Request) -> httpx.Response:
        started.set()
        await asyncio.Event().wait()
        raise AssertionError

    clients = transport(monkeypatch, handler)
    monkeypatch.setattr(model_meta, "DISCOVERY_SECONDS", 0.02)
    task = asyncio.create_task(VolcEngine("fixture").get_model_list())
    await started.wait()
    if cancel:
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else DiscoveryError):
        await task
    assert all(client.is_closed for client in clients)


@pytest.mark.parametrize(
    "base", ["http://ark.cn-beijing.volces.com/api/v3", "https://fixture.invalid/api/v3", "https://ark.cn-beijing.volces.com/api/v3/models", "https://ark.cn-beijing.volces.com/api/v3?key=x"]
)
async def test_untrusted_origin_rejected_without_clients(monkeypatch: pytest.MonkeyPatch, base: str) -> None:
    def forbidden(_: Base) -> httpx.AsyncClient:
        pytest.fail("untrusted origin cannot receive credential")

    monkeypatch.setattr(Base, "_client", forbidden)
    with pytest.raises(ValueError):
        await VolcEngine("fixture", base).get_model_list()


async def test_complete_inventory_exceeds_api_page_size(monkeypatch: pytest.MonkeyPatch) -> None:
    clients = transport(monkeypatch, lambda _: httpx.Response(200, json={"data": [{"id": f"model-{i}", "domain": "Embedding"} for i in range(125)]}))
    assert len(await VolcEngine("fixture").get_model_list()) == 125
    assert all(client.is_closed for client in clients)
