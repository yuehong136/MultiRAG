"""OpenAI catalog additions must reach the registered tenant model endpoint."""

from contextlib import nullcontext
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from api.db import init_data
from api.db.db_models import LLM
from api.db.services.llm_service import LLMService
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


def test_openai_catalog_lists_declarations_without_registration_ids(client: TestClient, openai_catalog: list[LLM]) -> None:
    from api.db.services.model_provider_service import catalog

    models = {item.model_name: item for item in catalog("OpenAI")}
    for name, (context, tools) in CHAT_MODELS.items():
        assert models[name].max_tokens == context and models[name].is_tools is tools
        assert models[name].model_types == ["chat", "image2text"]
        assert "id" not in models[name].model_dump()
    assert client.get("/v1/llm/list").status_code == 404
