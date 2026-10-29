"""Production catalog, probe selection and credential-safe provider failures."""

import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import openai
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.db_models import get_async_db
from api.db.model_contract import ModelRegistration
from api.db.services import model_provider_service, provider_verification
from common import settings
from common.app_config import get_factory_llm_infos


def test_openai_catalog_matches_go_and_excludes_retired_or_incompatible_models(monkeypatch: pytest.MonkeyPatch) -> None:
    factory = next(item for item in get_factory_llm_infos() if item["name"] == "OpenAI")
    monkeypatch.setattr(settings, "FACTORY_LLM_INFOS", [factory])
    models = {model.model_name: model for model in model_provider_service.catalog("OpenAI")}
    excluded = {"gpt-3.5-turbo", "gpt-3.5-turbo-16k-0613", "gpt-4.1-nano", "gpt-4.5-preview", "gpt-4-32k", "o4-mini-high", "gpt-5.2-pro"}
    assert not models.keys() & excluded
    assert factory["default_probe_model"] == "gpt-6-luna"
    assert models[factory["default_probe_model"]].model_types[0] == "chat"
    assert models["gpt-6.1-sol"].max_tokens == models["gpt-5.5"].max_tokens == 1050000
    assert models["gpt-5.4-mini"].max_tokens == 400000
    assert all(not model.is_tools for name, model in models.items() if name.startswith("gpt-6"))
    assert models["gpt-transcribe"].model_types == ["speech2text"]
    assert models["gpt-transcribe"].max_tokens == models["whisper-1"].max_tokens == 0
    go = json.loads((Path(__file__).resolve().parents[2] / "configs/models/openai.json").read_text())
    assert len(go["models"]) == len(models)
    types = {"asr": "speech2text", "vision": "image2text"}
    for model in go["models"]:
        declaration = models[model["name"]]
        assert model["max_tokens"] == declaration.max_tokens
        assert [types.get(kind, kind) for kind in model["model_types"]] == declaration.model_types


@pytest.mark.parametrize(
    "selection,expected",
    [
        ({}, "gpt-6-luna"),
        ({"model_ids": ["gpt-5.4-mini"]}, "gpt-5.4-mini"),
        ({"model_ids": [], "custom_model": {"model_name": "custom", "model_types": ["chat"], "max_tokens": 0}}, "custom"),
    ],
)
def test_probe_default_does_not_override_explicit_selection(monkeypatch: pytest.MonkeyPatch, async_db: AsyncSession, client: TestClient, selection: dict[str, Any], expected: str) -> None:
    factory = next(item for item in get_factory_llm_infos() if item["name"] == "OpenAI")
    monkeypatch.setattr(settings, "FACTORY_LLM_INFOS", [factory])
    monkeypatch.setattr(async_db, "get", AsyncMock(return_value=SimpleNamespace(status="1")))
    monkeypatch.setattr(async_db, "scalar", AsyncMock(return_value=None))
    rollback = AsyncMock()
    monkeypatch.setattr(async_db, "rollback", rollback)
    verify = AsyncMock(return_value={"verified": True})
    monkeypatch.setattr(provider_verification, "verify_model", verify)
    client.app.dependency_overrides[get_async_db] = lambda: async_db
    response = client.post("/api/v1/providers/OpenAI/connection", json={"api_key": "fixture", **selection})
    assert response.status_code == 200 and response.json()["data"] == {"verified": True}
    assert verify.await_args.args[3].model_name == expected
    rollback.assert_awaited_once()


@pytest.mark.parametrize("returned_error", [False, True])
@pytest.mark.parametrize(
    "status,code,reason",
    [
        (400, None, "request parameters"),
        (401, None, "API key"),
        (403, None, "permissions"),
        (404, "model_not_found", "model or provider endpoint"),
        (429, "insufficient_quota", "quota or billing"),
        (429, None, "rate limit"),
        (503, None, "service error"),
    ],
)
async def test_probe_reports_safe_upstream_reason_and_closes(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, returned_error: bool, status: int, code: str | None, reason: str
) -> None:
    import core.llm

    secret = "private-fixture-credential"
    error = openai.APIStatusError(secret, response=httpx.Response(status, request=httpx.Request("POST", f"https://fixture.invalid/{secret}")), body={"code": code, "message": secret})
    closed: list[bool] = []

    class Adapter:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def _exceptions_async(self, error: BaseException, attempt: int) -> str:
            raise AssertionError("probe must replace the adapter's diagnostic logger")

        async def async_chat(self, *args: Any) -> tuple[str, int]:
            if returned_error:
                return await self._exceptions_async(error, 0), 0
            raise error

        async def aclose(self) -> None:
            closed.append(True)

    monkeypatch.setattr(core.llm, "ChatModel", {"OpenAI": Adapter})
    with pytest.raises(ValueError, match=reason) as raised:
        await provider_verification.verify_model("OpenAI", secret, "", ModelRegistration(model_name="fixture", model_types=["chat"], max_tokens=0))
    assert str(status) in str(raised.value)
    assert secret not in str(raised.value) + caplog.text
    assert "fixture.invalid" not in str(raised.value) + caplog.text
    assert closed == [True]


@pytest.mark.parametrize(
    "error,reason", [(httpx.ConnectError("private"), "could not connect"), (httpx.ReadTimeout("private"), "timed out"), (ValueError("private"), "Provider connection verification failed")]
)
def test_unknown_or_network_probe_failure_does_not_echo_exception(error: Exception, reason: str) -> None:
    message = provider_verification._failure_message(error)
    assert reason in message and "private" not in message


@pytest.mark.parametrize("model_name", ["gpt-6.1-sol", "gpt-6-luna"])
async def test_gpt6_chat_does_not_send_unsupported_sampling_parameters(monkeypatch: pytest.MonkeyPatch, model_name: str) -> None:
    from core.llm.chat import LiteLLMBase

    model = LiteLLMBase("fixture", model_name, provider="OpenAI", max_retries=0)
    seen: list[dict[str, Any]] = []

    @asynccontextmanager
    async def completion(options: dict[str, Any]) -> Any:
        seen.append(options)
        yield SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="OK"), finish_reason="stop")], usage=SimpleNamespace(total_tokens=0))

    monkeypatch.setattr(model, "_completion", completion)
    config = {"temperature": 0.3, "top_p": 0.8, "logprobs": True, "top_logprobs": 2, "reasoning_effort": "low"}
    answer, _usage = await model.async_chat("", [{"role": "user", "content": "Reply OK"}], config, temperature=0.7)
    assert answer == "OK"
    assert len(seen) == 1 and seen[0]["model"] == f"openai/{model_name}"
    assert not {"temperature", "top_p", "logprobs", "top_logprobs"} & seen[0].keys()
    assert seen[0]["reasoning_effort"] == "low"
    assert config["temperature"] == 0.3 and config["top_logprobs"] == 2
