"""add_llm 探测失败时的可诊断性钉板测试。

修复前的三个黑洞：
1. 上游报错以 "**ERROR**: ..." 字符串 yield 出来，探测循环只判断有没有它、不记录内容，
   最后统一抛 "No valid response received"，401/404/参数错误全部丢失；
2. 探测超时用 str(TimeoutError()) 拼消息，得到空串，用户看到的是一句没有下文的失败；
3. 保存失败走 HTTPException(400) → {"detail": ...} 不带 retcode，前端当成正常数据放过，
   表现为「点保存没反应、也没报错、库里没记录」。
"""

import asyncio
import json
import time
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.orm import Session

from api.apps import llm_app
from api.apps.llm_app import AddLLMRequest, add_llm
from api.db.services.tenant_llm_service import TenantLLMService

FACTORY = "OpenAI-API-Compatible"
ZHIPU_V4 = "https://open.bigmodel.cn/api/paas/v4"


class _FakeChat:
    """记录构造参数的假 chat 模型；yield 的内容由 chunks 决定。"""

    instances: list["_FakeChat"] = []

    chunks: list[Any] = ["Hi", 2]
    delay: float = 0.0

    def __init__(self, key=None, model_name="", base_url="", **kwargs):
        self.key = key
        self.model_name = model_name
        self.base_url = base_url
        self.kwargs = kwargs
        _FakeChat.instances.append(self)

    async def async_chat_streamly(self, system, history, gen_conf, **kwargs):
        if _FakeChat.delay:
            await asyncio.sleep(_FakeChat.delay)
        for chunk in _FakeChat.chunks:
            yield chunk


@pytest.fixture
def probe_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """把工厂白名单、注册表与落库都换成可观测的假实现。"""
    _FakeChat.instances = []
    _FakeChat.chunks = ["Hi", 2]
    _FakeChat.delay = 0.0

    monkeypatch.setattr(llm_app, "get_allowed_llm_factories", lambda db: [SimpleNamespace(name=FACTORY)])
    monkeypatch.setattr(llm_app, "ChatModel", {FACTORY: _FakeChat})

    saved: list[dict[str, Any]] = []
    monkeypatch.setattr(TenantLLMService, "filter_update", classmethod(lambda cls, db, conds, payload: False))
    monkeypatch.setattr(TenantLLMService, "save", classmethod(lambda cls, db, **payload: saved.append(payload)))
    return {"saved": saved}


def _request(*, drop: tuple[str, ...] = (), **overrides: Any) -> AddLLMRequest:
    payload: dict[str, Any] = {
        "llm_factory": FACTORY,
        "llm_name": "glm-4.6",
        "mdl_type": "chat",
        "api_key": "sk-test",
        "api_base": ZHIPU_V4,
    }
    payload.update(overrides)
    for key in drop:
        payload.pop(key, None)
    return AddLLMRequest(**payload)


def _body(response) -> dict[str, Any]:
    return json.loads(response.body)


async def _call(request: AddLLMRequest, db: Session) -> dict[str, Any]:
    return _body(await add_llm(request, db, SimpleNamespace(id="tenant-1")))


@pytest.mark.asyncio
async def test_upstream_error_is_surfaced_verbatim(probe_env: dict[str, Any], db: Session) -> None:
    _FakeChat.chunks = ["**ERROR**: AUTH_ERROR - Error code: 401 - {'error': {'message': 'invalid api key'}}", 0]

    body = await _call(_request(), db)

    assert body["retcode"] != 0
    assert "401" in body["retmsg"]
    assert "invalid api key" in body["retmsg"]
    assert not probe_env["saved"]


