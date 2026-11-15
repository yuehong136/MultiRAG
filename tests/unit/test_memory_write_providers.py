"""Ingestion models use recorded sources, released SQL and strict outputs."""

import json
import threading
from dataclasses import replace
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.services.tenant_llm_service import PreparedTenantLLM
from core import llm
from memory.revisions.domain import RevisionConflict, RevisionValidationError
from memory.writes.providers import MemoryWriteProviders, MemoryWriteSettings, embedding_binding, parse_extractions


def prepared(capability: str, **changes: Any) -> PreparedTenantLLM:
    return PreparedTenantLLM(
        "tenant",
        {
            "id": 71,
            "tenant_id": "tenant",
            "llm_factory": "Controlled",
            "llm_name": "model",
            "mdl_type": capability,
            "api_key": "synthetic-private",
            "api_base": "http://model.invalid",
            "max_tokens": 8192,
            **changes,
        },
        None,
    )


def configuration(**changes: Any) -> MemoryWriteSettings:
    return replace(MemoryWriteSettings("bank", "tenant", "embedding", 71, "chat", 72, 3, 0.5, "", ""), **changes)


def raw(**changes: Any) -> dict[str, Any]:
    return {
        "memory_id": "bank",
        "message_id": 1,
        "source_id": 0,
        "message_type": "raw",
        "status": True,
        "content": "User Input: last year\nAgent Response: recorded answer",
        "valid_at": "2025-12-31 23:59:58",
        **changes,
    }


def test_model_fingerprint_excludes_usage_and_changes_with_credentials() -> None:
    value = embedding_binding(prepared("embedding"), 2)
    assert value == embedding_binding(prepared("embedding", used_tokens=12, update_time=123), 2)
    assert value != embedding_binding(prepared("embedding", api_key="changed"), 2)
    assert "synthetic-private" not in value.config_reference


@pytest.mark.parametrize(
    "response",
    ["", "not json", "[]", '{"raw":[]}', '{"semantic":{}}', '{"semantic":[{"content":" ","valid_at":"2025-01-01 00:00:00"}]}', '{"semantic":[{"content":"fact","valid_at":"2025-02-30 00:00:00"}]}'],
)
def test_invalid_model_output_is_not_an_empty_success(response: str) -> None:
    with pytest.raises(RevisionValidationError):
        parse_extractions(response, {"semantic"})


def test_extraction_constructs_host_fields_and_preserves_literal_content() -> None:
    result = parse_extractions('```json\n{"semantic":[{"content":"  a fact\\n","valid_at":"2025-01-01T10:00:00Z","tenant_id":"foreign","assessment":"verified"}]}\n```', {"semantic"})
    assert result == [{"message_type": "semantic", "content": "  a fact\n", "valid_at": "2025-01-01 10:00:00", "invalid_at": None}]
    assert parse_extractions("{}", {"semantic"}) == []
    with pytest.raises(RevisionValidationError, match="limit"):
        parse_extractions(json.dumps({"semantic": [{"content": "fact", "valid_at": "2025-01-01T10:00:00"}] * 101}), {"semantic"})


