from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from api.db.db_models import Knowledgebase
from api.db.services import dialog_service


def test_get_models_uses_kb_owner_for_shared_embedding(monkeypatch):
    # 真实模型实例（不落库）：get_models 内部会走 ensure_same_embedding_model，
    # 其 Sequence[Knowledgebase] 注解受 beartype 运行时校验，SimpleNamespace 过不了。
    kb = Knowledgebase(
        tenant_id="owner-tenant",
        tenant_embd_id=None,
        embd_id="bge-m3",
    )
    dialog = SimpleNamespace(
        kb_ids=["kb-1"],
        tenant_id="member-tenant",
        rerank_id=None,
        prompt_config={},
    )
    resolved_models = []
    bundles = []

    monkeypatch.setattr(
        dialog_service.KnowledgebaseService,
        "get_by_ids",
        lambda _db, _kb_ids: [kb],
    )
    monkeypatch.setattr(
        dialog_service,
        "_resolve_dialog_primary_model_config",
        lambda _db, _dialog: {"model": "chat"},
    )

    def resolve_model_config(_db, tenant_id, tenant_model_id, model_type, model_name):
        resolved_models.append((tenant_id, tenant_model_id, model_type, model_name))
        return {"model": model_name}

    def build_bundle(_db, tenant_id, model_config):
        bundles.append((tenant_id, model_config))
        return object()

    monkeypatch.setattr(dialog_service, "_resolve_model_config", resolve_model_config)
    monkeypatch.setattr(dialog_service, "LLMBundle", build_bundle)

    dialog_service.get_models(object(), dialog)

    assert resolved_models[0][0] == "owner-tenant"
    assert bundles[0][0] == "owner-tenant"
    assert bundles[1][0] == "member-tenant"


@pytest.mark.parametrize("surface", ["ask", "async_ask", "mindmap"])
async def test_shared_embedding_is_resolved_and_built_for_dataset_owner(db: Session, async_db: AsyncSession, monkeypatch: pytest.MonkeyPatch, surface: str) -> None:
    kb = Knowledgebase(id="kb-1", name="Shared", tenant_id="owner-tenant", tenant_embd_id=7, embd_id="bge-m3", parser_id="naive")
    resolved: list[tuple[str, int | None, str, str]] = []
    bundles: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(dialog_service.KnowledgebaseService, "accessible", lambda *_args: True)
    monkeypatch.setattr(dialog_service.KnowledgebaseService, "get_by_ids", lambda *_args: [kb])

    def resolve(_db: Session, tenant_id: str, model_id: int | None, model_type: str, name: str) -> dict[str, Any]:
        resolved.append((tenant_id, model_id, model_type, name))
        return {"id": model_id, "mdl_type": model_type}

    class BundleReached(Exception):
        pass

    def build(_db: Session, tenant_id: str, config: dict[str, Any]) -> Any:
        bundles.append((tenant_id, config))
        raise BundleReached

    monkeypatch.setattr(dialog_service, "_resolve_model_config", resolve)
    monkeypatch.setattr(dialog_service, "LLMBundle", build)
    monkeypatch.setattr(dialog_service, "detached_bundle", build)
    with pytest.raises(BundleReached):
        if surface == "ask":
            next(dialog_service.ask(db, "question", [kb.id], "member-tenant"))
        elif surface == "async_ask":
            await anext(dialog_service.async_ask(async_db, "question", [kb.id], "member-tenant"))
        else:
            await dialog_service.gen_mindmap(async_db, "question", [kb.id], "member-tenant")
    assert resolved == [("owner-tenant", 7, "embedding", "bge-m3")]
    assert bundles == [("owner-tenant", {"id": 7, "mdl_type": "embedding"})]
