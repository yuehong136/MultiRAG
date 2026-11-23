"""Failure receipts, exact replay identity and official SDK retry boundaries."""

import json
from typing import Any, cast

import httpx
import pytest
import sqlalchemy as sa
from openai import AsyncOpenAI
from sqlalchemy.orm import Session

from api.db.db_models import Memory, TenantModel, TenantModelInstance, TenantModelProvider
from api.db.memory_compaction_models import MemoryCompactionArtifact
from api.db.memory_writer_models import MemoryWriterAdmission
from api.db.services.tenant_llm_service import TenantLLMService
from memory.compaction.service import CompactionService
from memory.revisions.domain import RevisionPersistenceUnknown
from tests.support.memory_compaction_http import compaction_api as compaction_api
from tests.support.memory_compaction_http import release_api as release_api
from tests.support.memory_compaction_http import request
from tests.support.memory_compaction_http import writes_api as writes_api
from tests.support.memory_writes_http import api_request, result

_REAL_MODEL_FACTORY: Any = cast(Any, TenantLLMService.model_instance_from_config).__func__


def test_pre_model_budget_rejection_has_no_operation_or_model_effect(compaction_api: dict[str, Any]) -> None:
    env, mid = compaction_api, compaction_api["memory_id"]
    body = {**request(env), "input_budget_tokens": 1024}
    response = api_request(env, "POST", f"/memories/{mid}/context-compactions", json=body)
    assert response.status_code == 422 and response.json()["data"]["error_code"] == "compaction_input_budget_exceeded"
    assert env["summary_calls"] == []
    with Session(env["engine"]) as reader:
        assert reader.get(MemoryWriterAdmission, (mid, body["operation_id"])) is None
        assert reader.scalar(sa.select(sa.func.count()).select_from(MemoryCompactionArtifact).where(MemoryCompactionArtifact.memory_id == mid)) == 0


def test_existing_operation_is_not_discarded_after_invalid_422(compaction_api: dict[str, Any]) -> None:
    env, mid = compaction_api, compaction_api["memory_id"]
    body = request(env)
    initial = result(api_request(env, "POST", f"/memories/{mid}/context-compactions", json=body))
    invalid = api_request(env, "POST", f"/memories/{mid}/context-compactions", json={**body, "prefix_count": True})
    assert invalid.status_code == 422
    current = result(api_request(env, "GET", f"/memories/{mid}/context-compaction-operations/{body['operation_id']}"))
    assert current == initial and len(env["summary_calls"]) == 1


@pytest.mark.parametrize("boundary", ["before", "after"])
def test_unknown_result_commit_reads_original_operation_without_repeating_model(compaction_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, boundary: str) -> None:
    env, mid = compaction_api, compaction_api["memory_id"]
    original = CompactionService._seal

    async def unknown(service: CompactionService, memory_id: str, operation_id: str, **kwargs: Any) -> None:
        if boundary == "after":
            await original(service, memory_id, operation_id, **kwargs)
        raise RevisionPersistenceUnknown(operation_id)

    monkeypatch.setattr(CompactionService, "_seal", unknown)
    body = request(env)
    response = api_request(env, "POST", f"/memories/{mid}/context-compactions", json=body)
    assert response.status_code == 503 and response.json()["data"]["operation_id"] == body["operation_id"]
    replay = result(api_request(env, "POST", f"/memories/{mid}/context-compactions", json=body))
    assert replay["operation"]["status"] == ("generated" if boundary == "after" else "persistence_unknown")
    assert (replay["artifact"]["package"] is not None) is (boundary == "after")
    assert len(env["summary_calls"]) == 1
    with Session(env["engine"]) as reader:
        admission = reader.get(MemoryWriterAdmission, (mid, body["operation_id"]))
        assert admission is not None and admission.active_token is None and admission.receipt["finished_tokens"]


def test_after_model_budget_failure_retains_measurement_without_summary(compaction_api: dict[str, Any]) -> None:
    env, mid = compaction_api, compaction_api["memory_id"]
    baseline = result(api_request(env, "POST", f"/memories/{mid}/context-compactions", json=request(env, "calibrate-owned-package")))
    budget = max(1024, baseline["artifact"]["usage"]["estimated_after_tokens"] + 10)
    env["summary_response"] = "摘要保留必要条件、否定与未解决事项，不引入新事实。" * 45
    value = result(api_request(env, "POST", f"/memories/{mid}/context-compactions", json={**request(env, "after-budget"), "budget_tokens": budget}))
    assert value["operation"]["status"] == "failed" and value["operation"]["error_code"] == "compaction_output_budget_exceeded"
    assert value["artifact"]["usage"]["estimated_after_tokens"] > budget
    assert value["artifact"]["package"] is None and len(env["summary_calls"]) == 2