@pytest.mark.asyncio
@pytest.mark.parametrize("env_var", ["LLM_PROBE_TIMEOUT_SECONDS", "LLM_TIMEOUT_SECONDS"])
async def test_timeout_message_is_not_empty(probe_env: dict[str, Any], db: Session, monkeypatch: pytest.MonkeyPatch, env_var: str) -> None:
    monkeypatch.setenv(env_var, "1")
    _FakeChat.delay = 5.0

    body = await _call(_request(), db)

    assert body["retcode"] != 0
    assert "timed out after 1s" in body["retmsg"]
    # 提示要指向探测专用变量：LLM_TIMEOUT_SECONDS 同时是生产客户端的 transport 超时
    assert "LLM_PROBE_TIMEOUT_SECONDS" in body["retmsg"]
    assert not probe_env["saved"]


def test_probe_timeout_prefers_dedicated_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.apps.llm_app import probe_timeout_seconds

    monkeypatch.delenv("LLM_PROBE_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("LLM_TIMEOUT_SECONDS", raising=False)
    # 默认值必须留在前端 30s abort 之内，否则诊断文案到不了界面
    assert probe_timeout_seconds() < 30

    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "600")
    assert probe_timeout_seconds() == 600

    monkeypatch.setenv("LLM_PROBE_TIMEOUT_SECONDS", "20")
    assert probe_timeout_seconds() == 20


@pytest.mark.asyncio
async def test_no_content_at_all_still_reports_something(probe_env: dict[str, Any], db: Session) -> None:
    _FakeChat.chunks = ["", 0]

    body = await _call(_request(), db)

    assert body["retcode"] != 0
    assert "No valid response received" in body["retmsg"]


@pytest.mark.asyncio
async def test_probe_disables_retry_backoff(probe_env: dict[str, Any], db: Session) -> None:
    await _call(_request(), db)

    assert _FakeChat.instances[0].kwargs["max_retries"] == 0
    assert _FakeChat.instances[0].kwargs["provider"] == FACTORY


@pytest.mark.asyncio
@pytest.mark.parametrize("request_kwargs", [{"drop": ("api_key",)}, {"api_key": ""}])
async def test_blank_api_key_does_not_reach_sdk_as_none(probe_env: dict[str, Any], db: Session, request_kwargs: dict[str, Any]) -> None:
    # 前端把 API Key 标为可选：字段缺失时 model_dump() 仍带 api_key=None，
    # 旧写法 req.get("api_key", "x") 取到的是 None，OpenAI(api_key=None) 直接抛异常。
    await _call(_request(**request_kwargs), db)

    assert _FakeChat.instances[0].key == "x"


@pytest.mark.asyncio
async def test_unsupported_factory_is_a_message_not_a_crash(probe_env: dict[str, Any], db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm_app, "ChatModel", {})

    body = await _call(_request(), db)

    assert body["retcode"] != 0
    assert "is not supported yet" in body["retmsg"]


