"""Discovery credentials remain usable by the active OpenRouter adapters."""

import json
from typing import Any

import pytest
from sqlalchemy.orm import Session

from api.db.provider_catalog import normalize_model_transport, provider_transport
from api.db.services.provider_verification import _close
from api.db.services.tenant_llm_service import TenantLLMService


@pytest.mark.parametrize(
    ("credential", "expected_order"),
    [
        ("controlled-key", ""),
        (json.dumps({"api_key": "controlled-key"}), ""),
        (json.dumps({"api_key": "controlled-key", "provider_order": "first,second"}), "first,second"),
        (json.dumps({"api_key": "controlled-key", "provider_order": ["first", "second"], "is_tools": False}), ["first", "second"]),
    ],
)
@pytest.mark.parametrize("capability", ["chat", "image2text"])
async def test_raw_and_known_credentials_construct_actual_runtime_adapters(credential: str, expected_order: str | list[str], capability: str) -> None:
    config: dict[str, Any] = {"llm_factory": "OpenRouter", "llm_name": "controlled-model", "mdl_type": capability, "api_key": credential, "api_base": "http://127.0.0.1:1/v1"}
    before = dict(config)
    key, base, region = provider_transport("OpenRouter", credential, config["api_base"], "")
    assert json.loads(key)["api_key"] == "controlled-key"
    assert json.loads(key)["provider_order"] == expected_order
    assert base == config["api_base"] and region == ""
    assert normalize_model_transport(config)["api_key"] == key and config == before
    with Session() as db:
        model = TenantLLMService.model_instance(db, "controlled-owner", config)
    try:
        assert (model.api_key if capability == "chat" else model.client.api_key) == "controlled-key"
        if capability == "chat":
            assert model.provider_order == expected_order
    finally:
        await _close(model)


@pytest.mark.parametrize(
    "credential",
    [
        "",
        " key",
        "key\n",
        "{}",
        "[]",
        '"key"',
        '{"api_key":',
        '{"api_key": ""}',
        '{"api_key": 1}',
        '{"api_key": "key", "unknown": "value"}',
        '{"api_key": "key", "provider_order": null}',
        '{"api_key": "key", "provider_order": [1]}',
        '{"api_key": "key", "is_tools": "false"}',
    ],
)
def test_invalid_credentials_are_rejected_without_echo(credential: str) -> None:
    with pytest.raises(ValueError, match=r"^Invalid OpenRouter credential$"):
        provider_transport("OpenRouter", credential, "", "")
