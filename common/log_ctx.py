"""日志上下文（方案 §8-3）：request_id/tenant_id/doc_id 的 contextvar 载体。

- API 进程：request_id 由 api/middleware/request_context.py 每请求生成/透传，
  tenant_id 在鉴权 user_loader 处绑定；
- task_executor：handle_task 拿到任务后以 task_id 充当 request_id，并绑定
  tenant_id/doc_id，任务收尾清除。

`ContextInjectFilter` 把当前值注入每条 LogRecord，供 JSON formatter 输出——
按 request_id 聚合一次请求/一个任务的全部日志。
"""

import logging
from contextvars import ContextVar

_request_id: ContextVar[str | None] = ContextVar("log_request_id", default=None)
_tenant_id: ContextVar[str | None] = ContextVar("log_tenant_id", default=None)
_doc_id: ContextVar[str | None] = ContextVar("log_doc_id", default=None)

_FIELDS: dict[str, ContextVar[str | None]] = {
    "request_id": _request_id,
    "tenant_id": _tenant_id,
    "doc_id": _doc_id,
}


def bind_log_context(request_id: str | None = None, tenant_id: str | None = None, doc_id: str | None = None) -> None:
    """绑定当前 task/线程的日志上下文；None 的参数不动既有值。"""
    if request_id is not None:
        _request_id.set(request_id)
    if tenant_id is not None:
        _tenant_id.set(tenant_id)
    if doc_id is not None:
        _doc_id.set(doc_id)


def clear_log_context() -> None:
    for var in _FIELDS.values():
        var.set(None)


def get_log_context() -> dict[str, str]:
    """当前非空上下文字段（JSON formatter 消费）。"""
    return {name: value for name, var in _FIELDS.items() if (value := var.get()) is not None}


class ContextInjectFilter(logging.Filter):
    """把 contextvar 上下文注入 LogRecord 属性（永远放行记录）。"""

    def filter(self, record: logging.LogRecord) -> bool:
        for name, var in _FIELDS.items():
            setattr(record, name, var.get())
        return True
