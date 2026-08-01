"""LLM4Tenant 的 Langfuse 初始化契约（§11 Phase 2 任务 1：运行热路径零 preflight）。

钉住两点：构造期不做 auth_check（阻塞 HTTP，经 run_sync 构造 LLMBundle 时会冻结
事件循环——凭据有效性由配置写入期的 langfuse_app 校验）；Langfuse 构造失败
fail-open（tracer 置空、请求不受影响）。
"""

import types

import pytest

from api.db.services.tenant_llm_service import LLM4Tenant, TenantLangfuseService, TenantLLMService

_KEYS = types.SimpleNamespace(public_key="pk", secret_key="sk", host="http://langfuse.local")


class _LangfuseNoPreflight:
    """不带 auth_check 属性：若初始化仍调用 auth_check 会 AttributeError → fail-open 置空 → 断言必红。"""

    def __init__(self, public_key, secret_key, host):
        self.public_key = public_key

    def create_trace_id(self):
        return "trace-1"


class _LangfuseBoom:
    def __init__(self, *args, **kwargs):
        raise RuntimeError("langfuse host unreachable")


@pytest.fixture
def _stubs(monkeypatch):
    monkeypatch.setattr(TenantLLMService, "model_instance", classmethod(lambda cls, *a, **kw: object()))
    monkeypatch.setattr(TenantLangfuseService, "filter_by_tenant", classmethod(lambda cls, s, tenant_id=None: _KEYS))


def test_initialize_creates_tracer_without_preflight(monkeypatch, db, _stubs):
    monkeypatch.setattr("api.db.services.tenant_llm_service.Langfuse", _LangfuseNoPreflight)

    bundle = LLM4Tenant(db, "tenant-1", {"llm_name": "m", "max_tokens": 16})

    assert isinstance(bundle.langfuse, _LangfuseNoPreflight)
    assert bundle.trace_context == {"trace_id": "trace-1"}


def test_initialize_fail_open_on_langfuse_error(monkeypatch, db, _stubs):
    monkeypatch.setattr("api.db.services.tenant_llm_service.Langfuse", _LangfuseBoom)

    bundle = LLM4Tenant(db, "tenant-1", {"llm_name": "m", "max_tokens": 16})

    assert bundle.langfuse is None
    assert bundle.trace_context == {}


def test_initialize_without_keys_skips_tracer(monkeypatch, db):
    monkeypatch.setattr(TenantLLMService, "model_instance", classmethod(lambda cls, *a, **kw: object()))
    monkeypatch.setattr(TenantLangfuseService, "filter_by_tenant", classmethod(lambda cls, s, tenant_id=None: None))

    bundle = LLM4Tenant(db, "tenant-1", {"llm_name": "m", "max_tokens": 16})

    assert bundle.langfuse is None
    assert bundle.trace_context == {}


# ---------------------------------------------------------------------------
# SDK v4 API 面契约
# ---------------------------------------------------------------------------


def test_sdk_exposes_the_observation_api_we_call():
    """langfuse v4 删掉了 start_generation，只剩 start_observation(as_type=...)。

    两个方向都要钉：真装的 SDK 必须有我们调的方法与参数；同时 start_generation
    必须不存在——它一旦复活就说明依赖被降回 v3，而我们全部埋点已按 v4 写。
    """
    import inspect

    from langfuse import Langfuse
    from langfuse._client.span import LangfuseGeneration

    assert not hasattr(Langfuse, "start_generation"), "依赖疑似降回 langfuse v3"

    params = inspect.signature(Langfuse.start_observation).parameters
    for name in ("trace_context", "as_type", "name", "model", "input", "metadata"):
        assert name in params, f"start_observation 缺少我们在用的参数 {name}"

    # 我们的 end() 一律无参调用（v4 只接受 end_time），输出走 update()
    end_params = inspect.signature(LangfuseGeneration.end).parameters
    assert all(p.default is not inspect.Parameter.empty for p in end_params.values() if p.name != "self")

    update_params = inspect.signature(LangfuseGeneration.update).parameters
    for name in ("output", "usage_details"):
        assert name in update_params, f"update 缺少我们在用的参数 {name}"


def test_projects_response_is_pydantic_v2():
    """langfuse_app 用 model_dump() 读项目信息；v3 的 fern SDK 是 pydantic.v1 风格。"""
    from langfuse.api.projects.types.projects import Projects

    assert hasattr(Projects, "model_dump")