@pytest.mark.asyncio
async def test_constructor_failure_is_a_message_not_a_500(probe_env: dict[str, Any], db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    class _Exploding(_FakeChat):
        def __init__(self, key=None, model_name="", base_url="", **kwargs):
            raise ValueError("url cannot be None")

    monkeypatch.setattr(llm_app, "ChatModel", {FACTORY: _Exploding})

    body = await _call(_request(api_base=""), db)

    assert body["retcode"] != 0
    assert "url cannot be None" in body["retmsg"]


@pytest.mark.asyncio
async def test_probe_success_persists_with_suffixed_name(probe_env: dict[str, Any], db: Session) -> None:
    body = await _call(_request(), db)

    assert body["retcode"] == 0
    assert probe_env["saved"][0]["llm_name"] == "glm-4.6___OpenAI-API"
    assert probe_env["saved"][0]["api_base"] == ZHIPU_V4
    # 探测请求打到的是用户填的 v4 根，不是被改写过的地址
    assert _FakeChat.instances[0].base_url == ZHIPU_V4
    assert _FakeChat.instances[0].model_name == "glm-4.6"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("upstream", "expected_code"),
    [
        # 不可重试类：改前就能立刻拿到错误
        ("Error code: 401 - {'error': {'message': 'invalid api key'}}", "AUTH_ERROR"),
        # 可重试类：这两类才是关键——只判「分类可重试」不判「还有剩余次数」时，
        # max_retries=0 也会先睡 20~300s，然后一个 chunk 都不 yield，错误被整条吞掉
        ("Error code: 429 - {'error': {'message': 'Rate limit reached'}}", "RATE_LIMIT_EXCEEDED"),
        ("litellm.InternalServerError: AnthropicException - Overloaded (529)", "SERVER_ERROR"),
    ],
)
async def test_disabled_retry_keeps_real_error_classification_and_does_not_back_off(upstream: str, expected_code: str) -> None:
    """探测用 max_retries=0：既不能退避（20~300s 会睡穿超时），也不能把真实分类盖成 MAX_RETRIES_EXCEEDED。"""
    from core.llm.chat import Base

    class _Failing:
        class chat:
            class completions:
                @staticmethod
                async def create(**kwargs):
                    raise RuntimeError(upstream)

    mdl = Base(key="k", model_name="glm-4.6", base_url="https://open.bigmodel.cn/api/paas/v4", max_retries=0)
    mdl.async_client = _Failing()

    started = asyncio.get_running_loop().time()
    chunks = [chunk async for chunk in mdl.async_chat_streamly("", [{"role": "user", "content": "Hi"}], {})]
    elapsed = asyncio.get_running_loop().time() - started

    assert elapsed < 1.0
    assert chunks, "max_retries=0 时必须 yield 出错误，不能静默结束"
    assert expected_code in chunks[0]
    assert "MAX_RETRIES_EXCEEDED" not in chunks[0]
    assert upstream in chunks[0]


@pytest.mark.asyncio
async def test_retry_still_happens_when_retries_are_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """max_retries>0 的生产路径行为不能被上一条的守卫改掉：仍要重试到耗尽再报 MAX_RETRIES_EXCEEDED。"""
    from core.llm import chat as chat_module

    slept: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(chat_module.asyncio, "sleep", _fake_sleep)

    class _RateLimited:
        class chat:
            class completions:
                @staticmethod
                async def create(**kwargs):
                    raise RuntimeError("Error code: 429 - Rate limit reached")

    mdl = chat_module.Base(key="k", model_name="glm-4.6", base_url="https://x/v1", max_retries=3)
    mdl.async_client = _RateLimited()

    chunks = [chunk async for chunk in mdl.async_chat_streamly("", [{"role": "user", "content": "Hi"}], {})]

    assert len(slept) == 3
    assert "MAX_RETRIES_EXCEEDED" in chunks[0]
    assert "Rate limit reached" in chunks[0]


