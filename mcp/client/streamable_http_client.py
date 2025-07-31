from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


async def main():
    try:
        async with streamablehttp_client("http://localhost:9382/mcp/") as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                tools = await session.list_tools()
                print(f"{tools.tools=}")
                response = await session.call_tool(name="ragflow_retrieval", arguments={"dataset_ids": ["bc4177924a7a11f09eff238aa5c10c94"], "document_ids": [], "question": "How to install neovim?"})
                print(f"Tool response: {response.model_dump()}")
    except Exception as e:
        print(e)


if __name__ == "__main__":
    from anyio import run

    run(main)
