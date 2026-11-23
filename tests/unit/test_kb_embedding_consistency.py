"""Registered embedding identities may share a vector space across connections."""

from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from api.db.db_models import Knowledgebase
from api.db.services.knowledgebase_service import EmbeddingModelMismatchError, KnowledgebaseService


def _kb(tenant_embd_id: str | None = None, embd_id: str = "", tenant: str = "tenant-unit") -> Knowledgebase:
    return Knowledgebase(tenant_embd_id=tenant_embd_id, embd_id=embd_id, tenant_id=tenant)


def test_same_base_model_across_registered_tenants_does_not_load_credentials(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    first, second = uuid4().hex, uuid4().hex

    def model_names(statement: object) -> list[tuple[str, str, str]]:
        query = str(statement)
        assert "api_key" not in query
        assert "(usr_ai.t_ai_tenant_model_providers.tenant_id, usr_ai.t_ai_tenant_models.id) IN" in query
        return [("tenant-unit", first, "bge-m3"), ("shared", second, "bge-m3")]

    monkeypatch.setattr(db, "execute", model_names)
    KnowledgebaseService.ensure_same_embedding_model([_kb(first, "stale"), _kb(second, tenant="shared")], db=db)
    with pytest.raises(EmbeddingModelMismatchError):
        KnowledgebaseService.ensure_same_embedding_model([_kb(first), _kb(second, tenant="unrelated")], db=db)


def test_known_distinct_models_are_rejected(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    first, second = uuid4().hex, uuid4().hex
    monkeypatch.setattr(db, "execute", lambda _: [("tenant-unit", first, "bge-m3"), ("tenant-unit", second, "text-embedding-3")])
    with pytest.raises(EmbeddingModelMismatchError):
        KnowledgebaseService.ensure_same_embedding_model([_kb(first), _kb(second)], db=db)


def test_base_name_for_builtin_without_tenant_registration(db: Session) -> None:
    KnowledgebaseService.ensure_same_embedding_model([_kb(embd_id="bge-m3@Builtin"), _kb(embd_id="bge-m3")], db=db)
    with pytest.raises(EmbeddingModelMismatchError, match="bge-m3") as exc_info:
        KnowledgebaseService.ensure_same_embedding_model([_kb(embd_id="bge-m3"), _kb(embd_id="text-embedding-3")], db=db)
    assert "text-embedding-3" in str(exc_info.value)


def test_empty_and_single_kb_pass(db: Session) -> None:
    KnowledgebaseService.ensure_same_embedding_model([], db=db)
    KnowledgebaseService.ensure_same_embedding_model([_kb(embd_id="bge-m3")], db=db)
