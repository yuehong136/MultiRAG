from contextlib import asynccontextmanager, contextmanager
from unittest.mock import AsyncMock, MagicMock

from api.db.services.llm_service import LLMBundle
from api.db.services.tenant_llm_service import TenantLLMService

MODEL_ID = "7" * 32


def test_record_usage_uses_model_config_id(monkeypatch):
    """_record_usage 通过 model_config 缓存的租户模型 id 计量，不触碰调用方会话。"""
    bundle = object.__new__(LLMBundle)
    bundle.db = MagicMock()
    bundle.model_config = {"id": MODEL_ID, "llm_factory": "OpenAI", "llm_name": "glm-4-airx"}

    calls = []

    def fake_increase(model_id, used_tokens):
        calls.append((model_id, used_tokens))
        return 1

    monkeypatch.setattr(TenantLLMService, "increase_usage_by_id", fake_increase)

    assert bundle._record_usage(128) is True
    assert calls == [(MODEL_ID, 128)]
    bundle.db.commit.assert_not_called()
    bundle.db.rollback.assert_not_called()


def test_record_usage_skips_builtin_factory():
    bundle = object.__new__(LLMBundle)
    bundle.model_config = {"llm_factory": "Builtin"}

    assert bundle._record_usage(128) is True


def test_record_usage_short_circuits_non_positive_tokens():
    bundle = object.__new__(LLMBundle)
    bundle.model_config = {}

    assert bundle._record_usage(0) is True
    assert bundle._record_usage(-1) is True


async def test_record_usage_async_uses_model_config_id(monkeypatch):
    """_record_usage_async 与同步版同语义:按缓存的租户模型 id 记账,不触碰调用方会话。"""
    bundle = object.__new__(LLMBundle)
    bundle.db = MagicMock()
    bundle.model_config = {"id": MODEL_ID, "llm_factory": "OpenAI", "llm_name": "glm-4-airx"}

    calls = []

    async def fake_increase(model_id, used_tokens):
        calls.append((model_id, used_tokens))
        return 1

    monkeypatch.setattr(TenantLLMService, "increase_usage_by_id_async", fake_increase)

    assert await bundle._record_usage_async(128) is True
    assert calls == [(MODEL_ID, 128)]
    bundle.db.commit.assert_not_called()


async def test_record_usage_async_skips_builtin_and_non_positive():
    bundle = object.__new__(LLMBundle)
    bundle.model_config = {"llm_factory": "Builtin"}
    assert await bundle._record_usage_async(128) is True

    bundle.model_config = {}
    assert await bundle._record_usage_async(0) is True
    assert await bundle._record_usage_async(-1) is True


async def test_increase_usage_by_id_async_uses_own_async_session(monkeypatch):
    """异步记账开独立短 AsyncSession,execute+commit 各一次。"""
    session = MagicMock()
    session.execute = AsyncMock(return_value=MagicMock(rowcount=1))
    session.commit = AsyncMock()

    @asynccontextmanager
    async def fake_factory():
        yield session

    monkeypatch.setattr("api.db.services.tenant_llm_service.async_session_factory", fake_factory)

    assert await TenantLLMService.increase_usage_by_id_async(MODEL_ID, 128) == 1
    session.execute.assert_awaited_once()
    session.commit.assert_awaited_once()


async def test_increase_usage_by_id_async_falls_back_without_async_engine(monkeypatch):
    """非 PG 后端(async_session_factory=None)退回线程池跑同步版。"""
    calls = []

    def fake_sync(model_id, used_tokens):
        calls.append((model_id, used_tokens))
        return 1

    monkeypatch.setattr("api.db.services.tenant_llm_service.async_session_factory", None)
    monkeypatch.setattr(TenantLLMService, "increase_usage_by_id", fake_sync)

    assert await TenantLLMService.increase_usage_by_id_async(MODEL_ID, 64) == 1
    assert calls == [(MODEL_ID, 64)]


async def test_increase_usage_by_id_async_best_effort_swallows_errors(monkeypatch):
    """任何 DB 异常吞掉返回 0,不影响主链路。"""

    @asynccontextmanager
    async def broken_factory():
        raise RuntimeError("db down")
        yield

    monkeypatch.setattr("api.db.services.tenant_llm_service.async_session_factory", broken_factory)

    assert await TenantLLMService.increase_usage_by_id_async(MODEL_ID, 128) == 0
    assert await TenantLLMService.increase_usage_by_id_async(None, 128) == 0
    assert await TenantLLMService.increase_usage_by_id_async(MODEL_ID, 0) == 0


def test_increase_usage_by_id_uses_independent_session(monkeypatch):
    usage_db = MagicMock()
    usage_db.execute.return_value.rowcount = 1

    @contextmanager
    def fake_db_connection():
        yield usage_db

    monkeypatch.setattr("api.db.services.tenant_llm_service.db_connection", fake_db_connection)
    assert TenantLLMService.increase_usage_by_id(MODEL_ID, 256) == 1
    statement = usage_db.execute.call_args.args[0]
    assert MODEL_ID in statement.compile().params.values()
    usage_db.commit.assert_called_once()


async def test_legacy_ids_do_not_open_accounting_session(monkeypatch):
    def forbid_session():
        raise AssertionError("A retired model identity opened an accounting session")

    monkeypatch.setattr("api.db.services.tenant_llm_service.db_connection", forbid_session)
    monkeypatch.setattr("api.db.services.tenant_llm_service.async_session_factory", forbid_session)
    assert TenantLLMService.increase_usage_by_id("7", 128) == 0
    assert await TenantLLMService.increase_usage_by_id_async("7", 128) == 0
