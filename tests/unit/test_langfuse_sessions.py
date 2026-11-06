"""Actual SDK v4 session attributes and bundle fail-open/cancellation contracts."""

import asyncio
import inspect
from collections.abc import AsyncIterator, Iterator
from typing import Any
from uuid import uuid4

import pytest
from langfuse import Langfuse
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy.orm import Session

from api.db.services import dialog_service, tenant_llm_service
from api.db.services.llm_service import LLMBundle
from api.db.services.tenant_llm_service import start_langfuse_observation


@pytest.fixture
def sdk() -> Iterator[tuple[Langfuse, InMemorySpanExporter]]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    client = Langfuse(public_key="pk-" + uuid4().hex, secret_key="fixture", base_url="http://langfuse.invalid", tracer_provider=provider, span_exporter=exporter)
    try:
        yield client, exporter
    finally:
        client.shutdown()
        provider.shutdown()


def test_real_sdk_groups_turns_and_isolates_tenant_user_session(sdk: tuple[Langfuse, InMemorySpanExporter]) -> None:
    client, exporter = sdk
    for tenant, user, session in [("a", "u", "s"), ("a", "u", "s"), ("b", "u", "s"), ("a", "v", "s"), ("a", "u", "t")]:
        observation = start_langfuse_observation(client, tenant_id=tenant, user_id=user, session_id=session, name="chat", as_type="generation", trace_context={"trace_id": client.create_trace_id()})
        observation.end()
    client.start_observation(name="unrelated").end()
    client.flush()
    spans = exporter.get_finished_spans()
    ids = [span.attributes.get("session.id") for span in spans if span.name == "chat"]
    assert len(ids) == 5 and ids[0] == ids[1] and len(set(ids)) == 4
    assert all(len(value) == 64 for value in ids)
    unrelated = next(span for span in spans if span.name == "unrelated")
    assert "session.id" not in unrelated.attributes and "user.id" not in unrelated.attributes
    assert "session_id" not in inspect.signature(Langfuse.start_observation).parameters


async def test_concurrent_sdk_observations_do_not_inherit_another_session(sdk: tuple[Langfuse, InMemorySpanExporter]) -> None:
    client, exporter = sdk

    async def turn(name: str) -> None:
        observation = start_langfuse_observation(client, tenant_id="tenant", user_id="user", session_id=name, name=name)
        await asyncio.sleep(0)
        observation.end()
        client.start_observation(name=name + "-outside").end()

    await asyncio.gather(turn("first"), turn("second"))
    client.flush()
    spans = {span.name: span.attributes for span in exporter.get_finished_spans()}
    assert spans["first"]["session.id"] != spans["second"]["session.id"]
    assert "session.id" not in spans["first-outside"] and "session.id" not in spans["second-outside"]


@pytest.mark.parametrize("failure", ["start", "update", "end", "model", "cancel", "close"])
async def test_bundle_observation_lifetime_preserves_model_result_or_error(monkeypatch: pytest.MonkeyPatch, db: Session, caplog: pytest.LogCaptureFixture, failure: str) -> None:
    ends: list[int] = []
    calls: list[dict[str, Any]] = []

    class Observation:
        def update(self, **kwargs: Any) -> None:
            if failure == "update":
                raise RuntimeError("secret-trace-credential")

        def end(self) -> None:
            ends.append(1)
            if failure == "end":
                raise RuntimeError("secret-trace-credential")

    class Tracer:
        def start_observation(self, **kwargs: Any) -> Observation:
            if failure == "start":
                raise RuntimeError("secret-trace-credential")
            return Observation()

    class Model:
        async def async_chat_streamly(self, *args: Any, **kwargs: Any) -> AsyncIterator[str | int]:
            calls.append(kwargs)
            if failure == "model":
                raise ValueError("model failed")
            if failure == "cancel":
                raise asyncio.CancelledError
            yield "answer"
            yield 0

    monkeypatch.setattr(tenant_llm_service.TenantLLMService, "model_instance", lambda *_args, **_kw: Model())
    monkeypatch.setattr(tenant_llm_service.TenantLangfuseService, "filter_by_tenant", lambda *_args, **_kw: None)
    bundle = LLMBundle(db, "owner", {"llm_name": "fixture", "llm_factory": "Builtin"}, trace_context={"trace_id": "1" * 32}, langfuse_session_id="session", langfuse_user_id="user")
    bundle.db = None
    bundle.langfuse = Tracer()
    stream = bundle.async_chat_streamly_delta("system", [])
    if failure == "model":
        with pytest.raises(ValueError, match="model failed"):
            _ = [value async for value in stream]
    elif failure == "cancel":
        with pytest.raises(asyncio.CancelledError):
            _ = [value async for value in stream]
    elif failure == "close":
        assert await anext(stream) == "answer"
        await stream.aclose()
    else:
        assert [value async for value in stream] == ["answer"]
    assert len(ends) == (0 if failure == "start" else 1)
    assert calls == [{}]  # Trace identities never enter model kwargs.
    assert bundle.db is None
    assert "secret-trace-credential" not in caplog.text


