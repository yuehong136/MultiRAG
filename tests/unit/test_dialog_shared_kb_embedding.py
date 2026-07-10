import inspect
from types import SimpleNamespace

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


def test_ask_paths_use_kb_owner_for_embedding_bundle():
    for fn in (dialog_service.ask, dialog_service.async_ask):
        source = inspect.getsource(fn)
        assert "embd_owner_tenant_id = kbs[0].tenant_id if kbs else tenant_id" in source
        assert "embd_owner_tenant_id,\n        kbs[0].tenant_embd_id" in source
        assert "LLMBundle(db, embd_owner_tenant_id, embd_model_config)" in source


def test_gen_mindmap_uses_kb_owner_for_embedding_bundle():
    source = inspect.getsource(dialog_service.gen_mindmap)

    assert "embd_owner_tenant_id = kbs[0].tenant_id" in source
    assert "embd_owner_tenant_id,\n            kbs[0].tenant_embd_id" in source
    assert "LLMBundle(s, embd_owner_tenant_id, embd_model_config)" in source
