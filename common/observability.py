"""OTel 自动埋点初始化（进程级单点，方案 §8-1）。

设计要点：
- **唯一调用点是 :func:`common.bootstrap.ensure_initialized`**——不要在 app/路由/
  入口脚本里散落 init；配置开关 ``observability.enabled`` 默认关闭，零开销。
- 四件套 Instrumentor 全部走**全局 patch**（不引用具体 engine/app 对象）：
  - FastAPI/SQLAlchemy 的 patch 拦截的是"之后创建"的实例——bootstrap 在
    api.db.db_models（模块级建 engine/async_engine）与 api.apps（模块级建
    FastAPI 实例）导入之前执行，因此同步/异步两个引擎与 app 都被覆盖；
  - 这也规避了 common → api 的 import-linter 契约违规。
- langfuse 自建独立的 TracerProvider（v3/v4 皆然，v4 可经 ``tracer_provider=``
  显式注入，我们不注入），与此处设置的全局 provider 互不干扰。
"""

import logging
import os
import sys
import threading

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_initialized = False


def _derive_service_name() -> str:
    """OTEL_SERVICE_NAME > observability.service_name（调用方已处理）> 入口模块名推导。"""
    argv0 = os.path.basename(sys.argv[0] or "").removesuffix(".py")
    for token, name in (
        ("task_executor", "multirag-task-executor"),
        ("multirag_server", "multirag-server"),
        ("admin", "multirag-admin"),
    ):
        if token in argv0 or any(token in a for a in sys.argv[1:2]):
            return name
    # ``python -m api.multirag_server`` 时 argv[0] 是完整路径，上面已覆盖；
    # 其余入口（脚本/测试）给通用名
    return "multirag"


def init_otel() -> None:
    """按配置初始化 OTel 追踪与四件套自动埋点。幂等；enabled=False 时零副作用。"""
    global _initialized
    with _lock:
        if _initialized:
            return

        from common.app_config import get_app_config

        conf = get_app_config().observability
        if not conf.enabled:
            return
        _initialized = True

        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        service_name = os.environ.get("OTEL_SERVICE_NAME") or conf.service_name or _derive_service_name()
        provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=conf.otlp_endpoint)))
        trace.set_tracer_provider(provider)

        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        from opentelemetry.instrumentation.redis import RedisInstrumentor
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

        FastAPIInstrumentor().instrument()
        SQLAlchemyInstrumentor().instrument(enable_commenter=False)
        RedisInstrumentor().instrument()
        HTTPXClientInstrumentor().instrument()

        logger.info("OTel tracing enabled: service=%s endpoint=%s", service_name, conf.otlp_endpoint)
