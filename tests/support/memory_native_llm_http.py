"""Shared controlled provider fixture for real native LLM HTTP consumers."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from typing import Any, cast

import pytest
from sqlalchemy.orm import Session

from api.db.db_models import Memory, TenantLLM
from api.db.services.tenant_llm_service import TenantLLMService
from tests.support.agent_update_release import release_api as release_api
from tests.support.memory_writes_http import WriteChat
from tests.support.memory_writes_http import writes_api as writes_api


@pytest.fixture
def native_api(writes_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    env = writes_api
    env["native_calls"] = []
    env["native_text"] = "仅演练。保留 0、false、[] 与字面 {{value}}，没有实际执行回执。"
    original = cast(Any, TenantLLMService.model_instance_from_config).__func__
    with Session(env["engine"]) as db:
        bank = db.get(Memory, env["memory_id"])
        assert bank is not None
        model = db.get(TenantLLM, bank.tenant_llm_id)
        assert model is not None
        model.max_tokens = 16384
        env["native_model_reference"] = str(model.id)
        db.commit()

    class NativeChat(WriteChat):
        async def async_chat_streamly(self, system: str, history: list[dict[str, Any]], gen_conf: dict[str, Any], **kwargs: Any) -> AsyncIterator[str | int]:
            env["native_calls"].append({"system": system, "history": history, "generation": gen_conf, "kwargs": kwargs})
            assert gen_conf["max_tokens"] == 512
            if "native_started" in env:
                env["native_started"].set()
                assert await asyncio.to_thread(env["native_release"].wait, 20), "Controlled provider was not released"
            text = env["native_text"]
            for piece in (text[:7], text[7:20], text[20:]):
                yield piece
                await asyncio.sleep(0)
            yield 0

    def factory(cls: type[TenantLLMService], config: dict[str, Any], lang: str = "Chinese", **kwargs: Any) -> Any:
        if config.get("mdl_type") == "chat":
            return NativeChat(env["chat_calls"])
        return original(cls, config, lang, **kwargs)

    monkeypatch.setattr(TenantLLMService, "model_instance_from_config", classmethod(factory))
    yield env
