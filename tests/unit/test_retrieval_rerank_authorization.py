"""Rerank denial precedes credentials, metadata and provider work."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from api.apps.sdk import doc, session
from api.db.db_models import Knowledgebase
from api.db.joint_services import tenant_model_service as models
from api.db.services.llm_service import LLMBundle
from api.utils.api_utils import token_required


def test_sdk_denies_foreign_model_before_metadata_and_any_bundle(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    client.app.dependency_overrides[token_required] = lambda: "owner"
    kb = Knowledgebase(id="kb", tenant_id="owner", name="kb", embd_id="embedding")
    monkeypatch.setattr(doc.KnowledgebaseService, "query", lambda *_a, **_kw: [kb])
    monkeypatch.setattr(doc.KnowledgebaseService, "get_by_ids", lambda *_: [kb])
    lookup = Mock(side_effect=LookupError("not authorized"))
    monkeypatch.setattr(doc, "get_model_config_by_id", lookup)
    bundle, metadata = Mock(), AsyncMock()
    monkeypatch.setattr(doc, "LLMBundle", bundle)
    monkeypatch.setattr(doc, "apply_meta_data_filter", metadata)
    response = client.post(
        "/api/v1/retrieval",
        json={
            "dataset_ids": ["kb"],
            "question": "q",
            "tenant_rerank_id": 42,
            "cross_languages": ["English"],
            "metadata_condition": {"conditions": [{"name": "version", "comparison_operator": "is", "value": "v1"}]},
        },
    )
    assert response.status_code == 200 and response.json()["code"] == 109
    assert lookup.call_args.kwargs["allowed_tenant_ids"] == {"owner"}
    bundle.assert_not_called()
    metadata.assert_not_called()


async def test_embedded_denies_foreign_model_before_saved_search_metadata(async_db: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    kb = Knowledgebase(id="kb", tenant_id="shared-owner", name="kb", embd_id="embedding")
    monkeypatch.setattr(session.UserTenantService, "query", lambda *_a, **_kw: [SimpleNamespace(tenant_id="shared-owner")])
    monkeypatch.setattr(session.KnowledgebaseService, "query", lambda *_a, **_kw: [kb])
    monkeypatch.setattr(session.KnowledgebaseService, "get_by_ids", lambda *_: [kb])
    monkeypatch.setattr(async_db, "scalar", AsyncMock(return_value=None))
    saved, bundle, translation = Mock(), AsyncMock(), AsyncMock()
    monkeypatch.setattr(session.SearchService, "get_detail", saved)
    monkeypatch.setattr(session, "build_bundle_by_id_async", bundle)
    monkeypatch.setattr(session, "cross_languages", translation)
    result = await session.retrieval_test_embedded(
        session.SearchBotRetrievalTestRequest(kb_id=["kb"], question="q", tenant_rerank_id=42, search_id="search", cross_languages=["English"]), async_db, "user"
    )
    import json

    assert json.loads(result.body)["retcode"] == 109
    saved.assert_not_called()
    bundle.assert_not_called()
    translation.assert_not_called()


async def test_scoped_bundle_uses_request_session_and_model_owner(async_db: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    config = {"tenant_id": "user", "mdl_type": "rerank", "llm_name": "m"}
    lookup = Mock(return_value=config)
    monkeypatch.setattr(models, "get_model_config_by_id", lookup)
    monkeypatch.setattr(models, "db_connection", Mock(side_effect=AssertionError("Request must not open another session")))

    class Bundle(LLMBundle):
        def __init__(self, db: Session, tenant_id: str, model_config: dict[str, Any], **kwargs: Any) -> None:
            self.db = db
            self.tenant_id = tenant_id
            self.model_config = model_config

    monkeypatch.setattr(models, "LLMBundle", Bundle)
    bundle = await models.build_bundle_by_id_async("shared-owner", 42, db=async_db, allowed_tenant_ids={"user", "shared-owner"})
    assert bundle.db is None and bundle.tenant_id == "user"
    assert lookup.call_args.kwargs == {"allowed_tenant_ids": {"user", "shared-owner"}}


def test_empty_scope_rejects_before_query_or_credentials(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    query, credentials = Mock(), Mock()
    monkeypatch.setattr(db, "scalar", query)
    monkeypatch.setattr(models.TenantLLMService, "resolve_model_config", credentials)
    with pytest.raises(LookupError, match="No authorization"):
        models.get_model_config_by_id(db, 42, allowed_tenant_ids=set())
    query.assert_not_called()
    credentials.assert_not_called()
