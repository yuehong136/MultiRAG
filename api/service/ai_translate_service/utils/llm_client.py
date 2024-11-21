from openai import OpenAI, AsyncOpenAI
from typing import Generator, Any

from abc import ABC, abstractmethod

from api.settings import AI_TRANSLATE_BASE_URL, AI_TRANSLATE_API_KEY, AI_TRANSLATE_MODEL_ID


class BaseLLMClient(ABC):
    def __init__(self, model_id: str):
        self.model_id = model_id
        self.base_url = AI_TRANSLATE_BASE_URL
        self.api_key = AI_TRANSLATE_API_KEY

    @staticmethod
    def _create_messages(system_content: str | None, user_content: str) -> list[dict[str, str | None]]:
        if system_content is None:
            return [{"role": "user", "content": user_content}]
        else:
            return [
                {"role": "system", "content": system_content},
                {"role": "user", "content": user_content}
            ]

    @abstractmethod
    def standard_request(self, system_content: str | None, user_content: str) -> Any:
        pass

    @abstractmethod
    def streaming_request(self, system_content: str | None, user_content: str) -> Any:
        pass


class SyncLLMClient(BaseLLMClient):
    def __init__(self, model_id: str):
        super().__init__(model_id)
        self.client = OpenAI(api_key=self.api_key, base_url=self.base_url)

    def standard_request(self, system_content: str | None = None, user_content: str = None) -> str:
        completion = self.client.chat.completions.create(
            model=self.model_id,
            messages=self._create_messages(system_content, user_content)
        )
        return completion.choices[0].message.content

    def streaming_request(self, system_content: str | None, user_content: str) -> Generator[str, None, None]:
        stream = self.client.chat.completions.create(
            model=self.model_id,
            messages=self._create_messages(system_content, user_content),
            stream=True
        )
        for chunk in stream:
            if chunk.choices:
                yield chunk.choices[0].delta.content or ""


class AsyncLLMClient(BaseLLMClient):
    def __init__(self, model_id: str):
        super().__init__(model_id)
        self.client = AsyncOpenAI(api_key=self.api_key, base_url=self.base_url)

    async def standard_request(self, system_content: str | None = None, user_content: str = None) -> str:
        completion = await self.client.chat.completions.create(
            model=self.model_id,
            messages=self._create_messages(system_content, user_content)
        )
        return completion.choices[0].message.content

    async def streaming_request(self, system_content: str | None, user_content: str):
        stream = await self.client.chat.completions.create(
            model=self.model_id,
            messages=self._create_messages(system_content, user_content),
            stream=True
        )
        async for chunk in stream:
            if chunk.choices:
                yield chunk.choices[0].delta.content or ""


# 同步使用示例
def print_streaming_response(generator: Generator[str, None, None]):
    for chunk in generator:
        print(chunk, end="")
    print()


# 异步使用示例
async def print_streaming_response_async(generator):
    async for chunk in generator:
        print(chunk, end="")
    print()


# 使用示例
if __name__ == "__main__":
    import asyncio

    model_id = AI_TRANSLATE_MODEL_ID
    sync_client = SyncLLMClient(model_id)
    async_client = AsyncLLMClient(model_id)

    system_content = "你是豆包，是由字节跳动开发的 AI 人工智能助手"
    user_content = "常见的十字花科植物有哪些？"

    print("----- 同步标准请求 -----")
    response = sync_client.standard_request(system_content, user_content)
    print(response)

    print("\n----- 同步流式请求 -----")
    streaming_response = sync_client.streaming_request(system_content, user_content)
    print_streaming_response(streaming_response)


    async def async_example():
        print("\n----- 异步标准请求 -----")
        response = await async_client.standard_request(system_content, user_content)
        print(response)

        print("\n----- 异步流式请求 -----")
        streaming_response = async_client.streaming_request(system_content, user_content)
        await print_streaming_response_async(streaming_response)


    asyncio.run(async_example())
