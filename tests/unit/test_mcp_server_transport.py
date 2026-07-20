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
import json
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
    import fastmcp
    from fastmcp import Client

    monkeypatch.setattr(server, "MODE", server.LaunchMode.SELF_HOST)
    monkeypatch.setattr(server, "HOST_API_KEY", _KEY)
    mcp = server.create_mcp_server()

    # 含后端开放字段集的真实形态 chunk：额外字段必须原样透传（ChunkInfo extra=allow）
    fake_result = {
        "chunks": [{"dataset_name": "kb-1", "document_name": "doc.pdf", "content_with_weight": "raw text", "similarity": 0.87}],
        "pagination": {"page": 1, "page_size": 10, "total_chunks": 1, "total_pages": 1},
        "query_info": {"question": "q", "similarity_threshold": 0.2, "vector_weight": 0.3, "keyword_search": False, "dataset_count": 1},
    }

    async def fake_retrieval(self, api_key, dataset_ids, **kwargs):
        return fake_result

    async def fake_list_datasets(self, api_key):
        return [{"id": "ds-1", "name": "kb-1", "description": "", "embedding_model": "bge", "embd_id": "bge"}]

    monkeypatch.setattr(server.MultiRAGConnector, "retrieval", fake_retrieval)
    monkeypatch.setattr(server.MultiRAGConnector, "list_datasets_structured", fake_list_datasets)

    async with Client(mcp) as client:
        # 回归点：缺省会误报 fastmcp 库版本
        assert client.initialize_result.serverInfo.version == server._server_version()
        assert client.initialize_result.serverInfo.version != fastmcp.__version__

        tools = {t.name: t for t in await client.list_tools()}
        assert set(tools) == {"list_datasets", "multirag_retrieval"}
        for tool in tools.values():
            assert tool.annotations is not None and tool.annotations.readOnlyHint is True
            assert tool.annotations.idempotentHint is True

        # 字段级 output schema（非裸 object）
        retrieval_schema = tools["multirag_retrieval"].outputSchema
        assert {"chunks", "pagination", "query_info"} <= set(retrieval_schema.get("properties", {}) or retrieval_schema.get("$defs", {}).get("RetrievalResult", {}).get("properties", {}))

        result = await client.call_tool("multirag_retrieval", {"question": "q"})
        assert result.structured_content == fake_result  # 额外字段透传契约

        listed = await client.call_tool("list_datasets", {})
        assert listed.structured_content["result"][0]["id"] == "ds-1"

        # resource 双轨：datasets://list 与工具同源
        catalog = await client.read_resource("datasets://list")
        assert json.loads(catalog[0].text)[0]["id"] == "ds-1"
