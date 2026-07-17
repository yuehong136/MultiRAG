"""MCP server 传输组装与鉴权契约测试。

背景：ASGI 组装从「抽子 app routes 平铺」改为官方范式（mcp.http_app() 整 app
Mount），子 app 的 app 级中间件（RequestContext、StaticTokenVerifier 鉴权、
streamable HTTP 的 Host/Origin DNS-rebinding 防护）得以保留。本文件锁：
- 三种历史凭证形态（Bearer / api_key / x-api-key）全部可过鉴权；
- 无凭证 401、self-host 模式错误密钥 401；
- /health 免鉴权存活；
- 工具带 readOnlyHint 注解、multirag_retrieval 产出 structured content。
"""

import importlib.util
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_SERVER_PATH = Path(__file__).resolve().parents[2] / "mcp" / "server" / "server.py"
_spec = importlib.util.spec_from_file_location("multirag_mcp_transport_test_module", _SERVER_PATH)
assert _spec is not None and _spec.loader is not None
server = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(server)

_KEY = "multirag-test-key"


@pytest.fixture
def self_host_app(monkeypatch):
    monkeypatch.setattr(server, "MODE", server.LaunchMode.SELF_HOST)
    monkeypatch.setattr(server, "HOST_API_KEY", _KEY)
    server.create_mcp_server()
    return server.create_starlette_app()


def _initialize_payload() -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "pytest", "version": "0"},
        },
    }


def _post_mcp(app, headers: dict[str, str]) -> int:
    full_headers = {"accept": "application/json, text/event-stream", "content-type": "application/json", **headers}
    with TestClient(app, raise_server_exceptions=False) as client:
        return client.post("/mcp", json=_initialize_payload(), headers=full_headers).status_code


def test_mcp_endpoint_rejects_missing_credentials(self_host_app):
    assert _post_mcp(self_host_app, {}) == 401


def test_mcp_endpoint_rejects_wrong_key(self_host_app):
    assert _post_mcp(self_host_app, {"Authorization": "Bearer wrong-key"}) == 401


@pytest.mark.parametrize(
    "headers",
    [
        {"Authorization": f"Bearer {_KEY}"},
        {"api_key": _KEY},
        {"x-api-key": _KEY},
    ],
    ids=["bearer", "api_key", "x-api-key"],
)
def test_mcp_endpoint_accepts_all_legacy_credential_forms(self_host_app, headers):
    assert _post_mcp(self_host_app, headers) == 200


def test_sse_endpoint_rejects_missing_credentials(self_host_app):
    with TestClient(self_host_app, raise_server_exceptions=False) as client:
        assert client.get("/sse", headers={"accept": "text/event-stream"}).status_code == 401


def test_health_route_is_public(self_host_app):
    with TestClient(self_host_app, raise_server_exceptions=False) as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


async def test_tools_are_read_only_annotated_and_retrieval_is_structured(monkeypatch):
    from fastmcp import Client

    monkeypatch.setattr(server, "MODE", server.LaunchMode.SELF_HOST)
    monkeypatch.setattr(server, "HOST_API_KEY", _KEY)
    mcp = server.create_mcp_server()

    fake_result = {"chunks": [], "pagination": {"page": 1}, "query_info": {"question": "q"}}

    async def fake_retrieval(self, api_key, dataset_ids, **kwargs):
        return fake_result

    monkeypatch.setattr(server.MultiRAGConnector, "retrieval", fake_retrieval)

    async with Client(mcp) as client:
        tools = {t.name: t for t in await client.list_tools()}
        assert set(tools) == {"list_datasets", "multirag_retrieval"}
        for tool in tools.values():
            assert tool.annotations is not None and tool.annotations.readOnlyHint is True
        assert tools["multirag_retrieval"].outputSchema is not None

        result = await client.call_tool("multirag_retrieval", {"question": "q"})
        assert result.structured_content == fake_result
