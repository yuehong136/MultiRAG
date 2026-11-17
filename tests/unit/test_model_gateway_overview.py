"""Address and data-plane probe of the model gateway overview, without a database."""

import asyncio

import httpx
import pytest

from api.model_gateway.management import data_plane_status, public_base_url
from common.app_config import ModelGatewayConfig


@pytest.mark.parametrize(
    ("settings", "expected"),
    [
        ({}, "http://127.0.0.1:9390/v1"),
        ({"host": "gateway.internal", "http_port": 8443}, "http://gateway.internal:8443/v1"),
        ({"host": "::1"}, "http://[::1]:9390/v1"),
        # A wildcard listener says nothing about the address callers use.
        ({"host": "0.0.0.0"}, None),
        ({"host": "::"}, None),
        ({"host": "0.0.0.0", "public_base_url": " https://llm.example.com/v1/ "}, "https://llm.example.com/v1"),
    ],
)
def test_public_base_url_prefers_the_configured_address(settings, expected):
    assert public_base_url(ModelGatewayConfig(**settings)) == expected


def _probe(handler, **settings) -> tuple[str, list[str]]:
    seen: list[str] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return handler(request)

    status = asyncio.run(data_plane_status(ModelGatewayConfig(**settings), transport=httpx.MockTransport(record)))
    return status, seen


def test_data_plane_is_up_when_its_health_check_answers():
    status, seen = _probe(lambda request: httpx.Response(200, json={"status": "ok"}), host="0.0.0.0", http_port=9391)

    assert status == "up"
    # A wildcard listener is probed on the loopback address.
    assert seen == ["http://127.0.0.1:9391/healthz"]


@pytest.mark.parametrize("response", [httpx.Response(503), httpx.ConnectError("refused")])
def test_data_plane_is_down_when_the_health_check_fails(response):
    def handler(request: httpx.Request) -> httpx.Response:
        if isinstance(response, Exception):
            raise response
        return response

    assert _probe(handler)[0] == "down"
