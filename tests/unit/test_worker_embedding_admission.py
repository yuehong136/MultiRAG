"""Title calls share admission, stay off-loop, and drain before releasing slots."""

import asyncio
import threading
from contextlib import nullcontext
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import numpy as np
import pytest
from sqlalchemy.orm import Session

from api.db.services.llm_service import LLMBundle, llm_bundle_scope
from api.db.services.tenant_llm_service import TenantLangfuseService, TenantLLMService
from common.asyncio_utils import LoopLocalSemaphore
from core.flow.tokenizer import tokenizer as tokenizer_module
from core.svr import task_executor

CONFIG = {"llm_name": "controlled", "llm_factory": "Builtin", "mdl_type": "embedding", "max_tokens": 8192, "api_key": "", "api_base": ""}


@pytest.fixture(params=["worker", "tokenizer"])
def consumer(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, db: Session) -> SimpleNamespace:
    models: list[Any] = []
    calls: list[list[str]] = []
    limiter = LoopLocalSemaphore(1)

    def construct(*args: Any, **kwargs: Any) -> Any:
        model = SimpleNamespace(client=SimpleNamespace(close=Mock()), async_client=SimpleNamespace(close=AsyncMock()))

        def encode(texts: list[str]) -> tuple[np.ndarray, int]:
            calls.append(texts)
            return np.array([[9.0, 8.0] if text == "title" else [1.0, 2.0] for text in texts]), len(texts)

        model.encode = encode
        models.append(model)
        return model

    monkeypatch.setattr(TenantLLMService, "model_instance", construct)
    monkeypatch.setattr(TenantLangfuseService, "filter_by_tenant", lambda *args, **kwargs: None)
    monkeypatch.setattr(task_executor, "embed_limiter", limiter)
    monkeypatch.setattr(tokenizer_module, "embed_limiter", limiter)
    monkeypatch.setattr(tokenizer_module, "db_connection", lambda: nullcontext(db))
    monkeypatch.setattr(tokenizer_module, "get_tenant_default_model_by_type", lambda *args: CONFIG)
    chunks = [{"text": "body", "docnm_kwd": "title", "content_with_weight": "body"}]
    tokenizer = tokenizer_module.Tokenizer.__new__(tokenizer_module.Tokenizer)
    tokenizer._param = tokenizer_module.TokenizerParam()
    tokenizer._param.filename_embd_weight = 0
    tokenizer._canvas = SimpleNamespace(_tenant_id="tenant", _kb_id=None)
    tokenizer.callback = lambda *args, **kwargs: None

    async def run() -> int:
        if request.param == "tokenizer":
            _, tokens = await tokenizer._embedding("title", chunks)
            return tokens
        async with llm_bundle_scope():
            model = LLMBundle(db, "tenant", CONFIG)
            return await task_executor.embedding(chunks, model, {"filename_embd_weight": 0}, lambda **kwargs: None)

    return SimpleNamespace(run=run, limiter=limiter, models=models, calls=calls, chunks=chunks)


async def test_waiting_title_cancellation_has_no_provider_call_and_slot_is_reusable(consumer: SimpleNamespace) -> None:
    async with consumer.limiter:
        waiting = asyncio.create_task(consumer.run())
        await asyncio.sleep(0.01)
        assert consumer.calls == []
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert consumer.calls == []
    assert await consumer.run() == 2
    assert consumer.calls == [["title"], ["body"]]
    assert consumer.chunks[0]["q_2_vec"] == [1.0, 2.0]
    for model in consumer.models:
        model.client.close.assert_called_once()
        model.async_client.close.assert_awaited_once()


@pytest.mark.parametrize("failure", [False, True])
async def test_title_repeated_cancel_drains_with_slot_held_and_preserves_business_error(consumer: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, failure: bool) -> None:
    started, release, stopped = threading.Event(), threading.Event(), threading.Event()
    original = TenantLLMService.model_instance

    def construct(*args: Any, **kwargs: Any) -> Any:
        model = original(*args, **kwargs)
        encode = model.encode
        if len(consumer.models) == 1:

            def slow_encode(texts: list[str]) -> tuple[np.ndarray, int]:
                started.set()
                try:
                    assert release.wait(5), "event loop blocked during title encode"
                    if failure:
                        raise ValueError("title failed")
                    return encode(texts)
                finally:
                    stopped.set()

            model.encode = slow_encode
        return model

    monkeypatch.setattr(TenantLLMService, "model_instance", construct)
    running = asyncio.create_task(consumer.run())
    next_run: asyncio.Task[int] | None = None
    try:
        assert await asyncio.to_thread(started.wait, 2)
        assert consumer.limiter.locked()
        running.cancel()
        await asyncio.sleep(0.01)
        running.cancel()
        await asyncio.sleep(0.01)
        assert not running.done() and not stopped.is_set()
        consumer.models[0].client.close.assert_not_called()
        next_run = asyncio.create_task(consumer.run())
        await asyncio.sleep(0.01)
        assert consumer.calls == [] and consumer.limiter.locked()
        release.set()
        with pytest.raises(ValueError if failure else asyncio.CancelledError, match="title failed" if failure else None):
            await running
        assert stopped.is_set()
        assert await next_run == 2
        assert consumer.calls == ([["title"], ["title"], ["body"]] if not failure else [["title"], ["body"]])
        for model in consumer.models:
            model.client.close.assert_called_once()
            model.async_client.close.assert_awaited_once()
    finally:
        release.set()
        await asyncio.gather(running, *([next_run] if next_run is not None else []), return_exceptions=True)


async def test_title_failure_does_not_write_vectors_or_call_body(consumer: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    original = TenantLLMService.model_instance

    def construct(*args: Any, **kwargs: Any) -> Any:
        model = original(*args, **kwargs)
        model.encode = Mock(side_effect=ValueError("title failed"))
        return model

    monkeypatch.setattr(TenantLLMService, "model_instance", construct)
    with pytest.raises(ValueError, match="title failed"):
        await consumer.run()
    assert "q_2_vec" not in consumer.chunks[0] and "vector" not in consumer.chunks[0]
    consumer.models[0].encode.assert_called_once_with(["title"])
    consumer.models[0].client.close.assert_called_once()
    consumer.models[0].async_client.close.assert_awaited_once()
    assert not consumer.limiter.locked()
