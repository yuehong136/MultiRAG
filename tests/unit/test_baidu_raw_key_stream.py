"""Baidu's active OpenAI-compatible adapter retains every streamed chunk."""

import json
from contextlib import asynccontextmanager
from typing import Any

import httpx
from openai import AsyncOpenAI

from core.llm import ChatModel


async def test_baidu_raw_key_real_sdk_stream_preserves_all_chunks() -> None:
    from litellm.litellm_core_utils.logging_worker import GLOBAL_LOGGING_WORKER

    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer fixture-baidu-raw"
        assert request.url.path == "/v2/chat/completions"
        body = json.loads(request.content)
        assert body["model"] == "ernie-fixture" and body["stream"] is True
        requests.append(request.url.path)
        chunks = [
            {"id": "fixture", "object": "chat.completion.chunk", "created": 1, "model": "ernie-fixture", "choices": [{"index": 0, "delta": {"content": value}, "finish_reason": None}]}
            for value in ("first", "second")
        ]
        payload = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=payload)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    model = ChatModel["BaiduYiyan"]("fixture-baidu-raw", "ernie-fixture", "https://fixture.invalid/v2", provider="BaiduYiyan", max_retries=0)
    original = model._completion
    try:
        async with AsyncOpenAI(api_key=model.api_key, base_url=model.base_url, http_client=http, max_retries=0) as sdk:

            @asynccontextmanager
            async def completion(options: dict[str, Any]) -> Any:
                async with original({**options, "client": sdk}) as response:
                    yield response

            model._completion = completion
            result = [chunk async for chunk in model.async_chat_streamly("", [{"role": "user", "content": "hello"}], {})]
            assert result[:2] == ["first", "second"] and type(result[-1]) is int
            assert requests == ["/v2/chat/completions"]
    finally:
        await GLOBAL_LOGGING_WORKER.stop()
        await GLOBAL_LOGGING_WORKER.clear_queue()
        await http.aclose()
        assert http.is_closed and GLOBAL_LOGGING_WORKER._worker_task is None and not GLOBAL_LOGGING_WORKER._running_tasks
