"""Tenant-scoped model selection reaches real consumers and LLMBundle.

Only the external provider implementation is controlled; all model lookups use
the scratch PostgreSQL database and production resolution/configuration code.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

import core.llm as provider_models
from agent.canvas import Canvas
from agent.component import agent_with_tools, html_report, llm
from api.db.db_models import TenantLLM
from api.db.joint_services import memory_message_service
from api.db.services import dialog_service
from common import settings
from core.prompts import generator

CONSUMERS = ["llm", "agent", "html", "dialog", "memory", "full_question", "cross_languages"]


@pytest.fixture
def scoped_models(bootstrapped_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    owner, other, missing = (uuid4().hex for _ in range(3))
    factories = ["DeltaTextFixture", "DeltaVisionFixture"]
    calls: list[tuple[str, str]] = []
    constructed: list[str] = []

    class Provider:
        def __init__(self, key: str, name: str, *args: Any, **kwargs: Any) -> None:
            self.key = key
            constructed.append(key)

        async def async_chat(self, system: str, history: list[dict[str, Any]], config: dict[str, Any], **kwargs: Any) -> tuple[str, int]:
            calls.append((self.key, system))
            return "{}", 0

    monkeypatch.setattr(settings, "FACTORY_LLM_INFOS", [{"name": name} for name in factories])
    monkeypatch.setattr(provider_models, "ChatModel", {factories[0]: Provider})
    monkeypatch.setattr(provider_models, "CvModel", {factories[1]: Provider})
    with Session(bootstrapped_engine) as db:
        for tenant, factory, name, kind, status in [
            (owner, factories[0], "same", "chat", "1"),
            (owner, factories[1], "same", "image2text", "1"),
            (other, factories[0], "same", "embedding", "1"),
            (owner, factories[0], "unique", "chat", "1"),
            (owner, factories[0], "disabled", "chat", "0"),
            (owner, factories[0], "untyped", None, "1"),
        ]:
            db.add(TenantLLM(tenant_id=tenant, llm_factory=factory, llm_name=name, mdl_type=kind, status=status, api_key=tenant + factory, api_base="https://unused.invalid", max_tokens=1024))
        db.commit()
    with Session(bootstrapped_engine) as reader:
        before = [row.to_dict() for row in reader.scalars(select(TenantLLM).where(TenantLLM.tenant_id.in_([owner, other])).order_by(TenantLLM.id))]

    @contextmanager
    def scratch_db() -> Iterator[Session]:
        with Session(bootstrapped_engine) as db:
            yield db

    for module in [llm, agent_with_tools, html_report, generator]:
        monkeypatch.setattr(module, "db_connection", scratch_db)

    def forbid_global_session(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Model consumer opened an unscoped global database session")

    monkeypatch.setattr("api.db.db_models.SessionLocal", forbid_global_session)
    try:
        yield {"engine": bootstrapped_engine, "owner": owner, "missing": missing, "calls": calls, "constructed": constructed}
    finally:
        with Session(bootstrapped_engine) as reader:
            assert [row.to_dict() for row in reader.scalars(select(TenantLLM).where(TenantLLM.tenant_id.in_([owner, other])).order_by(TenantLLM.id))] == before
            reader.execute(delete(TenantLLM).where(TenantLLM.tenant_id.in_([owner, other])))
            reader.commit()
        with Session(bootstrapped_engine) as reader:
            assert reader.scalar(select(TenantLLM.id).where(TenantLLM.tenant_id.in_([owner, other]))) is None


async def call_consumer(env: dict[str, Any], consumer: str, tenant: str, reference: str) -> bool:
    if consumer in {"llm", "agent", "html"}:
        canvas = object.__new__(Canvas)
        canvas._tenant_id = tenant
        canvas.tool_use_callback = lambda *args, **kwargs: None
        if consumer == "agent":
            param = agent_with_tools.AgentParam()
            component = agent_with_tools.Agent
        elif consumer == "html":
            param = html_report.HTMLReportParam()
            param.skeleton = {"sections": []}
            component = html_report.HTMLReport
        else:
            param = llm.LLMParam()
            component = llm.LLM
        param.llm_id = reference
        instance = component(canvas, consumer + ":delta", param)
        if instance.chat_mdl is None:
            return False
        assert await instance.chat_mdl.async_chat("probe", [{"role": "user", "content": "hello"}]) == "{}"
    elif consumer == "dialog":
        dialog = SimpleNamespace(tenant_id=tenant, llm_id=reference, tenant_llm_id=None, prompt_config={}, llm_setting={})
        with Session(env["engine"]) as db:
            result = [item async for item in dialog_service.async_chat_solo(db, dialog, [{"role": "user", "content": "hello"}], stream=False)]
        assert result[0]["answer"] == "{}"
    elif consumer == "memory":
        with Session(env["engine"]) as db:
            assert await memory_message_service.extract_by_llm(db, tenant, reference, None, {}, ["raw"], "hello", "world", system_prompt="probe", user_prompt="extract") == []
    elif consumer == "full_question":
        assert await generator.full_question(tenant, reference, [{"role": "user", "content": "hello"}]) == "{}"
    else:
        assert consumer == "cross_languages"
        assert await generator.cross_languages(tenant, reference, "hello", ["English"]) == "{}"
    return True


@pytest.mark.parametrize("consumer", CONSUMERS)
@pytest.mark.parametrize("reference,factory", [("same@DeltaTextFixture", "DeltaTextFixture"), ("same@DeltaVisionFixture", "DeltaVisionFixture"), ("unique", "DeltaTextFixture")])
async def test_tenant_model_selection_reaches_consumer(scoped_models: dict[str, Any], consumer: str, reference: str, factory: str) -> None:
    env = scoped_models
    assert await call_consumer(env, consumer, env["owner"], reference)
    assert len(env["calls"]) == 1
    assert env["calls"][0][0] == env["owner"] + factory
    assert set(env["constructed"]) == {env["owner"] + factory}


@pytest.mark.parametrize("consumer", CONSUMERS)
@pytest.mark.parametrize("failure", ["ambiguous", "disabled", "untyped", "foreign"])
async def test_rejected_model_never_reaches_provider(scoped_models: dict[str, Any], consumer: str, failure: str) -> None:
    env = scoped_models
    tenant = env["missing"] if failure == "foreign" else env["owner"]
    reference = "same@DeltaTextFixture" if failure == "foreign" else "same" if failure == "ambiguous" else failure
    if consumer in {"llm", "agent", "html"}:
        assert not await call_consumer(env, consumer, tenant, reference)
    else:
        with pytest.raises(LookupError):
            await call_consumer(env, consumer, tenant, reference)
    assert env["constructed"] == env["calls"] == []
