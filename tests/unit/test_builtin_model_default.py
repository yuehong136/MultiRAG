"""Configured TEI defaults have a display identity without a selectable model ID."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.db_models import TenantModel, TenantModelInstance, TenantModelProvider
from api.db.model_contract import DEFAULT_MODEL_FIELDS, MODEL_TYPE_BITS
from api.db.services import model_provider_service
from common import settings


@pytest.mark.parametrize("reference", ["org/embedding", "org/embedding@Builtin", "org/name@revision"])
async def test_unregistered_tei_default_has_readonly_display_identity(monkeypatch: pytest.MonkeyPatch, reference: str) -> None:
    name = reference.removesuffix("@Builtin")
    monkeypatch.setenv("COMPOSE_PROFILES", "milvus,tei-cpu")
    monkeypatch.setenv("TEI_MODEL", name)
    monkeypatch.setattr(settings, "FACTORY_LLM_INFOS", [{"name": "Builtin"}])
    owner = SimpleNamespace(
        **{field: reference if capability == "embedding" else "" for capability, field in DEFAULT_MODEL_FIELDS.items()}, **{"tenant_" + field: None for field in DEFAULT_MODEL_FIELDS.values()}
    )
    db = AsyncSession()
    monkeypatch.setattr(db, "get", AsyncMock(return_value=owner))
    monkeypatch.setattr(model_provider_service, "models_for_owner", AsyncMock(return_value=[]))
    result = await model_provider_service.defaults_for_owner(db, "tenant")
    embedding = next(item for item in result["models"] if item["model_type"] == "embedding")
    assert embedding == {"model_type": "embedding", "model_id": "", "enable": True, "model_provider": "Builtin", "model_instance": "default", "model_name": name}


@pytest.mark.parametrize(("profile", "configured", "reference"), [("milvus", "model", "model"), ("tei-cpu", "other", "model"), ("tei-cpu", "", ""), ("tei-cpu", "model", "model@Other")])
async def test_unmatched_configuration_does_not_claim_builtin_identity(monkeypatch: pytest.MonkeyPatch, profile: str, configured: str, reference: str) -> None:
    monkeypatch.setenv("COMPOSE_PROFILES", profile)
    monkeypatch.setenv("TEI_MODEL", configured)
    monkeypatch.setattr(settings, "FACTORY_LLM_INFOS", [{"name": "Builtin"}, {"name": "Other"}])
    owner = SimpleNamespace(
        **{field: reference if capability == "embedding" else "" for capability, field in DEFAULT_MODEL_FIELDS.items()}, **{"tenant_" + field: None for field in DEFAULT_MODEL_FIELDS.values()}
    )
    db = AsyncSession()
    monkeypatch.setattr(db, "get", AsyncMock(return_value=owner))
    monkeypatch.setattr(model_provider_service, "models_for_owner", AsyncMock(return_value=[]))
    result = await model_provider_service.defaults_for_owner(db, "tenant")
    assert result == {"models": []}


@pytest.mark.parametrize("registered", [False, True])
async def test_registered_default_never_becomes_a_builtin_placeholder(monkeypatch: pytest.MonkeyPatch, registered: bool) -> None:
    monkeypatch.setenv("COMPOSE_PROFILES", "tei-cpu")
    monkeypatch.setenv("TEI_MODEL", "model")
    owner = SimpleNamespace(
        **{field: "model" if capability == "embedding" else "" for capability, field in DEFAULT_MODEL_FIELDS.items()},
        **{"tenant_" + field: "a" * 32 if field == "embd_id" else None for field in DEFAULT_MODEL_FIELDS.values()},
    )
    row = (
        TenantModel(id="a" * 32, model_name="registered", model_type=MODEL_TYPE_BITS["embedding"], status="active", extra={}),
        TenantModelInstance(id="b" * 32, instance_name="local", status="active"),
        TenantModelProvider(id="c" * 32, provider_name="OpenAI", status="active"),
    )
    db = AsyncSession()
    monkeypatch.setattr(db, "get", AsyncMock(return_value=owner))
    monkeypatch.setattr(model_provider_service, "models_for_owner", AsyncMock(return_value=[row] if registered else []))
    result = await model_provider_service.defaults_for_owner(db, "tenant")
    if registered:
        assert result["models"] == [{"model_type": "embedding", "model_id": "a" * 32, "model_provider": "OpenAI", "model_instance": "local", "model_name": "registered", "enable": True}]
    else:
        assert result == {"models": []}
