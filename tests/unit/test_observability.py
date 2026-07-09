"""common/observability.py 单元测试（方案 §8-1）。

覆盖：配置段默认值、enabled=False 时 init_otel 零副作用、service_name 推导。
enabled=True 的真实埋点路径涉及全局 patch（FastAPI/SQLAlchemy 类级别），
不适合在单测进程内执行，由冒烟验收覆盖（Jaeger 端到端）。
"""

import sys

import pytest

from common import observability
from common.app_config import AppConfig, ObservabilityConfig


@pytest.fixture
def fresh_state(monkeypatch):
    """隔离模块级幂等守卫，避免测试间串扰。"""
    monkeypatch.setattr(observability, "_initialized", False)
    yield


class TestObservabilityConfig:
    def test_defaults(self):
        conf = ObservabilityConfig()

        assert conf.enabled is False
        assert conf.otlp_endpoint == "http://localhost:4317"
        assert conf.service_name == ""

    def test_section_wired_into_app_config(self):
        cfg = AppConfig.model_validate({"observability": {"enabled": False, "otlp_endpoint": "http://collector:4317"}})

        assert cfg.observability.otlp_endpoint == "http://collector:4317"


class TestInitOtelDisabled:
    def test_disabled_is_a_noop(self, fresh_state, monkeypatch):
        cfg = AppConfig.model_validate({"observability": {"enabled": False}})
        monkeypatch.setattr("common.app_config.get_app_config", lambda: cfg)

        observability.init_otel()

        # 未置位幂等标记（下次调用仍会重读配置），也未设置全局 TracerProvider
        assert observability._initialized is False
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider

        assert not isinstance(trace.get_tracer_provider(), TracerProvider)


class TestServiceNameDerivation:
    @pytest.mark.parametrize(
        ("argv", "expected"),
        [
            (["/path/to/task_executor.py"], "multirag-task-executor"),
            (["/path/multirag_server.py"], "multirag-server"),
            (["pytest"], "multirag"),
        ],
    )
    def test_derive_from_argv(self, monkeypatch, argv, expected):
        monkeypatch.setattr(sys, "argv", argv)

        assert observability._derive_service_name() == expected
