"""Persisted model metadata reaches the actual bundle without truthy defaults."""

import json
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.orm import Session

from api.db.services import provider_model_resolution
from api.db.services.tenant_llm_service import LLM4Tenant, TenantLangfuseService, TenantLLMService


@pytest.mark.parametrize("limit", [0, 32768])
@pytest.mark.parametrize("tools", [False, True])
def test_linked_record_metadata_reaches_bundle(monkeypatch: pytest.MonkeyPatch, db: Session, limit: int, tools: bool) -> None:
    model = SimpleNamespace(model_name="actual", model_types=["chat", "image2text"], is_tools=tools)
    instance = SimpleNamespace(id="instance", instance_name="named", status="active", api_key=json.dumps({"api_key": "fixture", "is_tools": not tools}), base_url="", region="")
    provider = SimpleNamespace(status="active")
    monkeypatch.setattr(provider_model_resolution, "linked_model", lambda *_args: (model, instance, provider))
    source = {"id": 7, "tenant_id": "owner", "llm_name": "opaque-storage-name", "llm_factory": "OpenAI", "mdl_type": "chat", "max_tokens": limit, "is_tools": not tools}
    config = TenantLLMService.resolve_model_config(db, source)
    assert config["max_tokens"] == limit and config["is_tools"] is tools
    assert config["llm_name"] == "actual" and config["api_key"] == "fixture"
    assert source["llm_name"] == "opaque-storage-name"
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(TenantLLMService, "model_instance", lambda _db, _owner, value, **_kw: seen.append(value) or object())
    monkeypatch.setattr(TenantLangfuseService, "filter_by_tenant", lambda *_args, **_kw: None)
    bundle = LLM4Tenant(db, "owner", config)
    assert bundle.max_length == limit and bundle.is_tools is tools
    assert seen[0]["max_tokens"] == limit and seen[0]["is_tools"] is tools


@pytest.mark.parametrize("tools", [False, True])
def test_legacy_credential_metadata_is_preserved(monkeypatch: pytest.MonkeyPatch, db: Session, tools: bool) -> None:
    config = TenantLLMService.resolve_model_config(db, {"llm_name": "legacy", "api_key": json.dumps({"api_key": "fixture", "is_tools": tools}), "max_tokens": 0})
    assert config["is_tools"] is tools and config["max_tokens"] == 0
    assert config["api_key"] == "fixture"
