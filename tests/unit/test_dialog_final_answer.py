"""async_ask final SSE 事件必须携带 decorate_answer 后的全文。

回归钉板：曾有 `final["answer"] = ""` 把装饰后答案（含 ##N$$ 引用标记、
按引用裁剪的 doc_aggs）从 final 事件里抹掉——客户端拿到为装饰版答案
构建的引用，却只见过裸流式文本，引用永远对不上。async_chat 的流式
final 事件是同一模式，共用该契约。
"""

import types

import pytest

from api.db.services import dialog_service
from api.db.services.dialog_service import async_ask
from api.db.services.knowledgebase_service import KnowledgebaseService
from common import settings

_DECORATED = "Answer about pandas ##0$$ and more detail."


class _FakeBundle:
    def __init__(self, db, tenant_id, model_config, **kwargs):
        self.db = db
        self.max_length = 1024

    async def async_chat_streamly_delta(self, system, history, gen_conf):
        # 累积式流：后一 chunk 是前一 chunk 的前缀扩展
        yield "Answer about pandas"
        yield "Answer about pandas and more detail."


@pytest.fixture
def final_answer_stubs(monkeypatch):
    monkeypatch.setattr(KnowledgebaseService, "get_by_ids", classmethod(lambda cls, s, ids, cols=None: []))
    monkeypatch.setattr(KnowledgebaseService, "ensure_same_embedding_model", classmethod(lambda cls, kbs: None))
    monkeypatch.setattr(dialog_service, "_resolve_model_config", lambda s, tid, inst, t, name: {"llm_name": "emb-m"})
    monkeypatch.setattr(dialog_service, "get_model_config_by_type_and_name", lambda s, tid, t, name: {"llm_name": "chat-m"})
    monkeypatch.setattr(dialog_service, "LLMBundle", _FakeBundle)
    monkeypatch.setattr(dialog_service, "label_question", lambda s, q, kbs: [])
    monkeypatch.setattr(dialog_service, "kb_prompt", lambda kbinfos, max_tokens: [])

    async def _retrieval(**kwargs):
        return {"total": 0, "chunks": [], "doc_aggs": []}

    def _insert_citations(answer, chunks, vectors, embd_mdl, tkweight=0.7, vtweight=0.3):
        return _DECORATED, set()

    monkeypatch.setattr(settings, "retriever", types.SimpleNamespace(retrieval=_retrieval, insert_citations=_insert_citations))


async def test_async_ask_final_event_carries_decorated_answer(async_db, final_answer_stubs):
    events = [event async for event in async_ask(async_db, "q", [], "t1")]

    assert events, "async_ask 应产出流式事件"
    final_events = [event for event in events if event.get("final")]
    assert len(final_events) == 1
    assert final_events[0] is events[-1]
    assert final_events[0]["answer"] == _DECORATED
    assert all(not event.get("final") for event in events[:-1])
