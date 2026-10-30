"""OpenAI catalog additions must reach the registered tenant model endpoint."""

from contextlib import nullcontext
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from api.apps import llm_app
from api.db import init_data
from api.db.db_models import LLM, TenantLLM
from api.db.services.llm_service import LLMService, get_init_tenant_llm
from api.db.services.tenant_llm_service import LLMFactoriesService, TenantLLMService
from common import settings
from common.app_config import get_factory_llm_infos

CHAT_MODELS = {
    "gpt-6.1-sol": (1050000, False),
    "gpt-6-luna": (1050000, False),
    "gpt-5.6-sol": (1050000, True),
    "gpt-5.5": (1050000, True),
    "gpt-5.4": (1050000, True),
    "gpt-5.4-mini": (400000, True),
}


@pytest.fixture
def openai_catalog(monkeypatch: pytest.MonkeyPatch, db: Session) -> list[LLM]:
    factory = next(item for item in get_factory_llm_infos() if item["name"] == "OpenAI")
    rows: list[LLM] = []

    monkeypatch.setattr(settings, "FACTORY_LLM_INFOS", [factory])
    monkeypatch.setattr(db, "begin_nested", nullcontext)
    monkeypatch.setattr(db, "execute", lambda *args: None)
    monkeypatch.setattr(db, "flush", lambda: None)
    monkeypatch.setattr(db, "commit", lambda: None)

    def capture_model(row: Any) -> None:
        if isinstance(row, LLM):
            row.status = "1"
            rows.append(row)

    monkeypatch.setattr(db, "add", capture_model)
    monkeypatch.setattr(LLMFactoriesService, "filter_delete", lambda *args: None)
    monkeypatch.setattr(LLMService, "filter_delete", lambda *args: None)
    monkeypatch.setattr(TenantLLMService, "filter_delete", lambda *args: None)
    monkeypatch.setattr(TenantLLMService, "filter_update", lambda *args: None)
    monkeypatch.setattr(init_data.TenantService, "filter_update", lambda *args: None)
    monkeypatch.setattr(init_data.DocumentService, "get_all_kb_doc_count", lambda *args: {})
    monkeypatch.setattr(init_data.KnowledgebaseService, "get_all_ids", lambda *args: [])
    init_data.init_llm_factory(db)
    for name, (context, tools) in CHAT_MODELS.items():
        matches = [row for row in rows if row.llm_name == name]
        assert len(matches) == 1
        row = matches[0]
        assert (row.fid, row.mdl_type, row.max_tokens, row.is_tools) == ("OpenAI", "chat", context, tools)
        assert "IMAGE2TEXT" in row.tags
    monkeypatch.setattr(LLMService, "get_all", lambda session: rows)
    monkeypatch.setattr(TenantLLMService, "ensure_mineru_from_env", lambda *args: None)
    monkeypatch.setattr(TenantLLMService, "ensure_opendataloader_from_env", lambda *args: None)
    return rows


@pytest.mark.parametrize("mdl_type", [None, "chat", "embedding"])
def test_explicit_openai_registrations_reach_legacy_list(monkeypatch: pytest.MonkeyPatch, db: Session, client: TestClient, openai_catalog: list[LLM], mdl_type: str | None) -> None:
    tenants = [
        TenantLLM(id=index, tenant_id="user-unit", llm_factory="OpenAI", llm_name=row.llm_name, mdl_type=row.mdl_type, api_key="fixture-key", max_tokens=row.max_tokens, status="1")
        for index, row in enumerate(openai_catalog, 1)
    ]
    tenants.append(TenantLLM(id=999, tenant_id="user-unit", llm_factory="Custom", llm_name="tenant-only", mdl_type="chat", api_key="fixture-key", status="1"))
    openai_catalog.append(LLM(fid="Builtin", llm_name="flag-embedding", mdl_type="embedding", status="1"))

    def tenant_query(session: Session, **filters: Any) -> list[TenantLLM]:
        assert filters == {"tenant_id": "user-unit"}
        return tenants

    monkeypatch.setattr(TenantLLMService, "query", tenant_query)
    response = client.get("/v1/llm/list", params={} if mdl_type is None else {"mdl_type": mdl_type})
    assert response.status_code == 200
    body = response.json()
    assert body["retcode"] == 0
    data = body["data"]
    if mdl_type != "embedding":
        models = {row["llm_name"]: row for row in data["OpenAI"]}
        for name, (context, tools) in CHAT_MODELS.items():
            assert models[name]["available"] is True
            assert models[name]["id"] in {row.id for row in tenants if row.llm_name == name}
            assert models[name]["max_tokens"] == context
            assert models[name]["is_tools"] is tools
        assert data["Custom"][0]["llm_name"] == "tenant-only"
    if mdl_type == "chat":
        assert "Builtin" not in data
    else:
        assert data["Builtin"][0]["available"] is True
    assert all(mdl_type is None or mdl_type in row["mdl_type"] for models in data.values() for row in models)
    assert "api_key" not in response.text


def test_catalog_does_not_enable_unconfigured_openai(monkeypatch: pytest.MonkeyPatch, client: TestClient, openai_catalog: list[LLM]) -> None:
    monkeypatch.setattr(TenantLLMService, "query", lambda *args, **kwargs: [])
    response = client.get("/v1/llm/list")
    assert response.status_code == 200
    assert response.json()["retcode"] == 0
    assert "OpenAI" not in response.json()["data"]


def test_catalog_requires_authentication(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    client.app.dependency_overrides.pop(llm_app.manager, None)

    def forbidden_read(*args: Any, **kwargs: Any) -> None:
        pytest.fail("unauthenticated catalog request read model rows")

    monkeypatch.setattr(LLMService, "get_all", forbidden_read)
    monkeypatch.setattr(TenantLLMService, "query", forbidden_read)
    assert client.get("/v1/llm/list").status_code == 401


def test_default_seed_contains_only_configured_models_and_preserves_zero(monkeypatch: pytest.MonkeyPatch, db: Session) -> None:
    for name in ("CHAT_CFG", "EMBEDDING_CFG", "ASR_CFG", "IMAGE2TEXT_CFG", "RERANK_CFG"):
        monkeypatch.setattr(settings, name, {"model": "", "factory": "OpenAI"})
    monkeypatch.setattr(settings, "CHAT_CFG", {"model": "gpt-6.1-sol@OpenAI", "factory": "OpenAI", "api_key": "fixture", "base_url": "", "max_tokens": 0})
    rows = get_init_tenant_llm(db, "user-unit")
    assert len(rows) == 1
    assert rows[0]["llm_name"] == "gpt-6.1-sol" and rows[0]["max_tokens"] == 0
