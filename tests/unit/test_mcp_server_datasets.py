"""MCP server 数据集发现与错误透传的打桩测试。

mcp/server/server.py 不是包（与 MCP SDK 的 ``mcp`` 包同名目录、无 __init__.py），
用 importlib 按文件路径加载，避免 shadow 掉 fastmcp 依赖的 SDK 包。
"""

import importlib.util
from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError

_SERVER_PATH = Path(__file__).resolve().parents[2] / "mcp" / "server" / "server.py"
_spec = importlib.util.spec_from_file_location("multirag_mcp_server_module", _SERVER_PATH)
assert _spec is not None and _spec.loader is not None
server = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(server)


class FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def json(self) -> dict:
        return self._payload


def _make_connector(pages: list[FakeResponse | None], page_size: int = 2):
    """构造一个 _get 被打桩的 connector，按调用次序返回 pages 中的响应。"""
    connector = server.MultiRAGConnector(base_url="http://stub")
    connector._DATASET_PAGE_SIZE = page_size
    calls: list[dict] = []

    async def fake_get(path, api_key, params=None):
        calls.append({"path": path, "params": dict(params or {})})
        return pages[len(calls) - 1]

    connector._get = fake_get
    return connector, calls


def _dataset(idx: str) -> dict:
    return {"id": idx, "name": f"ds-{idx}", "description": "", "embedding_model": "bge-m3"}


async def test_list_datasets_structured_paginates_until_total_and_dedupes():
    pages = [
        FakeResponse({"code": 0, "data": [_dataset("a"), _dataset("b")], "total": 3}),
        # 与上一页有重叠，验证跨页去重
        FakeResponse({"code": 0, "data": [_dataset("b"), _dataset("c")], "total": 3}),
    ]
    connector, calls = _make_connector(pages)

    datasets = await connector.list_datasets_structured("key")

    assert [d["id"] for d in datasets] == ["a", "b", "c"]
    assert [c["params"]["page"] for c in calls] == [1, 2]
    assert all(c["params"]["page_size"] == 2 for c in calls)


async def test_list_datasets_structured_reads_full_page_without_total():
    pages = [FakeResponse({"code": 0, "data": [_dataset("a"), _dataset("b")]}), FakeResponse({"code": 0, "data": []})]
    connector, calls = _make_connector(pages)

    datasets = await connector.list_datasets_structured("key")

    assert [d["id"] for d in datasets] == ["a", "b"]
    assert len(calls) == 2


async def test_fetch_datasets_page_surfaces_backend_error_message():
    pages = [FakeResponse({"code": 102, "message": "Invalid API key"})]
    connector, _ = _make_connector(pages)

    with pytest.raises(ToolError, match="Invalid API key"):
        await connector.list_datasets_structured("key")


async def test_fetch_datasets_page_surfaces_http_error_message():
    pages = [FakeResponse({"message": "Unauthorized"}, status_code=401)]
    connector, _ = _make_connector(pages)

    with pytest.raises(ToolError, match="Unauthorized"):
        await connector.list_datasets_structured("key")


async def test_resolve_retrieval_dataset_ids_raises_when_no_datasets():
    pages = [FakeResponse({"code": 0, "data": [], "total": 0})]
    connector, _ = _make_connector(pages)

    with pytest.raises(ToolError, match="No accessible datasets"):
        await connector._resolve_retrieval_dataset_ids("key", [])


async def test_list_datasets_uses_actual_rest_total_datasets_field() -> None:
    pages = [FakeResponse({"code": 0, "data": [_dataset("a"), _dataset("b")], "total_datasets": 3}), FakeResponse({"code": 0, "data": [_dataset("c")], "total_datasets": 3})]
    connector, calls = _make_connector(pages)
    assert [row["id"] for row in await connector.list_datasets_structured("key")] == ["a", "b", "c"]
    assert len(calls) == 2


async def test_list_datasets_nonprogress_does_not_return_partial_success() -> None:
    pages = [FakeResponse({"code": 0, "data": [_dataset("a")], "total_datasets": 3}), FakeResponse({"code": 0, "data": [_dataset("a")], "total_datasets": 3})]
    connector, _ = _make_connector(pages)
    with pytest.raises(ToolError, match="stopped before"):
        await connector.list_datasets_structured("key")


@pytest.mark.parametrize(
    "payload",
    [
        {"code": 0, "total_datasets": 0},
        {"code": 0, "data": {}, "total_datasets": 0},
        {"code": 0, "data": None},
        {"code": 0, "data": [{"id": "one"}], "total_datasets": 0},
        {"code": 0, "data": [{"id": "one"}], "total_datasets": None},
        {"code": 0, "data": [{"id": "one"}], "total_datasets": 1, "total": 2},
        {"code": 0, "data": [{"id": 1}], "total_datasets": 1},
    ],
)
async def test_list_datasets_rejects_missing_inventory_and_inconsistent_totals(payload: dict) -> None:
    connector, _ = _make_connector([FakeResponse(payload)])
    with pytest.raises(ToolError):
        await connector.list_datasets_structured("key")


async def test_list_datasets_rejects_accumulated_inventory_larger_than_total() -> None:
    connector, _ = _make_connector(
        [FakeResponse({"code": 0, "data": [_dataset("one")], "total_datasets": 2}), FakeResponse({"code": 0, "data": [_dataset("two"), _dataset("three")], "total_datasets": 2})]
    )
    with pytest.raises(ToolError, match="Invalid dataset total"):
        await connector.list_datasets_structured("key")


async def test_list_datasets_accepts_explicit_empty_inventory() -> None:
    connector, _ = _make_connector([FakeResponse({"code": 0, "data": [], "total_datasets": 0})])
    assert await connector.list_datasets_structured("key") == []


@pytest.mark.parametrize("tail", [{"code": 0, "data": []}, {"code": 0, "data": [], "total_datasets": 100}])
async def test_list_datasets_retains_first_total_across_later_pages(tail: dict) -> None:
    rows = [_dataset(str(index)) for index in range(100)]
    connector, _ = _make_connector([FakeResponse({"code": 0, "data": rows, "total_datasets": 106}), FakeResponse(tail)], page_size=100)
    with pytest.raises(ToolError, match=r"stopped before|changed between"):
        await connector.list_datasets_structured("key")
