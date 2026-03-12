from fastmcp import Client
from fastmcp.client.transports import SSETransport


async def main():
    try:
        # To access MultiRAG server in `host` mode, attach a Bearer token:
        # transport = SSETransport(
        #     url="http://localhost:9382/sse",
        #     headers={"Authorization": "Bearer multirag-xxxxx"},
        # )
        # Or use api_key header:
        # transport = SSETransport(
        #     url="http://localhost:9382/sse",
        #     headers={"api_key": "multirag-xxxxx"},
        # )

        transport = SSETransport(url="http://localhost:9382/sse")
        async with Client(transport) as client:
            tools = await client.list_tools()
            print(f"Tools: {[t.name for t in tools]}")

            response = await client.call_tool(
                name="multirag_retrieval",
                arguments={
                    "dataset_ids": ["ce3bb17cf27a11efa69751e139332ced"],
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