def test_trace_binding_preserves_owner_and_detached_session() -> None:
    bundles = [LLMBundle.__new__(LLMBundle), LLMBundle.__new__(LLMBundle)]
    for bundle, owner in zip(bundles, ["chat-owner", "dataset-owner"], strict=True):
        bundle.tenant_id, bundle.db = owner, None
    context = {"trace_id": "1" * 32}
    dialog_service._bind_chat_trace(tuple(bundles), context, "chat-owner", "session", "user")
    for bundle in bundles:
        assert bundle.db is None
        assert bundle.trace_context == context and bundle.langfuse_trace_tenant_id == "chat-owner"
        assert bundle.langfuse_session_id == "session"
    assert bundles[0].trace_context == context and bundles[0].trace_context is not context
    assert bundles[0].langfuse_session_id == "session"
    assert bundles[1].tenant_id == "dataset-owner"


def test_observation_does_not_inherit_or_mutate_ambient_identity(sdk: tuple[Langfuse, InMemorySpanExporter]) -> None:
    from langfuse import propagate_attributes

    client, exporter = sdk
    with propagate_attributes(session_id="ambient", user_id="ambient-user"):
        start_langfuse_observation(client, tenant_id="tenant", name="isolated").end()
        client.start_observation(name="ambient-restored").end()
    client.flush()
    spans = {span.name: span.attributes for span in exporter.get_finished_spans()}
    assert "session.id" not in spans["isolated"] and "user.id" not in spans["isolated"]
    assert spans["ambient-restored"]["session.id"] == "ambient"


@pytest.mark.parametrize("cancel", [False, True])
async def test_auxiliary_query_models_inherit_isolated_trace_and_close_owned_resources(monkeypatch: pytest.MonkeyPatch, db: Session, cancel: bool) -> None:
    models: list[Any] = []
    bundles: list[LLMBundle] = []
    ready = asyncio.Event()

    class Model:
        closed = False

        def close(self) -> None:
            self.closed = True

    def construct(*_args: Any, **_kwargs: Any) -> Model:
        model = Model()
        models.append(model)
        return model

    monkeypatch.setattr(tenant_llm_service.TenantLLMService, "model_instance", construct)
    monkeypatch.setattr(tenant_llm_service.TenantLangfuseService, "filter_by_tenant", lambda *_args, **_kw: None)

    async def query(owner: str) -> str:
        bundle = LLMBundle(db, owner, {"llm_name": "query", "llm_factory": "Builtin"})
        bundles.append(bundle)
        assert bundle.db is None
        ready.set()
        if cancel:
            await asyncio.Event().wait()
        await asyncio.sleep(0)
        assert bundle.langfuse_session_id == owner + "-session"
        return owner

    def invoke(owner: str) -> Any:
        return dialog_service._trace_chat_query(
            query(owner), tenant_id=owner, trace_context={"trace_id": ("1" if owner == "first" else "2") * 32}, session_id=owner + "-session", user_id=owner + "-user"
        )

    if cancel:
        task = asyncio.create_task(invoke("first"))
        await ready.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        assert await asyncio.gather(invoke("first"), invoke("second")) == ["first", "second"]
        assert bundles[0].langfuse_user_id != bundles[1].langfuse_user_id
    assert all(model.closed for model in models)
    outside = LLMBundle(db, "first", {"llm_name": "unrelated", "llm_factory": "Builtin"})
    assert outside.db is db and outside.langfuse_session_id is None and outside.trace_context == {}
    await outside.aclose()
