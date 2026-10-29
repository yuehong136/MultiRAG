"""Catalog and remote metadata remain declarations with accurate capabilities."""

import pytest

import core.llm as llm
from api.db.model_contract import ConnectionCandidate, DiscoveryInput
from api.db.services.model_provider_service import candidate_models, catalog, discover_models
from common import settings
from core.llm.model_meta import DiscoveryError, ModelMetadata


async def test_static_multi_capability_features_explicit_empty_and_legal_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        settings,
        "FACTORY_LLM_INFOS",
        [
            {
                "name": "static",
                "llm": [
                    {"llm_name": "multi", "mdl_type": ["chat", "image2text"], "max_tokens": 0, "features": [], "is_tools": True},
                    {"llm_name": "thinking", "mdl_type": "chat", "max_tokens": 0, "is_tools": True, "thinking": True},
                    {"llm_name": "merged", "mdl_type": "chat", "max_tokens": 0, "is_tools": True},
                    {"llm_name": "merged", "mdl_type": "embedding", "max_tokens": 0},
                ],
            }
        ],
    )
    items = {item.model_name: item for item in await discover_models("static", DiscoveryInput())}
    assert items["multi"].model_types == ["chat", "image2text"] and items["multi"].features == [] and items["multi"].is_tools is False
    assert items["multi"].max_tokens == 0
    assert items["thinking"].features == ["is_tools", "thinking"]
    assert items["merged"].model_types == ["chat", "embedding"] and items["merged"].is_tools is True
    assert len(catalog("static")) == 3
    assert next(item for item in catalog("static") if item.model_name == "multi").is_tools is False
    assert all("id" not in item.model_dump() and "status" not in item.model_dump() for item in items.values())


async def test_remote_replaces_static_metadata_and_failure_is_not_static_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "FACTORY_LLM_INFOS", [{"name": "remote", "llm": [{"llm_name": "same", "mdl_type": "chat", "max_tokens": 8192, "is_tools": True}]}])

    class Remote:
        def __init__(self, _key: str, _base: str) -> None:
            pass

        async def get_model_list(self) -> list[ModelMetadata]:
            return [ModelMetadata("same", ["embedding"], 0, [], "Fresh name")]

    monkeypatch.setattr(llm, "ModelMeta", {"remote": Remote})
    item = (await discover_models("remote", DiscoveryInput()))[0]
    assert item.model_types == ["embedding"] and item.max_tokens == 0 and item.is_tools is False and item.source == "remote"

    async def fail(_self: Remote) -> list[ModelMetadata]:
        raise DiscoveryError("Remote failure")

    monkeypatch.setattr(Remote, "get_model_list", fail)
    with pytest.raises(DiscoveryError):
        await discover_models("remote", DiscoveryInput())


def test_batch_custom_models_conflict_and_explicit_empty_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "FACTORY_LLM_INFOS", [{"name": "Ollama", "llm": [{"llm_name": "catalog", "mdl_type": "chat", "max_tokens": 0}]}])
    custom = {"model_name": "remote", "model_types": ["chat", "image2text"], "max_tokens": 0}
    assert [item.model_name for item in candidate_models("Ollama", ConnectionCandidate(api_key="", model_ids=[], custom_models=[custom]))] == ["remote"]
    assert [item.model_name for item in candidate_models("Ollama", ConnectionCandidate(api_key="", custom_models=[custom]))] == ["remote", "catalog"]
    with pytest.raises(ValueError, match="duplicates"):
        candidate_models("Ollama", ConnectionCandidate(api_key="", custom_models=[{**custom, "model_name": "catalog"}]))