def test_creation_inventory_cap_rejects_before_model_and_rolls_back_admission(compaction_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    import memory.compaction.service as implementation

    env, mid = compaction_api, compaction_api["memory_id"]
    result(api_request(env, "POST", f"/memories/{mid}/context-compactions", json=request(env, "first-artifact")))
    monkeypatch.setattr(implementation, "MAX_ARTIFACTS", 1)
    response = api_request(env, "POST", f"/memories/{mid}/context-compactions", json=request(env, "over-inventory"))
    assert response.status_code == 409 and response.json()["data"]["error_code"] == "compaction_artifact_inventory_limit"
    assert len(env["summary_calls"]) == 1
    with Session(env["engine"]) as reader:
        assert reader.get(MemoryWriterAdmission, (mid, "over-inventory")) is None


@pytest.mark.parametrize("fault", ["500", "timeout"])
def test_official_openai_sdk_does_not_retry_without_provider_observer(compaction_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, fault: str) -> None:
    env, mid = compaction_api, compaction_api["memory_id"]
    with Session(env["engine"]) as writer:
        bank = writer.get(Memory, mid)
        assert bank is not None
        model = writer.get(TenantModel, bank.tenant_llm_id)
        assert model is not None
        model.model_name = "gpt-6.1-terra"
        provider = writer.get(TenantModelProvider, model.provider_id)
        instance = writer.get(TenantModelInstance, model.instance_id)
        assert provider is not None and instance is not None
        provider.provider_name = "OpenAI"
        instance.base_url, instance.api_key = "https://owned-compaction.invalid/v1", "owned-test-value"
        writer.commit()
    controlled = cast(Any, TenantLLMService.model_instance_from_config).__func__
    instances: list[Any] = []
    sdk_instances: list[AsyncOpenAI] = []
    wires: list[dict[str, Any]] = []
    sdk_initialize = AsyncOpenAI.__init__

    def initialize_sdk(client: AsyncOpenAI, *args: Any, **kwargs: Any) -> None:
        sdk_initialize(client, *args, **kwargs)
        sdk_instances.append(client)

    def factory(cls: type[TenantLLMService], config: dict[str, Any], lang: str = "Chinese", **kwargs: Any) -> Any:
        if config["llm_factory"] != "OpenAI":
            return controlled(cls, config, lang, **kwargs)
        assert kwargs["max_retries"] == 0
        instance = _REAL_MODEL_FACTORY(cls, config, lang, **kwargs)
        instances.append(instance)
        return instance

    async def send(client: httpx.AsyncClient, outgoing: httpx.Request, **kwargs: Any) -> httpx.Response:
        assert outgoing.url.host == "owned-compaction.invalid" and outgoing.url.path == "/v1/chat/completions"
        assert instances[-1].max_retries == 0
        assert sdk_instances and all(instance.max_retries == 0 for instance in sdk_instances)
        body = json.loads(outgoing.content)
        assert body["model"] == "gpt-6.1-terra"
        assert body.get("max_completion_tokens", body.get("max_tokens")) == 1024
        wires.append(body)
        if fault == "timeout":
            raise httpx.ReadTimeout("Controlled indeterminate provider response", request=outgoing)
        return httpx.Response(500, request=outgoing, json={"error": {"message": "Controlled server error", "type": "server_error"}})

    monkeypatch.setattr(TenantLLMService, "model_instance_from_config", classmethod(factory))
    monkeypatch.setattr(AsyncOpenAI, "__init__", initialize_sdk)
    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    body = request(env)
    value = result(api_request(env, "POST", f"/memories/{mid}/context-compactions", json=body))
    assert value["operation"]["status"] == "failed" and value["artifact"]["package"] is None
    assert value["artifact"]["usage"]["provider_tokens"] is None and len(wires) == 1
    result(api_request(env, "POST", f"/memories/{mid}/context-compactions", json=body))
    assert len(wires) == 1
    # LiteLLM owns its internal SDK cache; this request's bundle cannot claim
    # ownership or closure of those shared client instances.
    assert instances and sdk_instances
