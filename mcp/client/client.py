"""Legacy SSE 传输兼容性验证客户端。

SSE 传输已被 MCP 规范标记为 legacy（正典传输见 streamable_http_client.py），
本文件仅用于验证 /sse 端点对存量消费者仍然可用；新集成一律使用 streamable HTTP。
"""

from fastmcp import Client
from fastmcp.client.transports import SSETransport


async def main():
    # host 模式带 Bearer token；旧版 api_key 头同样兼容：
    #   SSETransport(url=..., headers={"api_key": "multirag-xxxxx"})
    transport = SSETransport(
        url="http://localhost:9382/sse",
        headers={"Authorization": "Bearer multirag-xxxxx"},
    )
    async with Client(transport) as client:
        tools = await client.list_tools()
        print(f"Tools: {[t.name for t in tools]}")

        result = await client.call_tool(
            name="multirag_retrieval",
            arguments={
                "dataset_ids": ["ce3bb17cf27a11efa69751e139332ced"],
                "question": "How to install neovim?",
            },
        )
        print(f"chunks: {len(result.data.chunks)}")


if __name__ == "__main__":
    from anyio import run

    run(main)
