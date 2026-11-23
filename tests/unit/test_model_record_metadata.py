"""Persisted model metadata reaches the actual bundle without truthy defaults."""

import json
from typing import Any

import pytest
from sqlalchemy.orm import Session

from api.db.db_models import TenantModel, TenantModelInstance, TenantModelProvider
from api.db.model_contract import MODEL_TYPE_BITS
from api.db.services import provider_model_resolution
from api.db.services.tenant_llm_service import LLM4Tenant, TenantLangfuseService, TenantLLMService


@pytest.mark.parametrize("limit", [0, 32768])
@pytest.mark.parametrize("tools", [False, True])
def test_registered_record_metadata_reaches_bundle(monkeypatch: pytest.MonkeyPatch, db: Session, limit: int, tools: bool) -> None:
    provider = TenantModelProvider(id="b" * 32, tenant_id="owner", provider_name="OpenAI", status="active")
    instance = TenantModelInstance(
        id="c" * 32,
        provider_id=provider.id,
        instance_name="named",
        status="active",
        api_key=json.dumps({"api_key": "fixture", "is_tools": not tools}),
        base_url="",
        region="",
    )
    model = TenantModel(
        id="a" * 32,
        provider_id=provider.id,
        instance_id=instance.id,
        model_name="actual",
        model_type=MODEL_TYPE_BITS["chat"] | MODEL_TYPE_BITS["image2text"],
        status="active",
        extra={"max_tokens": limit, "is_tools": tools},
        used_tokens=0,
    )

    def model_for_id(_db: Session, identifier: str, tenant_id: str) -> provider_model_resolution.ModelRecord:
        assert (_db, identifier, tenant_id) == (db, model.id, provider.tenant_id)
        return model, instance, provider

    monkeypatch.setattr(provider_model_resolution, "model_for_id", model_for_id)
    source = {"id": model.id, "tenant_id": provider.tenant_id, "llm_name": "supplied-name", "mdl_type": "chat", "max_tokens": 9999, "is_tools": not tools}
    config = TenantLLMService.resolve_model_config(db, source)
    assert config["max_tokens"] == limit and config["is_tools"] is tools
    assert config["llm_name"] == "actual" and config["api_key"] == "fixture"
    assert config["reference"] == model.id and config["model_types"] == ["chat", "image2text"]
    assert source["llm_name"] == "supplied-name"
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(TenantLLMService, "model_instance", lambda _db, _owner, value, **_kw: seen.append(value) or object())
    monkeypatch.setattr(TenantLangfuseService, "filter_by_tenant", lambda *_args, **_kw: None)
    bundle = LLM4Tenant(db, "owner", config)
    assert bundle.max_length == limit and bundle.is_tools is tools
    assert seen[0]["max_tokens"] == limit and seen[0]["is_tools"] is tools


def test_provider_credential_payload_is_preserved() -> None:
    payload = {"api_key": "fixture", "api_version": "version"}
    config = TenantLLMService.decode_model_config({"llm_name": "actual", "api_key": json.dumps(payload), "max_tokens": 0, "is_tools": False})
    assert config["api_key"] == "fixture"
    assert json.loads(config["api_key_payload"]) == payload
    assert config["max_tokens"] == 0 and config["is_tools"] is False
