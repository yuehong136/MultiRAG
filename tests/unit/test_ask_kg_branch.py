"""ask/async_ask KG 分支 correctness（§11 Phase 2 任务 6）。

钉两点：空 KB 列表不得被 all(空)=True 误判成 KG（应走 Dealer 检索）；
KG 分支按全库统一约定位置传参调用 KGSearch.retrieval
（旧代码把 Dealer kwargs 传给 KGSearch.retrieval 必 TypeError）。
哨兵异常在检索点截断生成器，不进入下游 LLM 流式。
"""

import types

import pytest

from api.db.services import dialog_service
from api.db.services.dialog_service import ask, async_ask
from api.db.services.knowledgebase_service import KnowledgebaseService
from common import settings


class _Sentinel(Exception):
    def __init__(self, which, captured=None):
        super().__init__(which)
        self.which = which
        self.captured = captured


class _FakeBundle:
    def __init__(self, db, tenant_id, model_config, **kwargs):
        self.db = db
        self.max_length = 1024


def _fake_kb(parser_id="naive"):
    return types.SimpleNamespace(
        id="kb1",
        tenant_id="t1",
        name="kb",
        tenant_embd_id="emb@F",
        embd_id="emb",
        parser_id=parser_id,
    )


@pytest.fixture
def ask_stubs(monkeypatch):
    monkeypatch.setattr(KnowledgebaseService, "ensure_same_embedding_model", classmethod(lambda cls, kbs: None))
    monkeypatch.setattr(dialog_service, "_resolve_model_config", lambda s, tid, inst, t, name: {"llm_name": "emb-m"})
    monkeypatch.setattr(dialog_service, "get_model_config_by_type_and_name", lambda s, tid, t, name: {"llm_name": "chat-m"})
    monkeypatch.setattr(dialog_service, "LLMBundle", _FakeBundle)
    monkeypatch.setattr(dialog_service, "label_question", lambda s, q, kbs: [])

    async def _dealer_retrieval(**kwargs):
        raise _Sentinel("dealer")

    async def _kg_retrieval(*args, **kwargs):
        raise _Sentinel("kg", captured=args)

    monkeypatch.setattr(settings, "retriever", types.SimpleNamespace(retrieval=_dealer_retrieval))
    monkeypatch.setattr(settings, "kg_retriever", types.SimpleNamespace(retrieval=_kg_retrieval))


async def test_async_ask_empty_kbs_use_dealer_not_kg(monkeypatch, db, ask_stubs):
    monkeypatch.setattr(KnowledgebaseService, "get_by_ids", classmethod(lambda cls, s, ids, cols=None: []))

    with pytest.raises(_Sentinel) as exc:
        async for _ in async_ask(db, "q", [], "t1"):
            pass

    assert exc.value.which == "dealer"


async def test_async_ask_kg_branch_uses_positional_convention(monkeypatch, db, ask_stubs):
    kb = _fake_kb(parser_id=dialog_service.ParserType.KG)
    monkeypatch.setattr(KnowledgebaseService, "get_by_ids", classmethod(lambda cls, s, ids, cols=None: [kb]))

    with pytest.raises(_Sentinel) as exc:
        async for _ in async_ask(db, "q", ["kb1"], "t1"):
            pass

    assert exc.value.which == "kg"
    question, tenant_ids, kb_ids, emb_mdl, llm = exc.value.captured
    assert question == "q"
    assert tenant_ids == ["t1"]
    assert kb_ids == ["kb1"]
    assert isinstance(emb_mdl, _FakeBundle)
    assert isinstance(llm, _FakeBundle)


def test_sync_ask_empty_kbs_use_dealer_not_kg(monkeypatch, db, ask_stubs):
    monkeypatch.setattr(KnowledgebaseService, "get_by_ids", classmethod(lambda cls, s, ids, cols=None: []))

    with pytest.raises(_Sentinel) as exc:
        for _ in ask(db, "q", [], "t1"):
            pass

    assert exc.value.which == "dealer"


def test_sync_ask_kg_branch_uses_positional_convention(monkeypatch, db, ask_stubs):
    kb = _fake_kb(parser_id=dialog_service.ParserType.KG)
    monkeypatch.setattr(KnowledgebaseService, "get_by_ids", classmethod(lambda cls, s, ids, cols=None: [kb]))

    with pytest.raises(_Sentinel) as exc:
        for _ in ask(db, "q", ["kb1"], "t1"):
            pass

    assert exc.value.which == "kg"
    assert exc.value.captured[0] == "q"
    assert exc.value.captured[2] == ["kb1"]
