"""多知识库 embedding 一致性校验：service 权威 helper + chunk 检索路由契约。

`KnowledgebaseService.ensure_same_embedding_model` 是所有多知识库联合检索/对话
入口的唯一校验点（dialog_service 四路径、dialog_app、sdk/doc、chat_api、chunk_app）。
判定键：tenant_embd_id（provider 实例级）；缺失时回退 embd_id 并剥 @factory 后缀。
"""

from types import SimpleNamespace

import pytest

from api.db.db_models import Knowledgebase
from api.db.services import tenant_llm_service
from api.db.services.knowledgebase_service import EmbeddingModelMismatchError, KnowledgebaseService
from api.db.services.user_service import UserTenantService
from common.constants import RetCode


def _kb(tenant_embd_id=None, embd_id="", **kw):
    defaults = {"id": "kb-1", "name": "ds", "tenant_id": "tenant-unit"}
    defaults.update(kw)
    return Knowledgebase(tenant_embd_id=tenant_embd_id, embd_id=embd_id, **defaults)


def test_same_tenant_embd_id_passes_even_with_different_names():
    kbs = [_kb(tenant_embd_id=7, embd_id="bge-m3"), _kb(tenant_embd_id=7, embd_id="alias-name")]
    KnowledgebaseService.ensure_same_embedding_model(kbs)


def test_different_tenant_embd_id_raises():
    kbs = [_kb(tenant_embd_id=7), _kb(tenant_embd_id=8)]
    with pytest.raises(EmbeddingModelMismatchError):
        KnowledgebaseService.ensure_same_embedding_model(kbs)


def test_fallback_same_model_name_across_factories_passes(monkeypatch):
    # 宽松语义：同名模型挂不同 factory 视为同一向量空间（与上游一致）
    monkeypatch.setattr(tenant_llm_service.settings, "FACTORY_LLM_INFOS", [{"name": "BAAI"}, {"name": "Ollama"}], raising=False)
    kbs = [_kb(embd_id="bge-m3@BAAI"), _kb(embd_id="bge-m3@Ollama")]
    KnowledgebaseService.ensure_same_embedding_model(kbs)


def test_fallback_different_model_names_raise_with_detail():
    kbs = [_kb(embd_id="bge-m3"), _kb(embd_id="text-embedding-3")]
    with pytest.raises(EmbeddingModelMismatchError, match="bge-m3") as exc_info:
        KnowledgebaseService.ensure_same_embedding_model(kbs)
    assert "text-embedding-3" in str(exc_info.value)


def test_empty_and_single_kb_pass():
    KnowledgebaseService.ensure_same_embedding_model([])
    KnowledgebaseService.ensure_same_embedding_model([_kb(embd_id="bge-m3")])


def test_chunk_retrieval_test_rejects_mismatched_embeddings(client, monkeypatch):
    """POST /v1/chunk/retrieval_test：embedding 不一致的多知识库必须拒绝（DATA_ERROR）。"""
    monkeypatch.setattr(UserTenantService, "query", lambda *_a, **_k: [SimpleNamespace(tenant_id="tenant-unit")])
    monkeypatch.setattr(KnowledgebaseService, "query", lambda *_a, **_k: [object()])
    monkeypatch.setattr(
        KnowledgebaseService,
        "get_by_ids",
        lambda *_a, **_k: [
            _kb(id="kb-a", embd_id="bge-m3"),
            _kb(id="kb-b", embd_id="text-embedding-3"),
        ],
    )

    res = client.post("/v1/chunk/retrieval_test", json={"kb_ids": ["kb-a", "kb-b"], "question": "什么是机器学习？"})

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["retcode"] == RetCode.DATA_ERROR, body
    assert "different embedding models" in body["retmsg"], body
