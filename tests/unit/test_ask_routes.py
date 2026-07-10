"""async_ask 服务与四条 ask 路由契约（§11 Phase 2 任务 7）。

服务层：AsyncSession 化后 bundles 剥离 facade；citation finalize（decorate_answer）
经 asyncio.to_thread 在工作线程执行（非事件循环线程）；非空 retrieval 桩覆盖
citation 真分支（空 Milvus 冒烟只走空分支，不足以验收）。
路由层：REST/SDK 的 code/message 帧与 conversation 的 legacy retcode/retmsg 帧
两套 SSE 契约分别钉住。
"""

import json
import sys
import threading
import types

import pytest

from api.db.services import dialog_service
from api.db.services.dialog_service import async_ask
from api.db.services.knowledgebase_service import KnowledgebaseService
from common import settings


def _route_module(name: str):
    return sys.modules[name]


def _sse_frames(text: str):
    frames = []
    for chunk in text.split("\n\n"):
        chunk = chunk.strip()
        if chunk.startswith("data:"):
            frames.append(json.loads(chunk[len("data:") :]))
    return frames


class _FakeBundle:
    instances: list["_FakeBundle"] = []

    def __init__(self, db, tenant_id, model_config, **kwargs):
        self.db = db
        self.max_length = 512
        type(self).instances.append(self)

    def async_chat_streamly_delta(self, prompt, msg, conf):
        async def _gen():
            yield "你"
            yield "你好"

        return _gen()


def _fake_kb():
    return types.SimpleNamespace(
        id="kb1",
        tenant_id="t1",
        name="kb",
        tenant_embd_id="emb@F",
        embd_id="emb",
        parser_id="naive",
        chunk_num=3,
    )


async def test_async_ask_strips_bundles_and_finalizes_citation_off_loop(monkeypatch, async_db):
    _FakeBundle.instances = []
    kb = _fake_kb()
    captured = {}

    monkeypatch.setattr(KnowledgebaseService, "get_by_ids", classmethod(lambda cls, s, ids, cols=None: [kb]))
    monkeypatch.setattr(KnowledgebaseService, "ensure_same_embedding_model", classmethod(lambda cls, kbs: None))
    monkeypatch.setattr(dialog_service, "_resolve_model_config", lambda s, tid, inst, t, name: {"llm_name": "emb-m"})
    monkeypatch.setattr(dialog_service, "get_model_config_by_type_and_name", lambda s, tid, t, name: {"llm_name": "chat-m"})
    monkeypatch.setattr(dialog_service, "LLMBundle", _FakeBundle)
    monkeypatch.setattr(dialog_service, "label_question", lambda s, q, kbs: [])
    monkeypatch.setattr(dialog_service, "chunks_format", lambda refs: refs["chunks"])

    async def _retrieval(**kwargs):
        return {
            "chunks": [{"content_ltks": "tok", "vector": [0.1], "doc_id": "d1"}],
            "doc_aggs": [{"doc_id": "d1", "doc_name": "doc"}],
        }

    def _insert_citations(answer, chunks, vectors, emb_mdl, tkweight=0.7, vtweight=0.3):
        captured["thread"] = threading.current_thread()
        return answer, {0}

    monkeypatch.setattr(settings, "retriever", types.SimpleNamespace(retrieval=_retrieval, insert_citations=_insert_citations))

    frames = []
    async for frame in async_ask(async_db, "q", ["kb1"], "t1"):
        frames.append(frame)

    assert frames[-1]["final"] is True
    assert frames[-1]["reference"]["doc_aggs"] == [{"doc_id": "d1", "doc_name": "doc"}]
    assert "".join(f["answer"] for f in frames[:-1]) == "你好"
    # citation finalize 经 to_thread：insert_citations 必须在工作线程执行
    assert captured["thread"] is not threading.main_thread()
    # 全部 bundle 剥离 facade（AGENTS.md 规约）
    assert _FakeBundle.instances
    assert all(bundle.db is None for bundle in _FakeBundle.instances)


# ---------------------------------------------------------------------------
# 路由层（async_ask 打桩，两套 SSE 契约分别钉板）
# ---------------------------------------------------------------------------


def _fake_async_ask(answers):
    async def _gen(db, question, kb_ids, tenant_id, chat_llm_name=None, search_config=None):
        for answer in answers:
            yield answer

    return _gen


@pytest.fixture
def ask_route_stubs(monkeypatch, client):
    answers = [{"answer": "你好", "reference": {}, "final": False}]
    for mod_name in ("api.apps.sdk.session", "api.apps.restful_apis.chat", "api.apps.conversation"):
        monkeypatch.setattr(_route_module(mod_name), "async_ask", _fake_async_ask(answers))
    kb = _fake_kb()
    monkeypatch.setattr(KnowledgebaseService, "accessible", classmethod(lambda cls, s, kb_id, tid: True))
    monkeypatch.setattr(KnowledgebaseService, "query", classmethod(lambda cls, s, **kw: [kb]))


@pytest.fixture
def auth_client(client):
    from api.utils.api_utils import async_beta_token_required, async_token_required, beta_token_required, token_required

    client.app.dependency_overrides[token_required] = lambda: "tenant-unit"
    client.app.dependency_overrides[async_token_required] = lambda: "tenant-unit"
    client.app.dependency_overrides[beta_token_required] = lambda: "tenant-unit"
    client.app.dependency_overrides[async_beta_token_required] = lambda: "tenant-unit"
    return client


def test_chat_api_ask_sse_code_frames(auth_client, ask_route_stubs):
    resp = auth_client.post("/api/v1/chats/ask", json={"question": "q", "kb_ids": ["kb1"]})

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    frames = _sse_frames(resp.text)
    assert frames[0] == {"code": 0, "message": "", "data": {"answer": "你好", "reference": {}, "final": False}}
    assert frames[-1] == {"code": 0, "message": "", "data": True}


def test_sdk_ask_about_sse_code_frames(auth_client, ask_route_stubs):
    resp = auth_client.post("/api/v1/sessions/ask", json={"question": "q", "dataset_ids": ["kb1"]})

    assert resp.status_code == 200
    frames = _sse_frames(resp.text)
    assert frames[0] == {"code": 0, "message": "", "data": {"answer": "你好", "reference": {}, "final": False}}
    assert frames[-1] == {"code": 0, "message": "", "data": True}


def test_searchbot_ask_embedded_sse_code_frames(auth_client, ask_route_stubs):
    resp = auth_client.post("/api/v1/searchbots/ask", json={"question": "q", "kb_ids": ["kb1"]})

    assert resp.status_code == 200
    frames = _sse_frames(resp.text)
    assert frames[0] == {"code": 0, "message": "", "data": {"answer": "你好", "reference": {}, "final": False}}
    assert frames[-1] == {"code": 0, "message": "", "data": True}


def test_conversation_ask_legacy_retcode_frames(auth_client, ask_route_stubs):
    """conversation_app 是 legacy retcode/retmsg 契约（与 REST/SDK 的 code/message 不同，钉住差异）。"""
    resp = auth_client.post("/v1/conversation/ask", json={"question": "q", "kb_ids": ["kb1"]})

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    frames = _sse_frames(resp.text)
    assert frames[0] == {"retcode": 0, "retmsg": "", "data": {"answer": "你好", "reference": {}, "final": False}}
    assert frames[-1] == {"retcode": 0, "retmsg": "", "data": True}
