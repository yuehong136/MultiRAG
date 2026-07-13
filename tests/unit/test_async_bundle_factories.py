"""异步 LLMBundle 工厂（tenant_model_service）契约：构造在工作线程 + 返回前剥离 session。

三个工厂是 canvas/Agent 批全部 bundle 构造的汇点——剥离断言在此集中钉板
（批次一验收口径：每个剥离点都要有断言；变异验证做 default 一处，三工厂同构）。
"""

import threading

import pytest

from api.db.joint_services import tenant_model_service as tms
from api.db.services.llm_service import LLMBundle


class _FakeBundle(LLMBundle):
    """继承真类过 beartype；__init__ 不触库，记录构造线程。"""

    def __init__(self, db, tenant_id, model_config, **kwargs):
        self.db = db
        self.tenant_id = tenant_id
        self.model_config = model_config
        self.kwargs = kwargs
        self.built_off_loop = threading.current_thread() is not threading.main_thread()


@pytest.fixture
def factory_stubs(monkeypatch):
    monkeypatch.setattr(tms, "LLMBundle", _FakeBundle)
    monkeypatch.setattr(tms, "get_tenant_default_model_by_type", lambda db, tid, t: {"llm_name": "default-m"})
    monkeypatch.setattr(tms, "get_model_config_by_type_and_name", lambda db, tid, t, name: {"llm_name": name})
    monkeypatch.setattr(tms, "get_model_config_by_id", lambda db, mid: {"llm_name": f"id-{mid}"})


async def test_default_factory_strips_session_and_builds_off_loop(factory_stubs):
    bundle = await tms.build_default_bundle_async("tenant-unit", "chat")

    assert bundle.db is None  # 构造用短会话已关闭，不得随 bundle 逸出（变异验证锚点）
    assert bundle.built_off_loop is True
    assert bundle.model_config == {"llm_name": "default-m"}


async def test_named_factory_strips_session(factory_stubs):
    bundle = await tms.build_named_bundle_async("tenant-unit", "chat", "m-x", max_retries=2)

    assert bundle.db is None
    assert bundle.built_off_loop is True
    assert bundle.model_config == {"llm_name": "m-x"}
    assert bundle.kwargs == {"max_retries": 2}


async def test_by_id_factory_strips_session(factory_stubs):
    bundle = await tms.build_bundle_by_id_async("tenant-unit", 7)

    assert bundle.db is None
    assert bundle.built_off_loop is True
    assert bundle.model_config == {"llm_name": "id-7"}
