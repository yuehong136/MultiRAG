"""Task-owned models close independently of shared sessions and tracers."""

import asyncio
import threading
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import numpy as np
import pytest
from sqlalchemy.orm import Session

from api.db.services.llm_service import LLMBundle, llm_bundle_scope
from api.db.services.tenant_llm_service import TenantLangfuseService, TenantLLMService

CONFIG = {"llm_name": "controlled", "llm_factory": "Builtin", "mdl_type": "embedding", "max_tokens": 8192, "api_key": "", "api_base": ""}


@pytest.fixture
def providers(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    models = []

    def construct(*args: Any, **kwargs: Any) -> Any:
        model = SimpleNamespace(client=SimpleNamespace(close=Mock()), async_client=SimpleNamespace(close=AsyncMock()))
        model.encode = lambda texts: (np.asarray([[0.0] for _ in texts]), 0)
        models.append(model)
        return model

    monkeypatch.setattr(TenantLLMService, "model_instance", construct)
    monkeypatch.setattr(TenantLangfuseService, "filter_by_tenant", lambda *args, **kwargs: None)
    return models


@pytest.mark.parametrize("failure", [False, True])
async def test_owned_clients_close_without_touching_shared_session_or_trace(db: Session, providers: list[Any], failure: bool) -> None:
    tracer = SimpleNamespace(shutdown=Mock(), flush=Mock())
    try:
        async with llm_bundle_scope():
            bundle = LLMBundle(db, "tenant", CONFIG)
            bundle.langfuse = tracer
            if failure:
                raise ValueError("business failure")
    except ValueError:
        assert failure
    assert bundle.db is db
    providers[0].client.close.assert_called_once()
    providers[0].async_client.close.assert_awaited_once()
    tracer.shutdown.assert_not_called()
    tracer.flush.assert_not_called()
    await bundle.aclose()
    providers[0].client.close.assert_called_once()
    with pytest.raises(RuntimeError, match="closed"):
        bundle.encode(["late"])


async def test_nested_and_parallel_scopes_close_only_their_own_models(db: Session, providers: list[Any]) -> None:
    async with llm_bundle_scope():
        outer = LLMBundle(db, "outer", CONFIG)

        async def child() -> Any:
            async with llm_bundle_scope():
                inner = LLMBundle(db, "inner", CONFIG)
                await asyncio.sleep(0)
            return inner.mdl

        children = await asyncio.gather(child(), child())
        for model in children:
            model.client.close.assert_called_once()
        outer.mdl.client.close.assert_not_called()
    outer.mdl.client.close.assert_called_once()
    assert len(providers) == 3


async def test_repeated_cancel_drains_inflight_encode_before_clients_close(db: Session, providers: list[Any]) -> None:
    started, release, stopped = threading.Event(), threading.Event(), threading.Event()
    bundle = LLMBundle(db, "tenant", CONFIG)

    def encode(texts: list[str]) -> tuple[np.ndarray[Any, Any], int]:
        started.set()
        try:
            assert release.wait(5)
            providers[0].client.close.assert_not_called()
            return np.asarray([[0.0]]), 0
        finally:
            stopped.set()

    providers[0].encode = encode
    encoding = asyncio.create_task(asyncio.to_thread(bundle.encode, ["body"]))
    closing: asyncio.Task[None] | None = None
    try:
        assert await asyncio.to_thread(started.wait, 2)
        closing = asyncio.create_task(bundle.aclose())
        await asyncio.sleep(0.01)
        closing.cancel()
        await asyncio.sleep(0.01)
        closing.cancel()
        await asyncio.sleep(0.01)
        assert not closing.done() and not stopped.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert stopped.is_set()
        providers[0].client.close.assert_called_once()
        providers[0].async_client.close.assert_awaited_once()
    finally:
        release.set()
        await encoding
        if closing is not None:
            await asyncio.gather(closing, return_exceptions=True)


async def test_parallel_encodes_are_not_serialized_by_lifetime_tracking(db: Session, providers: list[Any]) -> None:
    barrier = threading.Barrier(2, timeout=3)
    bundle = LLMBundle(db, "tenant", CONFIG)

    def encode(texts: list[str]) -> tuple[np.ndarray[Any, Any], int]:
        barrier.wait()
        return np.asarray([[0.0]]), 0

    providers[0].encode = encode
    try:
        await asyncio.gather(*(asyncio.to_thread(bundle.encode, ["body"]) for _ in range(2)))
    finally:
        await bundle.aclose()
    providers[0].client.close.assert_called_once()


async def test_cleanup_failure_closes_remaining_clients_and_fails_success(db: Session, providers: list[Any]) -> None:
    with pytest.raises(ExceptionGroup, match="task model bundles"):
        async with llm_bundle_scope():
            LLMBundle(db, "tenant", CONFIG)
            providers[0].client.close.side_effect = RuntimeError("close failure")
    providers[0].async_client.close.assert_awaited_once()


async def test_partial_constructor_failure_still_closes_created_provider(db: Session, monkeypatch: pytest.MonkeyPatch, providers: list[Any]) -> None:
    def fail_trace_lookup(*args: Any, **kwargs: Any) -> None:
        raise ValueError("trace lookup failed after provider construction")

    monkeypatch.setattr(TenantLangfuseService, "filter_by_tenant", fail_trace_lookup)
    with pytest.raises(ValueError, match="trace lookup"):
        async with llm_bundle_scope():
            LLMBundle(db, "tenant", CONFIG)
    assert len(providers) == 1
    providers[0].client.close.assert_called_once()
    providers[0].async_client.close.assert_awaited_once()


async def test_real_openai_sdk_sync_and_async_clients_are_closed(db: Session, monkeypatch: pytest.MonkeyPatch, providers: list[Any]) -> None:
    import httpx
    from openai import AsyncOpenAI, OpenAI

    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    client = OpenAI(api_key="controlled", http_client=httpx.Client(transport=transport))
    async_client = AsyncOpenAI(api_key="controlled", http_client=httpx.AsyncClient(transport=transport))
    provider = SimpleNamespace(client=client, async_client=async_client)
    monkeypatch.setattr(TenantLLMService, "model_instance", lambda *args, **kwargs: provider)
    async with llm_bundle_scope():
        LLMBundle(db, "tenant", CONFIG)
        assert not client.is_closed() and not async_client.is_closed()
    assert client.is_closed() and async_client.is_closed()


@pytest.mark.parametrize("body", ["business_failure", "success", "canceled"])
async def test_scope_cleanup_cancellation_priority_and_isolation(db: Session, providers: list[Any], body: str) -> None:
    from api.db.services import llm_service

    close_started, release_close, body_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = ValueError("original business failure")
    restored = []

    async def delayed_close() -> None:
        close_started.set()
        await release_close.wait()

    async def operation() -> None:
        try:
            async with llm_bundle_scope():
                LLMBundle(db, "first", CONFIG)
                LLMBundle(db, "second", CONFIG)
                providers[-1].async_client.close.side_effect = delayed_close
                body_started.set()
                if body == "business_failure":
                    raise original
                if body == "canceled":
                    await asyncio.Event().wait()
        finally:
            restored.append(llm_service._owned_bundles.get() is None)

    work = asyncio.create_task(operation())
    try:
        await asyncio.wait_for(body_started.wait(), 2)
        if body == "canceled":
            work.cancel()
        await asyncio.wait_for(close_started.wait(), 2)
        work.cancel()
        await asyncio.sleep(0.01)
        work.cancel()
        await asyncio.sleep(0.01)
        assert not work.done()
        release_close.set()
        if body == "business_failure":
            with pytest.raises(ValueError) as failure:
                await work
            assert failure.value is original
        else:
            with pytest.raises(asyncio.CancelledError):
                await work
        assert restored == [True]
        for model in providers:
            model.client.close.assert_called_once()
            model.async_client.close.assert_awaited_once()
        async with llm_bundle_scope():
            LLMBundle(db, "later", CONFIG)
            providers[-1].client.close.assert_not_called()
        assert len(providers) == 3
        for model in providers:
            model.client.close.assert_called_once()
            model.async_client.close.assert_awaited_once()
    finally:
        release_close.set()
        await asyncio.gather(work, return_exceptions=True)


async def test_worker_model_closes_after_binding_probe_then_index_failure(db: Session, monkeypatch: pytest.MonkeyPatch, providers: list[Any]) -> None:
    from core.svr import task_executor

    task = {"id": "task", "doc_id": "doc", "kb_id": "kb", "tenant_id": "tenant", "from_page": 0, "to_page": 1, "embd_id": "embedding", "llm_id": "chat", "parser_id": "naive", "parser_config": {}}
    monkeypatch.setattr(task_executor, "has_canceled", lambda task_id: False)
    monkeypatch.setattr(task_executor, "set_progress", lambda *args, **kwargs: None)
    monkeypatch.setattr(task_executor, "build_chunks", AsyncMock(return_value=[{"pk": "chunk"}]))
    monkeypatch.setattr(task_executor, "get_model_config_by_type_and_name", lambda *args: CONFIG)
    monkeypatch.setattr(task_executor.KnowledgebaseService, "get_by_id", lambda *args: SimpleNamespace(name="dataset"))
    monkeypatch.setattr(task_executor, "init_kb", AsyncMock(side_effect=RuntimeError("index failure")))
    with pytest.raises(RuntimeError, match="index failure"):
        await task_executor.do_handle_task(db, task)
    assert len(providers) == 1
    providers[0].client.close.assert_called_once()
    providers[0].async_client.close.assert_awaited_once()


@pytest.mark.parametrize("weight,expected", [(0, [1.0, 2.0]), (0.5, [5.5, 11.0]), (None, [1.9, 3.8])])
async def test_embedding_zero_title_weight_preserves_content_vector(weight: float | None, expected: list[float]) -> None:
    from core.svr import task_executor

    class Model:
        max_length = 8192

        def encode(self, texts: list[str]) -> tuple[np.ndarray[Any, Any], int]:
            return np.asarray([[10.0, 20.0] if text == "title" else [1.0, 2.0] for text in texts]), len(texts)

    chunks = [{"docnm_kwd": "title", "content_with_weight": "body"}]
    tokens = await task_executor.embedding(chunks, Model(), {"filename_embd_weight": weight}, lambda **kwargs: None)
    assert tokens == 2
    np.testing.assert_allclose(chunks[0]["q_2_vec"], expected)
    assert chunks[0]["vector"] == chunks[0]["q_2_vec"]
