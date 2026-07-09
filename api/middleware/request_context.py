"""request_id 中间件（方案 §8-3）：生成/透传 X-Request-ID 并绑定日志上下文。

纯 ASGI 形态（不用 BaseHTTPMiddleware：其响应包装会破坏 SSE 流式背压）。
tenant_id 由鉴权 user_loader 绑定（api/apps/__init__.py::load_user），
两者经 common/log_ctx 的 contextvar 注入每条日志。
"""

import uuid

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from common.log_ctx import bind_log_context, clear_log_context

REQUEST_ID_HEADER = "X-Request-ID"


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        incoming = headers.get(REQUEST_ID_HEADER.lower().encode())
        request_id = incoming.decode("latin-1") if incoming else uuid.uuid4().hex
        bind_log_context(request_id=request_id)

        async def send_with_request_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message).append(REQUEST_ID_HEADER, request_id)
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            clear_log_context()
