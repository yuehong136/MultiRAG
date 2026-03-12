from fastmcp import Client


async def main():
    try:
        # To access MultiRAG server in `host` mode, attach a Bearer token:
        # async with Client("http://localhost:9382/mcp/", auth="multirag-xxxxx") as client:

        async with Client("http://localhost:9382/mcp/") as client:
            tools = await client.list_tools()
            print(f"Tools: {[t.name for t in tools]}")

            response = await client.call_tool(
                name="multirag_retrieval",
                arguments={
                    "dataset_ids": ["bc4177924a7a11f09eff238aa5c10c94"],
                    "document_ids": [],
                    "question": "How to install neovim?",
                },
            )
            print(f"Tool response: {response}")

    except Exception as e:
        print(e)


if __name__ == "__main__":
    from anyio import run

    run(main)
