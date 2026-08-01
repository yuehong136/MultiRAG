"""async_ask final SSE 事件必须携带 decorate_answer 后的全文。

回归钉板：曾有 `final["answer"] = ""` 把装饰后答案（含 ##N$$ 引用标记、
按引用裁剪的 doc_aggs）从 final 事件里抹掉——客户端拿到为装饰版答案
构建的引用，却只见过裸流式文本，引用永远对不上。async_chat 的流式
final 事件是同一模式，共用该契约。

既然 final 事件带的是全文，全文里的 think 标签就必须是规范的一对——
``_extract_visible_answer`` 的钉板也收在本文件。
"""

import types

import pytest

from api.db.services import dialog_service
from api.db.services.dialog_service import _extract_visible_answer, async_ask
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


async def test_async_ask_final_event_normalizes_think_tags(async_db, final_answer_stubs, monkeypatch):
    """模型重复吐 think 标签时，final 事件只留规范的一对。"""

    class _RepeatedThinkBundle(_FakeBundle):
        async def async_chat_streamly_delta(self, system, history, gen_conf):
            yield "<think>step one</think>"
            yield "<think>step one</think><think>step two</think>visible answer"

    monkeypatch.setattr(dialog_service, "LLMBundle", _RepeatedThinkBundle)
    monkeypatch.setattr(settings.retriever, "insert_citations", lambda answer, *args, **kwargs: (answer, set()))

    events = [event async for event in async_ask(async_db, "q", [], "t1")]

    final_events = [event for event in events if event.get("final")]
    assert len(final_events) == 1
    assert final_events[0]["answer"] == "<think>step onestep two</think>visible answer"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # 无 think 标签：原样透出
        ("plain answer", "plain answer"),
        # 规范的一对：保持不变
        ("<think>reasoning</think>answer", "<think>reasoning</think>answer"),
        # 未闭合的 <think>：剥掉标签，内容仍作正文，不吞答案
        ("<think>truncated reasoning", "truncated reasoning"),
        # 重复标签：按最后一个 </think> 切分，思考段合并成一段
        ("<think>a</think><think>b</think>answer", "<think>ab</think>answer"),
        # 空思考段：只留正文，不留空的 think 壳
        ("<think></think>answer", "answer"),
        ("<think>   </think>answer", "answer"),
        # 正文里再出现游离标签：一并剥掉
        ("<think>r</think>ans<think>tail", "<think>r</think>anstail"),
        ("", ""),
    ],
)
def test_extract_visible_answer(text, expected):
    assert _extract_visible_answer(text) == expected
