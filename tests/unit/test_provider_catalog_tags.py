"""Catalog capability merges remain unique and ordered through public routes."""

from copy import deepcopy
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.db.services import model_provider_service as service
from common import settings


@pytest.fixture
def repeated_catalog(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    declarations = [
        {"llm_name": "shared", "mdl_type": ["chat", "image2text"], "max_tokens": 100, "features": ["thinking"]},
        {"llm_name": "shared", "mdl_type": ["image2text", "embedding"], "max_tokens": 200, "features": ["thinking", "is_tools"]},
        {"llm_name": "shared", "mdl_type": "chat", "max_tokens": 50, "features": ["is_tools"]},
    ]
    monkeypatch.setattr(settings, "FACTORY_LLM_INFOS", [{"name": "fixture", "llm": declarations}])
    return declarations


def test_catalog_merges_repeated_capabilities_without_mutating_source(repeated_catalog: list[dict[str, Any]]) -> None:
    before = deepcopy(repeated_catalog)
    for _ in range(2):
        model = service.catalog("fixture")[0]
        assert model.model_types == ["chat", "image2text", "embedding"]
        assert model.max_tokens == 200
        assert model.is_tools is True
    assert repeated_catalog == before


def test_public_catalog_and_discovery_preserve_stable_unique_tags(client: TestClient, repeated_catalog: list[dict[str, Any]]) -> None:
    response = client.get("/api/v1/providers/fixture/models")
    assert response.status_code == 200
    assert response.json()["code"] == 0
    models = response.json()["data"]["items"]
    assert len(models) == 1
    assert models[0]["model_types"] == ["chat", "image2text", "embedding"]

    response = client.post("/api/v1/providers/fixture/models/discovery", json={})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["code"] == 0
    models = response.json()["data"]["items"]
    assert len(models) == 1
    assert models[0]["model_types"] == ["chat", "image2text", "embedding"]
    assert models[0]["features"] == ["thinking", "is_tools"]
