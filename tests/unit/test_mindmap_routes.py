"""gen_mindmap 服务与三条 mindmap 路由契约（§11 Phase 2 任务 5）。

服务层：AsyncSession 化后全部 LLMBundle 经 run_sync 构造并剥离 facade（db is None）；
路由层：REST 的 code/data 与 legacy retcode/retmsg 两套形状分别钉住。
"""

import sys
import types

import pytest

from api.db.services import dialog_service
from api.db.services.dialog_service import gen_mindmap
from api.db.services.knowledgebase_service import KnowledgebaseService
from common import settings


def _route_module(name: str):
    return sys.modules[name]


class _FakeBundle:
    instances: list["_FakeBundle"] = []

    def __init__(self, db, tenant_id, model_config, **kwargs):
        self.db = db
        self.tenant_id = tenant_id
        type(self).instances.append(self)


class _FakeMindMapExtractor:
    def __init__(self, chat_mdl):
        self.chat_mdl = chat_mdl

    async def __call__(self, contents):
        return types.SimpleNamespace(output={"id": "root", "children": contents})


def _fake_kb():
    return types.SimpleNamespace(
        id="kb1",
        tenant_id="tenant-unit",
        name="kb",
        tenant_embd_id="emb@F",
        embd_id="emb",
        parser_id="naive",
    )


@pytest.fixture
def mindmap_service_stubs(monkeypatch):
    _FakeBundle.instances = []
    kb = _fake_kb()

    async def _fake_retrieval(**kwargs):
        return {"chunks": [{"content_with_weight": "c1"}]}

    monkeypatch.setattr(KnowledgebaseService, "get_by_ids", classmethod(lambda cls, s, ids, cols=None: [kb]))
    monkeypatch.setattr(KnowledgebaseService, "ensure_same_embedding_model", classmethod(lambda cls, kbs: None))
    monkeypatch.setattr(dialog_service, "_resolve_model_config", lambda s, tid, inst, t, name: {"llm_name": "emb-m"})
    monkeypatch.setattr(dialog_service, "get_model_config_by_type_and_name", lambda s, tid, t, name: {"llm_name": name})
    monkeypatch.setattr(dialog_service, "get_tenant_default_model_by_type", lambda s, tid, t: {"llm_name": "chat-m"})
    monkeypatch.setattr(dialog_service, "LLMBundle", _FakeBundle)
    monkeypatch.setattr(dialog_service, "label_question", lambda s, q, kbs: [])
    monkeypatch.setattr(dialog_service, "MindMapExtractor", _FakeMindMapExtractor)
    monkeypatch.setattr(settings, "retriever", types.SimpleNamespace(retrieval=_fake_retrieval))
    return _FakeBundle


async def test_gen_mindmap_strips_all_bundles(async_db, mindmap_service_stubs):
    result = await gen_mindmap(async_db, "q", ["kb1"], "tenant-unit", {"rerank_id": "rk"})

    assert result == {"id": "root", "children": ["c1"]}
    assert len(mindmap_service_stubs.instances) == 3  # embd + chat + rerank
    assert all(bundle.db is None for bundle in mindmap_service_stubs.instances)


async def test_gen_mindmap_without_kb_returns_error(async_db, monkeypatch):
    monkeypatch.setattr(KnowledgebaseService, "get_by_ids", classmethod(lambda cls, s, ids, cols=None: []))

    assert await gen_mindmap(async_db, "q", [], "tenant-unit", {}) == {"error": "No KB selected"}


# ---------------------------------------------------------------------------
# 路由层（gen_mindmap 打桩，锁响应形状）
# ---------------------------------------------------------------------------


@pytest.fixture
def mindmap_route_stubs(monkeypatch, client):
    async def _fake_gen_mindmap(db, question, kb_ids, tenant_id, search_config=None):
        return {"id": "root"}

    for mod_name in ("api.apps.sdk.session", "api.apps.restful_apis.chat", "api.apps.conversation"):
        monkeypatch.setattr(_route_module(mod_name), "gen_mindmap", _fake_gen_mindmap)


@pytest.fixture
def auth_client(client):
    from api.utils.api_utils import async_beta_token_required, async_token_required, beta_token_required, token_required

    client.app.dependency_overrides[token_required] = lambda: "tenant-unit"
    client.app.dependency_overrides[async_token_required] = lambda: "tenant-unit"
    client.app.dependency_overrides[beta_token_required] = lambda: "tenant-unit"
    client.app.dependency_overrides[async_beta_token_required] = lambda: "tenant-unit"
    return client


def test_chat_api_mindmap_shape(auth_client, mindmap_route_stubs):
    resp = auth_client.post("/api/v1/chat/mindmap", json={"question": "q", "kb_ids": ["k"]})

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    assert body["data"] == {"id": "root"}


def test_sdk_searchbot_mindmap_legacy_shape(auth_client, mindmap_route_stubs):
    resp = auth_client.post("/api/v1/searchbots/mindmap", json={"question": "q", "kb_ids": ["k"]})

    assert resp.status_code == 200
    body = resp.json()
    assert body["retcode"] == 0
    assert body["data"] == {"id": "root"}


def test_conversation_mindmap_legacy_shape(auth_client, mindmap_route_stubs):
    resp = auth_client.post("/v1/conversation/mindmap", json={"question": "q", "kb_ids": ["k"]})

    assert resp.status_code == 200
    body = resp.json()
    assert body["retcode"] == 0
    assert body["data"] == {"id": "root"}
