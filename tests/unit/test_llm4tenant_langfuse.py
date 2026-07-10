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
