"""OpenAI catalog additions must reach the registered tenant model endpoint."""

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


@pytest.fixture
def openai_catalog(monkeypatch: pytest.MonkeyPatch, db: Session) -> list[LLM]:
    factory = next(item for item in get_factory_llm_infos() if item["name"] == "OpenAI")
    rows: list[LLM] = []

    def save_model(session: Session, **payload: Any) -> None:
        assert session is db
        rows.append(LLM(status="1", **payload))

    monkeypatch.setattr(settings, "FACTORY_LLM_INFOS", [factory])
    monkeypatch.setattr(LLMFactoriesService, "filter_delete", lambda *args: None)
    monkeypatch.setattr(LLMFactoriesService, "save", lambda *args, **kwargs: None)
    monkeypatch.setattr(LLMService, "filter_delete", lambda *args: None)
    monkeypatch.setattr(LLMService, "save", save_model)
    monkeypatch.setattr(TenantLLMService, "filter_delete", lambda *args: None)
    monkeypatch.setattr(TenantLLMService, "filter_update", lambda *args: None)
    monkeypatch.setattr(init_data.TenantService, "filter_update", lambda *args: None)
    monkeypatch.setattr(init_data.DocumentService, "get_all_kb_doc_count", lambda *args: {})
    monkeypatch.setattr(init_data.KnowledgebaseService, "get_all_ids", lambda *args: [])
    init_data.init_llm_factory(db)
    for name in ("gpt-5.5", "gpt-5.4"):
        matches = [row for row in rows if row.llm_name == name]
        assert len(matches) == 1
        row = matches[0]
        assert (row.fid, row.mdl_type, row.max_tokens, row.tags, row.is_tools) == ("OpenAI", "chat", 400000, "LLM,CHAT,400k,IMAGE2TEXT", True)
    monkeypatch.setattr(LLMService, "get_all", lambda session: rows)
    monkeypatch.setattr(TenantLLMService, "ensure_mineru_from_env", lambda *args: None)
    monkeypatch.setattr(TenantLLMService, "ensure_opendataloader_from_env", lambda *args: None)
    return rows


@pytest.mark.parametrize("mdl_type", [None, "chat", "embedding"])
def test_default_openai_catalog_reaches_registered_list(monkeypatch: pytest.MonkeyPatch, db: Session, client: TestClient, openai_catalog: list[LLM], mdl_type: str | None) -> None:
    config = {"factory": "OpenAI", "api_key": "fixture-key", "base_url": "https://fixture.invalid/v1"}
    for name in ("CHAT_CFG", "EMBEDDING_CFG", "ASR_CFG", "IMAGE2TEXT_CFG", "RERANK_CFG"):
        monkeypatch.setattr(settings, name, config)
    monkeypatch.setattr(settings, "BUILTIN_EMBEDDING_MODELS", [])
    monkeypatch.setattr(LLMService, "query", lambda session, **filters: [row for row in openai_catalog if row.fid == filters["fid"]])
    defaults = get_init_tenant_llm(db, "user-unit")
    tenants = [TenantLLM(id=index, status="1", **row) for index, row in enumerate(defaults, 1)]
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
        for name in ("gpt-5.5", "gpt-5.4"):
            assert models[name]["available"] is True
            assert models[name]["id"] in {row.id for row in tenants if row.llm_name == name}
            assert models[name]["max_tokens"] == 400000
            assert models[name]["is_tools"] is True
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
