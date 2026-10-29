"""The active LM Studio adapter completes blocking probes before closing."""

import asyncio
import threading
import time
from typing import Any

import httpx
import pytest
from openai import OpenAI

from api.db.model_contract import ModelRegistration
from api.db.services.provider_verification import verify_model


@pytest.mark.parametrize("mode", ["success", "timeout", "cancel"])
async def test_lmstudio_actual_embedding_probe_awaits_and_drains(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    from core.llm import embedding

    clients: list[OpenAI] = []
    started, completed = threading.Event(), threading.Event()
    ready = asyncio.Event()
    loop = asyncio.get_running_loop()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/embeddings"
        assert request.headers["Authorization"] == "Bearer lm-studio"
        started.set()
        loop.call_soon_threadsafe(ready.set)
        if mode != "success":
            time.sleep(0.08)
        assert not clients[0].is_closed()
        completed.set()
        return httpx.Response(200, json={"object": "list", "data": [{"index": 0, "embedding": [0.0, 1.0]}], "model": "bge-m3", "usage": {"prompt_tokens": 0, "total_tokens": 0}})

    def sdk(**kwargs: Any) -> OpenAI:
        result = OpenAI(**kwargs, max_retries=0, http_client=httpx.Client(transport=httpx.MockTransport(handler)))
        clients.append(result)
        return result

    monkeypatch.setattr(embedding, "OpenAI", sdk)
    monkeypatch.setenv("LLM_PROBE_TIMEOUT_SECONDS", "0.03" if mode == "timeout" else "2")
    task = asyncio.create_task(verify_model("LM-Studio", "unused-key", "http://127.0.0.1:1/v1", ModelRegistration(model_name="bge-m3", model_types=["embedding"], max_tokens=0)))
    if mode == "cancel":
        await ready.wait()
        task.cancel()
    if mode == "success":
        assert (await task)["verified"] is True
    else:
        with pytest.raises(asyncio.CancelledError if mode == "cancel" else ValueError):
            await task
    assert started.is_set() and completed.is_set() and clients[0].is_closed()
