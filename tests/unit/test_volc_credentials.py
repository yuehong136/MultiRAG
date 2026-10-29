"""Volc credentials have the same strict meaning across active adapters."""

import json
from typing import Any

import pytest

from api.db.model_contract import ConnectionCandidate
from api.db.services.provider_verification import _close
from core.llm.chat import VolcEngineChat
from core.llm.cv import VolcEngineCV
from core.llm.embedding import VolcEngineEmbed
from core.llm.volc_auth import decode_volc_key


@pytest.mark.parametrize(
    "payload,expected",
    [
        ("fixture-ark", "display-model"),
        ({"ark_api_key": "fixture-ark"}, "display-model"),
        ({"ark_api_key": "fixture-ark", "ep_id": "ep-one"}, "ep-one"),
        ({"ark_api_key": "fixture-ark", "endpoint_id": "ep-two"}, "ep-two"),
        ({"ark_api_key": "fixture-ark", "ep_id": "ep-both", "endpoint_id": "ep-both"}, "ep-both"),
    ],
)
@pytest.mark.parametrize("adapter", [VolcEngineChat, VolcEngineCV, VolcEngineEmbed])
async def test_active_adapters_share_credentials_and_endpoint(payload: Any, expected: str, adapter: Any) -> None:
    key = json.dumps(payload) if isinstance(payload, dict) else payload
    model = adapter(key, "display-model", base_url="http://fixture.invalid/custom/v3", max_retries=0)
    try:
        assert model.model_name == ("display-model" if adapter is VolcEngineEmbed else expected)
        if adapter is VolcEngineEmbed:
            assert model.api_key == "fixture-ark"
            assert model.multimodal_endpoint == "http://fixture.invalid/custom/v3/embeddings/multimodal"
        else:
            assert model.client.api_key == model.async_client.api_key == "fixture-ark"
            assert str(model.client.base_url) == "http://fixture.invalid/custom/v3/"
    finally:
        await _close(model)
    if adapter is not VolcEngineEmbed:
        assert model.client.is_closed() and model.async_client.is_closed()


@pytest.mark.parametrize(
    "key",
    [
        "",
        " ",
        " fixture-secret",
        "fixture-secret\n",
        "[]",
        "null",
        "true",
        "42",
        '"fixture-secret"',
        '{"ark_api_key":"fixture-secret"',
        '{"ark_api_key":"fixture-secret","ark_api_key":"other"}',
        '{"ark_api_key":null}',
        '{"ark_api_key":42}',
        '{"ark_api_key":""}',
        '{"ark_api_key":"fixture-secret","ep_id":""}',
        '{"ark_api_key":"fixture-secret","endpoint_id":false}',
        '{"ark_api_key":"fixture-secret","ep_id":"ep-a","endpoint_id":"ep-b"}',
    ],
)
@pytest.mark.parametrize("adapter", [VolcEngineChat, VolcEngineCV, VolcEngineEmbed])
def test_invalid_credentials_fail_before_clients_without_secret_echo(key: str, adapter: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Invalid credentials must fail before allocating a client")

    module = __import__(adapter.__module__, fromlist=["unused"])
    monkeypatch.setattr(module, "OpenAI", forbidden)
    monkeypatch.setattr(module, "AsyncOpenAI", forbidden, raising=False)
    monkeypatch.setattr(module.requests if hasattr(module, "requests") else module, "Session", forbidden, raising=False)
    with pytest.raises(ValueError) as failure:
        adapter(key, "display-model")
    assert "fixture-secret" not in str(failure.value)


def test_envelope_options_and_internal_embedding_dict_are_preserved() -> None:
    payload = {"ark_api_key": "fixture-ark", "endpoint_id": "ep-model", "dimensions": 512, "encoding_format": "float"}
    original = payload.copy()
    model = VolcEngineEmbed(payload, "display-model")
    try:
        assert model.model_name == "display-model" and model.default_dimensions == 512 and model.default_encoding_format == "float"
        assert payload == original
    finally:
        model.close()


@pytest.mark.parametrize("adapter", [VolcEngineChat, VolcEngineCV, VolcEngineEmbed])
async def test_pretty_printed_json_envelope_remains_valid(adapter: Any) -> None:
    model = adapter(json.dumps({"ark_api_key": "fixture-ark", "endpoint_id": "ep-pretty"}, indent=2), "display")
    try:
        assert model.model_name == ("display" if adapter is VolcEngineEmbed else "ep-pretty")
    finally:
        await _close(model)


def test_plain_key_requires_explicit_model_and_http_dto_remains_string() -> None:
    with pytest.raises(ValueError, match="explicit model"):
        decode_volc_key("fixture-ark", "")
    with pytest.raises(ValueError):
        ConnectionCandidate(api_key={"ark_api_key": "fixture-secret"})


def test_embedding_requires_explicit_model_even_with_chat_endpoint() -> None:
    with pytest.raises(ValueError, match="explicit model"):
        VolcEngineEmbed('{"ark_api_key":"fixture-ark","endpoint_id":"ep-chat"}', "")


def test_embedding_base64_vectors_remain_supported() -> None:
    import base64

    import numpy as np

    model = VolcEngineEmbed("fixture-ark", "embed-model")
    try:
        encoded = base64.b64encode(np.array([0.25, 0.75], dtype="float32").tobytes()).decode()
        assert model._extract_multimodal_embedding({"data": {"embedding": encoded}}) == [0.25, 0.75]
    finally:
        model.close()


def test_embedding_probe_closes_its_owned_session(monkeypatch: pytest.MonkeyPatch) -> None:
    model = VolcEngineEmbed("fixture-ark", "ep-model")
    closed: list[bool] = []
    monkeypatch.setattr(model.session, "close", lambda: closed.append(True))
    import asyncio

    asyncio.run(_close(model))
    assert closed == [True]


@pytest.mark.parametrize("failure", ["status", "invalid-json", "transport", "base64", "nonfinite"])
def test_embedding_errors_never_log_provider_echo_and_close_response(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, failure: str) -> None:
    import requests

    from common.exceptions import ModelException

    model = VolcEngineEmbed("fixture-secret-ark", "ep-model")
    response = requests.Response()
    response.status_code = 403 if failure == "status" else 200
    response._content = b"fixture-secret-ark"
    if failure in {"base64", "nonfinite"}:
        response._content = json.dumps({"data": {"embedding": "a" if failure == "base64" else [float("inf")]}, "msg": "fixture-secret-ark"}).encode()
    closed: list[bool] = []
    monkeypatch.setattr(response, "close", lambda: closed.append(True))

    def post(**kwargs: Any) -> requests.Response:
        if failure == "transport":
            raise requests.ConnectionError("fixture-secret-ark")
        return response

    monkeypatch.setattr(model.session, "post", post)
    try:
        with pytest.raises(ModelException) as error:
            model.encode(["hello"])
        assert "fixture-secret-ark" not in str(error.value) + caplog.text
        assert closed == ([] if failure == "transport" else [True])
    finally:
        model.close()
