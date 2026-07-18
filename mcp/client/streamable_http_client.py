"""MultiRAG MCP 规范客户端示例（streamable HTTP，正典传输）。

展示 fastmcp 3.x 惯用法：auth 一参鉴权、ToolError 区分业务错误、
result.data 直接拿 output_schema 水合后的结构化结果。
"""

from fastmcp import Client
from fastmcp.exceptions import ToolError


async def main():
    # self-host / host 模式都用 Bearer token（auth= 一参搞定）；
    # 旧版 api_key / x-api-key 头仍兼容：
    #   from fastmcp.client.transports import StreamableHttpTransport
    #   transport = StreamableHttpTransport("http://localhost:9382/mcp", headers={"x-api-key": "multirag-xxxxx"})
    #   async with Client(transport=transport) as client: ...
    async with Client("http://localhost:9382/mcp", auth="multirag-xxxxx") as client:
        tools = await client.list_tools()
        print(f"Tools: {[t.name for t in tools]}")

        # 数据集目录也可作为 resource 直接读（不消耗工具调用）
        catalog = await client.read_resource("datasets://list")
        print(f"Dataset catalog: {catalog[0].text[:200]}")

        try:
            result = await client.call_tool(
                name="multirag_retrieval",
                arguments={
                    "dataset_ids": ["bc4177924a7a11f09eff238aa5c10c94"],
                    "question": "How to install neovim?",
                },
                timeout=60,
            )
        except ToolError as e:
            # 业务错误（数据集不存在、embedding_model 分组冲突等）走这里
            print(f"Tool error: {e}")
            return

        # result.data 按服务端 output_schema 水合为结构化对象
        print(f"chunks: {len(result.data.chunks)}, page: {result.data.pagination.page}")
        for chunk in result.data.chunks[:3]:
            print(f"- [{chunk.dataset_name}] {chunk.document_name}")


if __name__ == "__main__":
    from anyio import run

    run(main)