class TestSetApiKeyProbe:
    """set_api_key 是「选 ZHIPU-AI / Tongyi-Qianwen 原生厂商 + 只填 key」的那条路径，
    也是我们推荐用户走的路，同一套诊断能力必须在这里也成立。"""

    @pytest.fixture
    def factory_env(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        _FakeChat.instances = []
        _FakeChat.chunks = ["Hi", 2]
        _FakeChat.delay = 0.0

        monkeypatch.setattr(llm_app, "ChatModel", {"ZHIPU-AI": _FakeChat})
        monkeypatch.setattr(
            llm_app.LLMService,
            "query",
            classmethod(lambda cls, db, **kw: [SimpleNamespace(fid="ZHIPU-AI", llm_name="glm-4.6", mdl_type="chat", max_tokens=128000)]),
        )
        saved: list[dict[str, Any]] = []
        monkeypatch.setattr(TenantLLMService, "filter_update", classmethod(lambda cls, db, conds, payload: False))
        monkeypatch.setattr(TenantLLMService, "save", classmethod(lambda cls, db, **payload: saved.append(payload)))
        return {"saved": saved}

    async def _call(self, db: Session, **overrides: Any) -> dict[str, Any]:
        payload = {"llm_factory": "ZHIPU-AI", "api_key": "sk-test"}
        payload.update(overrides)
        return _body(await llm_app.set_api_key(llm_app.SetAPIKeyRequest(**payload), db, SimpleNamespace(id="tenant-1")))

    @pytest.mark.asyncio
    async def test_upstream_error_is_surfaced(self, factory_env: dict[str, Any], db: Session) -> None:
        _FakeChat.chunks = ["**ERROR**: AUTH_ERROR - Error code: 401 - {'error': {'message': 'invalid api key'}}", 0]

        body = await self._call(db)

        assert body["retcode"] != 0
        assert "invalid api key" in body["retmsg"]
        assert not factory_env["saved"]

    @pytest.mark.asyncio
    async def test_timeout_message_is_not_empty(self, factory_env: dict[str, Any], db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LLM_PROBE_TIMEOUT_SECONDS", "1")
        _FakeChat.delay = 5.0

        body = await self._call(db)

        assert body["retcode"] != 0
        assert "timed out after 1s" in body["retmsg"]

    @pytest.mark.asyncio
    async def test_probe_disables_retry_backoff(self, factory_env: dict[str, Any], db: Session) -> None:
        await self._call(db)

        assert _FakeChat.instances[0].kwargs["max_retries"] == 0

    @pytest.mark.asyncio
    async def test_total_budget_is_not_multiplied_by_model_count(self, factory_env: dict[str, Any], db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
        """三种模型类型串行探测时总耗时不能是 3 倍超时，否则必然超过前端的请求 abort。"""
        monkeypatch.setenv("LLM_PROBE_TIMEOUT_SECONDS", "1")
        monkeypatch.setattr(
            llm_app.LLMService,
            "query",
            classmethod(
                lambda cls, db, **kw: [
                    SimpleNamespace(fid="ZHIPU-AI", llm_name="embedding-3", mdl_type="embedding", max_tokens=3072),
                    SimpleNamespace(fid="ZHIPU-AI", llm_name="glm-4.6", mdl_type="chat", max_tokens=128000),
                    SimpleNamespace(fid="ZHIPU-AI", llm_name="rerank", mdl_type="rerank", max_tokens=8192),
                ]
            ),
        )

        class _Hanging:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                pass

            def encode(self, texts: list[str]) -> tuple[list[list[float]], int]:
                time.sleep(5)
                return [[0.1]], 1

            def similarity(self, query: str, texts: list[str]) -> tuple[list[float], int]:
                time.sleep(5)
                return [0.1], 1

        monkeypatch.setattr(llm_app, "EmbeddingModel", {"ZHIPU-AI": _Hanging})
        monkeypatch.setattr(llm_app, "RerankModel", {"ZHIPU-AI": _Hanging})
        _FakeChat.delay = 5.0

        started = asyncio.get_running_loop().time()
        body = await self._call(db)
        elapsed = asyncio.get_running_loop().time() - started

        assert body["retcode"] != 0
        assert elapsed < 3.0, f"三个模型各 1s 预算，总耗时不应线性叠加，实测 {elapsed:.1f}s"
        assert not factory_env["saved"]

    @pytest.mark.asyncio
    async def test_success_registers_the_whole_catalog(self, factory_env: dict[str, Any], db: Session) -> None:
        body = await self._call(db, base_url="https://open.bigmodel.cn/api/paas/v4")

        assert body["retcode"] == 0
        assert factory_env["saved"][0]["llm_name"] == "glm-4.6"
        assert factory_env["saved"][0]["api_base"] == "https://open.bigmodel.cn/api/paas/v4"


@pytest.mark.asyncio
async def test_verify_mode_reports_message_without_saving(probe_env: dict[str, Any], db: Session) -> None:
    _FakeChat.chunks = ["**ERROR**: MODEL_ERROR - model not found", 0]

    body = await _call(_request(verify=True), db)

    assert body["data"]["success"] is False
    assert "model not found" in body["data"]["message"]
    assert not probe_env["saved"]
