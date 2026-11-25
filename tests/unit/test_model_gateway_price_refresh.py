"""Fetching LiteLLM's cost map: what replaces the price table and what must not."""

import asyncio
import json
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from api.model_gateway import price_refresh, pricing
from api.model_gateway.price_refresh import PriceMapRefresher, refresher_of, validate_cost_map
from common.app_config import ModelGatewayConfig

URL = "https://prices.test/map.json"


@pytest.fixture(autouse=True)
def _bundled_table_afterwards() -> Iterator[None]:
    yield
    pricing.install_table(None)
    price_refresh._shared = None


def _map(models: int, **extra: dict[str, Any]) -> dict[str, Any]:
    entries: dict[str, Any] = {f"openai/model-{index}": {"litellm_provider": "openai", "input_cost_per_token": 1e-6, "output_cost_per_token": 2e-6} for index in range(models)}
    return {**entries, **extra}


def _refresher(answer: Any, source: str = URL) -> tuple[PriceMapRefresher, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if isinstance(answer, Exception):
            raise answer
        return answer if isinstance(answer, httpx.Response) else httpx.Response(200, json=answer)

    return PriceMapRefresher(source, 3600.0, transport=httpx.MockTransport(handler)), seen


def _newest_price() -> Decimal | None:
    price = pricing.resolve_price("OpenAI", "brand-new")
    return price.base.input if price is not None else None


async def test_a_fetched_map_prices_models_the_bundled_one_does_not_know():
    bundled = len(pricing.default_table())
    refresher, seen = _refresher(_map(bundled, **{"openai/brand-new": {"litellm_provider": "openai", "input_cost_per_token": 3e-6, "output_cost_per_token": 9e-6}}))
    assert _newest_price() is None

    assert await refresher.refresh_once() is True

    assert _newest_price() == Decimal("0.000003")
    assert str(seen[0].url) == URL


@pytest.mark.parametrize(
    "answer",
    [httpx.ConnectError("no route"), httpx.Response(404), httpx.Response(200, content=b"<html>not json"), httpx.Response(200, json=["a", "list"]), httpx.Response(200, json={})],
    ids=["unreachable", "http-error", "not-json", "not-an-object", "empty"],
)
async def test_a_failed_or_malformed_fetch_keeps_the_current_prices(answer):
    before = pricing.default_table()
    refresher, _ = _refresher(answer)

    assert await refresher.refresh_once() is False

    assert pricing.default_table() is before


async def test_a_map_far_smaller_than_the_one_in_use_is_rejected():
    before = pricing.default_table()
    refresher, _ = _refresher(_map(len(before) // 2 - 10))

    assert await refresher.refresh_once() is False
    assert pricing.default_table() is before


def test_validation_follows_litellm_thresholds():
    assert validate_cost_map(_map(49), 0) == "it lists 49 models, fewer than 50"
    assert validate_cost_map(_map(50), 0) is None
    assert validate_cost_map(_map(100), 201) == "it lists 100 models, less than half of the 201 in use"
    assert validate_cost_map(_map(100), 200) is None
    # Entries that are not objects, such as a sample spec, are not models.
    assert validate_cost_map({**_map(49), "sample_spec": "see docs"}, 0) is not None
    assert validate_cost_map("text", 0) == "it is not a JSON object with entries"


async def test_an_internal_mirror_can_be_a_file(tmp_path: Path):
    mirror = tmp_path / "prices.json"
    mirror.write_text(json.dumps(_map(len(pricing.default_table()), **{"openai/brand-new": {"litellm_provider": "openai", "input_cost_per_token": 5e-6}})), encoding="utf-8")
    refresher, seen = _refresher({}, source=str(mirror))

    assert await refresher.refresh_once() is True
    assert _newest_price() == Decimal("0.000005") and seen == []
    # A missing file is a failed fetch like any other.
    assert await PriceMapRefresher(str(tmp_path / "missing.json"), 3600.0).refresh_once() is False


async def test_kicking_refreshes_in_the_background_and_not_again_until_due():
    refresher, seen = _refresher(_map(len(pricing.default_table())))

    refresher.kick()
    refresher.kick()
    await asyncio.sleep(0.05)
    refresher.kick()
    await asyncio.sleep(0.05)

    assert len(seen) == 1


def test_refreshing_is_on_by_default_and_zero_hours_switch_it_off():
    assert refresher_of(ModelGatewayConfig(price_map_refresh_hours=0.0)) is None
    first = refresher_of(ModelGatewayConfig())
    assert first is not None and first is refresher_of(ModelGatewayConfig())
    assert first._interval == 24 * 3600
    # An empty address means LiteLLM's own repository.
    assert first._source == price_refresh.MODEL_COST_MAP_URL
