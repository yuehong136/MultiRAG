"""MCP ASGI 服务的调试信息泄露回归测试。"""

import importlib.util
from pathlib import Path
from typing import NoReturn

from fastapi.testclient import TestClient
from starlette.requests import Request

_SERVER_PATH = Path(__file__).resolve().parents[2] / "mcp" / "server" / "server.py"
_spec = importlib.util.spec_from_file_location("multirag_mcp_security_test_module", _SERVER_PATH)
assert _spec is not None and _spec.loader is not None
server = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(server)


def test_unhandled_mcp_exception_does_not_expose_debug_traceback() -> None:
    server.create_mcp_server()
    app = server.create_starlette_app()

    async def debug_probe(_request: Request) -> NoReturn:
        raise RuntimeError("synthetic-sensitive-debug-detail")

    # SSE 子 app 挂在根路径兜底，探针必须插到路由表最前才可达
    from starlette.routing import Route

    app.router.routes.insert(0, Route("/__debug_probe__", debug_probe))

    response = TestClient(app, raise_server_exceptions=False).get(
        "/__debug_probe__",
        headers={"accept": "text/plain"},
    )

    assert app.debug is False
    assert response.status_code == 500
    assert "synthetic-sensitive-debug-detail" not in response.text
    assert "Traceback" not in response.text
