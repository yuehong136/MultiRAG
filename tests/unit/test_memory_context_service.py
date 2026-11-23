"""Prepared providers release SQL before construction and drain owned workers."""

import asyncio
import json
import threading
from copy import deepcopy
from types import SimpleNamespace
from typing import Any

import pytest
from beartype.roar import BeartypeCallHintParamViolation
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from api.apps.services import memory_context_service as service
from api.db.services import tenant_llm_service
from api.db.services.llm_service import LLMBundle, bundle_trace_scope, llm_bundle_scope
from api.db.services.tenant_llm_service import PreparedTenantLLM, TenantLangfuseService, TenantLLMService
from common.doc_store.doc_store_base import MatchTextExpr
from core import llm
from memory.services.context import ContextScope


@pytest.fixture
def model_config(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    config = {
        "id": "a" * 32,
        "tenant_id": "tenant",
        "llm_factory": "OpenAI",
        "llm_name": "controlled",
        "mdl_type": "embedding",
        "api_key": "synthetic-secret",
        "api_base": "http://model.invalid",
        "max_tokens": 0,
    }
    monkeypatch.setattr("api.db.services.provider_model_resolution.resolve_model_config", lambda db, value: deepcopy(value))
    monkeypatch.setattr(TenantLangfuseService, "filter_by_tenant", classmethod(lambda cls, *args, **kwargs: None))
    monkeypatch.setattr(service, "get_model_config_by_id", lambda *args, **kwargs: deepcopy(config))
    monkeypatch.setattr(service, "MsgTextQuery", lambda: SimpleNamespace(question=lambda text, **kwargs: (MatchTextExpr(["content"], text, 10), [])))

    def no_database(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Prepared construction must not enter the legacy database path")

    monkeypatch.setattr(tenant_llm_service, "db_connection", no_database)
    monkeypatch.setattr(TenantLLMService, "model_instance", no_database)
    return config


async def wait_event(event: threading.Event) -> None:
    if not await asyncio.wait_for(asyncio.to_thread(event.wait, 5), 6):
        raise TimeoutError("Controlled provider did not reach the expected phase")


async def test_source_search_bounds_distinct_memory_fanout_before_sql(async_db: AsyncSession) -> None:
    with pytest.raises(ValueError, match="at most 100"):
        await service._visible_bindings(async_db, [f"memory-{index}" for index in range(101)], actor_id="actor")


async def search(db: AsyncSession) -> list[dict[str, Any]]:
    async with llm_bundle_scope():
        return await service._search(
            db,
            [service.MemoryReadBinding("memory", "tenant", "a" * 32, "a" * 32)],
            "query",
            scope=ContextScope(frozenset({"memory"}), user_id="", session_id="session"),
            top_n=2,
            similarity_threshold=0.2,
            keywords_similarity_weight=0.7,
        )


async def test_cold_construction_runs_off_loop_after_sql_release(async_db: AsyncSession, model_config: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    started, release = threading.Event(), threading.Event()
    closed: list[bool] = []
    loop_thread = threading.get_ident()

    class Provider:
        def __init__(self, key: str, name: str, base_url: str) -> None:
            assert not async_db.in_transaction()
            assert threading.get_ident() != loop_thread
            started.set()
            assert release.wait(5)

        def encode_queries(self, query: str) -> tuple[list[float], int]:
            return [1.0, 0.0], 0

        def close(self) -> None:
            closed.append(True)

    def retrieve(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        assert args[1] == {"user_id": [""], "session_id": ["session"]}
        assert kwargs == {"strict": True}
        return []

    monkeypatch.setattr(llm, "EmbeddingModel", {"OpenAI": Provider})
    monkeypatch.setattr(service.MessageService, "search_message", retrieve)
    await async_db.begin()
    task = asyncio.create_task(search(async_db))
    try:
        await wait_event(started)
        await asyncio.wait_for(asyncio.sleep(0), 0.5)
        assert not task.done() and not async_db.in_transaction()
    finally:
        release.set()
    assert await task == []
    assert closed == [True]


@pytest.mark.parametrize("blocked_phase", ["construct", "retrieve"])
async def test_repeated_cancellation_drains_worker_before_one_close(async_db: AsyncSession, model_config: dict[str, Any], monkeypatch: pytest.MonkeyPatch, blocked_phase: str) -> None:
    started, release = threading.Event(), threading.Event()
    closed: list[bool] = []
    encoded: list[str] = []

    def pause() -> None:
        started.set()
        assert release.wait(5)

    class Provider:
        def __init__(self, key: str, name: str, base_url: str) -> None:
            if blocked_phase == "construct":
                pause()

        def encode_queries(self, query: str) -> tuple[list[float], int]:
            encoded.append(query)
            if blocked_phase == "retrieve":
                pause()
            return [1.0, 0.0], 0

        def close(self) -> None:
            assert release.is_set()
            closed.append(True)

    monkeypatch.setattr(llm, "EmbeddingModel", {"OpenAI": Provider})
    monkeypatch.setattr(service.MessageService, "search_message", lambda *args, **kwargs: [])
    task = asyncio.create_task(search(async_db))
    await wait_event(started)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done() and closed == []
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed == [True]
    assert encoded == ([] if blocked_phase == "construct" else ["query"])
    await asyncio.sleep(0)
    assert closed == [True]


@pytest.mark.parametrize("change", [{"tenant_id": "foreign"}, {"tenant_id": None}, {"id": None}, {"id": True}, {"id": "71"}, {"mdl_type": "chat"}])
def test_preparation_rejects_unbound_or_wrong_capability(db: Session, model_config: dict[str, Any], change: dict[str, Any]) -> None:
    with pytest.raises(LookupError):
        TenantLLMService.prepare_model_instance(db, "tenant", {**model_config, **change}, expected_type="embedding")


def test_preparation_copies_credentials_and_prohibits_dict_shortcuts(db: Session, model_config: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    keys = SimpleNamespace(public_key="public-key", secret_key="trace-secret", host="http://trace.invalid")
    monkeypatch.setattr(TenantLangfuseService, "filter_by_tenant", classmethod(lambda cls, *args, **kwargs: keys))
    prepared = TenantLLMService.prepare_model_instance(db, "tenant", model_config, expected_type="embedding")
    model_config["api_key"] = "changed"
    keys.secret_key = "changed"
    assert prepared.model_config["api_key"] == "synthetic-secret"
    assert prepared.langfuse_keys == ("public-key", "trace-secret", "http://trace.invalid")
    assert "synthetic-secret" not in repr(prepared) and "trace-secret" not in repr(prepared)
    with pytest.raises(BeartypeCallHintParamViolation):
        LLMBundle.from_prepared({"prepared": True, "tenant_id": "tenant"})  # type: ignore[arg-type]


def test_preparation_rechecks_resolved_tenant_and_explicit_builtin_branch(db: Session, model_config: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    builtin = {**model_config, "id": None, "tenant_id": None, "llm_factory": "Builtin"}
    with pytest.raises(LookupError):
        TenantLLMService.prepare_model_instance(db, "tenant", builtin, expected_type="embedding")
    del builtin["tenant_id"]
    assert TenantLLMService.prepare_model_instance(db, "tenant", builtin, expected_type="embedding", allow_unregistered_builtin=True).tenant_id == "tenant"
    monkeypatch.setattr(TenantLLMService, "resolve_model_instance_config", classmethod(lambda cls, *args: {**model_config, "tenant_id": "foreign"}))
    with pytest.raises(LookupError, match="Resolved model"):
        TenantLLMService.prepare_model_instance(db, "tenant", model_config, expected_type="embedding")


async def test_prepared_chat_factory_receives_no_trace_identity(db: Session, model_config: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    model_config["mdl_type"] = "chat"
    prepared = TenantLLMService.prepare_model_instance(db, "tenant", model_config, expected_type="chat")
    calls: list[dict[str, Any]] = []

    class Provider:
        def __init__(self, key: str, name: str, base_url: str, **kwargs: Any) -> None:
            calls.append(kwargs)

    monkeypatch.setattr(llm, "ChatModel", {"OpenAI": Provider})
    with bundle_trace_scope("tenant", {"trace_id": "private-trace"}, "private-session", "private-user"):
        async with llm_bundle_scope():
            LLMBundle.from_prepared(prepared, verbose_tool_use=True)
    assert calls == [{"verbose_tool_use": True, "provider": "OpenAI"}]


async def test_prepared_trace_payload_zero_limit_and_usage_identity(db: Session, model_config: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    model_config.update(api_key=json.dumps({"api_key": "synthetic-secret", "region": "local", "is_tools": False}))
    prepared = TenantLLMService.prepare_model_instance(db, "tenant", model_config, expected_type="embedding")
    calls: list[dict[str, Any]] = []
    usage: list[tuple[str, int]] = []
    closed: list[bool] = []

    class Provider:
        def __init__(self, key: str, name: str, base_url: str) -> None:
            calls.append({"key": json.loads(key), "name": name, "base_url": base_url})

        def encode_queries(self, query: str) -> tuple[list[float], int]:
            return [1.0, 0.0], 7

        def close(self) -> None:
            closed.append(True)

    monkeypatch.setattr(llm, "EmbeddingModel", {"OpenAI": Provider})
    monkeypatch.setattr(TenantLLMService, "increase_usage_by_id", lambda model_id, count: usage.append((model_id, count)) or 1)
    with bundle_trace_scope("tenant", {"trace_id": "server-trace"}, "server-session", "server-user"):
        async with llm_bundle_scope():
            bundle = LLMBundle.from_prepared(prepared)
            assert bundle.db is None and bundle.max_length == 0
            assert bundle.trace_context == {"trace_id": "server-trace"}
            assert bundle.langfuse_session_id == "server-session"
            assert bundle.langfuse_user_id == "server-user"
            bundle.encode_queries("query")
    assert calls == [{"key": {"api_key": "synthetic-secret", "region": "local"}, "name": "controlled", "base_url": "http://model.invalid"}]
    assert usage == [("a" * 32, 7)] and closed == [True]


async def test_failed_tracer_keeps_provider_owned(db: Session, model_config: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    closed: list[bool] = []
    prepared = PreparedTenantLLM("tenant", deepcopy(model_config), ("public", "secret", "http://trace.invalid"))

    class Provider:
        def close(self) -> None:
            closed.append(True)

    def broken_trace(**kwargs: Any) -> None:
        raise RuntimeError("controlled tracer failure")

    monkeypatch.setattr(TenantLLMService, "model_instance_from_config", classmethod(lambda cls, *args, **kwargs: Provider()))
    monkeypatch.setattr(tenant_llm_service, "Langfuse", broken_trace)
    async with llm_bundle_scope():
        bundle = LLMBundle.from_prepared(prepared)
        assert bundle.langfuse is None
    assert closed == [True]


@pytest.mark.parametrize("break_encoder", [False, True])
async def test_context_counts_literal_special_tokens_or_propagates_encoder_failure(async_db: AsyncSession, monkeypatch: pytest.MonkeyPatch, break_encoder: bool) -> None:
    content = "<|endoftext|>" * 1000
    assert service._estimate_context_tokens(content) > 0
    binding = service.MemoryReadBinding("memory", "tenant", "a" * 32, "a" * 32)

    async def authorize(*args: Any, **kwargs: Any) -> service.MemoryReadBinding:
        return binding

    async def retrieve(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return [{"message_id": 1, "source_id": 0, "message_type": "raw", "memory_id": "memory", "user_id": "", "agent_id": "", "session_id": "", "status": True, "forget_at": None, "content": content}]

    monkeypatch.setattr(service, "require_memory", authorize)
    monkeypatch.setattr(service, "_search", retrieve)
    if break_encoder:

        def fail(text: str, **kwargs: Any) -> list[int]:
            raise RuntimeError("controlled encoder failure")

        monkeypatch.setattr(service, "encoder", SimpleNamespace(encode=fail))
        with pytest.raises(RuntimeError, match="controlled encoder failure"):
            await service.query_context(async_db, "memory", "query", actor_id="actor", budget_tokens=512)
    else:
        result = await service.query_context(async_db, "memory", "query", actor_id="actor", budget_tokens=512)
        assert result["items"] == [] and result["omitted_count"] == 1
        assert 0 < result["usage"]["estimated_tokens"] <= 512
