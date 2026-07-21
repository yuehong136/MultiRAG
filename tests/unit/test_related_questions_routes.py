"""related_questions 四路由契约（§11 Phase 2 任务 4）。

REST/SDK 的 code/data 与 legacy retcode/retmsg/data 两套响应形状分别钉住；
LLMBundle 打桩带 instances 追踪，AsyncSession 化后断言 facade 剥离（db is None）。
"""

import sys

import pytest

from api.db.services.search_service import SearchService


def _route_module(name: str):
    return sys.modules[name]


class _FakeBundle:
    instances: list["_FakeBundle"] = []

    def __init__(self, db, tenant_id, model_config, **kwargs):
        self.db = db
        self.tenant_id = tenant_id
        type(self).instances.append(self)

    async def async_chat(self, prompt, messages, gen_conf):
        return "1. 相关词一\n2. 相关词二\n非编号行忽略"


@pytest.fixture
def rq_stubs(monkeypatch, client):
    _FakeBundle.instances = []
    for mod_name in ("api.apps.sdk.session", "api.apps.restful_apis.chat", "api.apps.conversation"):
        mod = _route_module(mod_name)
        monkeypatch.setattr(mod, "LLMBundle", _FakeBundle)
        monkeypatch.setattr(mod, "get_tenant_default_model_by_type", lambda s, tid, t: {"llm_name": "default-m"})
        monkeypatch.setattr(mod, "get_model_config_by_type_and_name", lambda s, tid, t, name: {"llm_name": name})
    monkeypatch.setattr(SearchService, "get_detail", classmethod(lambda cls, s, sid: {"search_config": {}}))
    return _FakeBundle


@pytest.fixture
def auth_client(client):
    from api.utils.api_utils import async_beta_token_required, async_token_required, beta_token_required, token_required

    client.app.dependency_overrides[token_required] = lambda: "tenant-unit"
    client.app.dependency_overrides[async_token_required] = lambda: "tenant-unit"
    client.app.dependency_overrides[beta_token_required] = lambda: "tenant-unit"
    client.app.dependency_overrides[async_beta_token_required] = lambda: "tenant-unit"
    return client


def test_sdk_related_questions_shape(auth_client, rq_stubs):
    resp = auth_client.post("/api/v1/sessions/related_questions", json={"question": "kw"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    assert body["data"] == ["相关词一", "相关词二"]


def test_chat_api_related_questions_shape(auth_client, rq_stubs):
    resp = auth_client.post("/api/v1/chat/recommendation", json={"question": "kw", "search_id": "s1"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    assert body["data"] == ["相关词一", "相关词二"]


def test_searchbot_related_questions_embedded_legacy_shape(auth_client, rq_stubs):
    resp = auth_client.post("/api/v1/searchbots/related_questions", json={"question": "kw", "search_id": "s1"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["retcode"] == 0
    assert body["retmsg"] == "success"
    assert body["data"] == ["相关词一", "相关词二"]


def test_conversation_related_questions_legacy_shape(auth_client, rq_stubs):
    resp = auth_client.post("/v1/conversation/related_questions", json={"question": "kw", "search_id": "s1"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["retcode"] == 0
    assert body["retmsg"] == "success"
    assert body["data"] == ["相关词一", "相关词二"]


def test_related_questions_bundles_strip_facade(auth_client, rq_stubs):
    """四路由的 LLMBundle 全部经 run_sync 构造后剥离 facade（db is None，AGENTS.md 规约）。"""
    for path, payload in [
        ("/api/v1/sessions/related_questions", {"question": "kw"}),
        ("/api/v1/chat/recommendation", {"question": "kw", "search_id": "s1"}),
        ("/api/v1/searchbots/related_questions", {"question": "kw", "search_id": "s1"}),
        ("/v1/conversation/related_questions", {"question": "kw", "search_id": "s1"}),
    ]:
        assert auth_client.post(path, json=payload).status_code == 200

    assert len(rq_stubs.instances) == 4
    assert all(bundle.db is None for bundle in rq_stubs.instances)