async def test_embedding_constructs_off_loop_after_releasing_sql(async_db: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = MemoryWriteProviders(async_db, tenant_id="tenant", actor_id="actor")
    loop_thread = threading.get_ident()
    closed: list[bool] = []

    async def prepare(memory_id: str, capability: str, **kwargs: Any) -> tuple[MemoryWriteSettings, PreparedTenantLLM]:
        assert memory_id == "bank" and capability == "embedding"
        return configuration(), prepared(capability)

    class Provider:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            assert not async_db.in_transaction() and threading.get_ident() != loop_thread

        def encode(self, texts: list[str]) -> tuple[list[list[float]], int]:
            assert texts == ["literal content"] and not async_db.in_transaction()
            return [[1.0, 0.0]], 0

        def close(self) -> None:
            closed.append(True)

    monkeypatch.setattr(runtime, "_prepared", prepare)
    monkeypatch.setitem(llm.EmbeddingModel, "Controlled", Provider)
    await async_db.begin()
    result = await runtime.embed("bank", [{"content": "literal content"}], None)
    assert result["vectors"] == [[1.0, 0.0]] and result["model_binding"].dimension == 2
    assert closed == [True]


async def test_embedding_rejects_changed_binding_before_provider_construction(async_db: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = MemoryWriteProviders(async_db, tenant_id="tenant", actor_id="actor")

    async def prepare(*args: Any, **kwargs: Any) -> tuple[MemoryWriteSettings, PreparedTenantLLM]:
        return configuration(), prepared("embedding", api_key="changed")

    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("Changed model must not execute")

    monkeypatch.setattr(runtime, "_prepared", prepare)
    monkeypatch.setitem(llm.EmbeddingModel, "Controlled", forbidden)
    with pytest.raises(RevisionConflict, match="model_changed"):
        await runtime.embed("bank", [{"content": "fact"}], embedding_binding(prepared("embedding"), 2))


@pytest.mark.parametrize("changed", [False, True])
async def test_extraction_uses_recorded_source_and_rechecks_settings(async_db: AsyncSession, monkeypatch: pytest.MonkeyPatch, changed: bool) -> None:
    runtime = MemoryWriteProviders(async_db, tenant_id="tenant", actor_id="actor")
    calls: list[str] = []
    observed: list[list[dict[str, str]]] = []
    closed: list[bool] = []

    async def settings(*args: Any, **kwargs: Any) -> MemoryWriteSettings:
        return configuration()

    async def prepare(memory_id: str, capability: str, **kwargs: Any) -> tuple[MemoryWriteSettings, PreparedTenantLLM]:
        calls.append(capability)
        return configuration(temperature=0.8 if changed and len(calls) > 1 else 0.5), prepared(capability)

    class Provider:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            assert not async_db.in_transaction()

        async def async_chat(self, system: str, history: list[dict[str, str]], generation: dict[str, Any], **kwargs: Any) -> tuple[str, int]:
            assert not async_db.in_transaction()
            observed.append(history)
            return '{"semantic":[{"content":"fact","valid_at":"2025-12-31T23:59:58"}]}', 0

        def close(self) -> None:
            closed.append(True)

    monkeypatch.setattr(runtime, "settings", settings)
    monkeypatch.setattr(runtime, "_prepared", prepare)
    monkeypatch.setitem(llm.ChatModel, "Controlled", Provider)
    await async_db.begin()
    if changed:
        with pytest.raises(RevisionConflict, match="configuration_changed"):
            await runtime.extract("bank", raw())
    else:
        result = await runtime.extract("bank", raw())
        assert result["items"][0]["content"] == "fact"
        assert result["model_reference"] == "model@Controlled"
    assert raw()["content"] in observed[0][-1]["content"] and "2025-12-31 23:59:58" in observed[0][-1]["content"]
    assert calls == ["chat", "chat"] and closed == [True]


async def test_raw_only_memory_needs_no_chat_binding(async_db: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = MemoryWriteProviders(async_db, tenant_id="tenant", actor_id="actor")

    async def settings(*args: Any, **kwargs: Any) -> MemoryWriteSettings:
        return configuration(memory_type=1)

    async def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("RAW-only memory must not prepare a chat provider")

    monkeypatch.setattr(runtime, "settings", settings)
    monkeypatch.setattr(runtime, "_prepared", forbidden)
    assert (await runtime.extract("bank", raw()))["items"] == []


async def test_invalid_source_fails_before_model_preparation(async_db: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = MemoryWriteProviders(async_db, tenant_id="tenant", actor_id="actor")

    async def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("Invalid source must not reach a model")

    monkeypatch.setattr(runtime, "_prepared", forbidden)
    for source in [raw(memory_id="other"), raw(status=False), raw(valid_at="yesterday")]:
        with pytest.raises((RevisionConflict, RevisionValidationError)):
            await runtime.extract("bank", source)
