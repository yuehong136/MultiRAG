from mcp.client.session import ClientSession
from mcp.client.sse import sse_client


async def main():
    try:
        # To access RAGFlow server in `host` mode, you need to attach `api_key` for each request to indicate identification.
        # async with sse_client("http://localhost:9382/sse", headers={"api_key": "ragflow-IyMGI1ZDhjMTA2ZTExZjBiYTMyMGQ4Zm"}) as streams:
        # Or follow the requirements of OAuth 2.1 Section 5 with Authorization header
        # async with sse_client("http://localhost:9382/sse", headers={"Authorization": "Bearer ragflow-IyMGI1ZDhjMTA2ZTExZjBiYTMyMGQ4Zm"}) as streams:

        async with sse_client("http://localhost:9382/sse") as streams:
            async with ClientSession(
                streams[0],
                streams[1],
            ) as session:
                await session.initialize()
                tools = await session.list_tools()
                print(f"{tools.tools=}")
                response = await session.call_tool(name="ragflow_retrieval", arguments={"dataset_ids": ["ce3bb17cf27a11efa69751e139332ced"], "document_ids": [], "question": "How to install neovim?"})
                print(f"Tool response: {response.model_dump()}")

    except Exception as e:
        print(e)


if __name__ == "__main__":
    from anyio import run

    run(main)
