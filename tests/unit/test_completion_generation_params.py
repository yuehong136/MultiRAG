"""Generation overrides at both registered completion HTTP boundaries."""

from collections.abc import AsyncIterator
from copy import deepcopy
from types import SimpleNamespace
from typing import Any

import pytest

from api.db.db_models import Dialog
from tests.unit.test_openai_compatible_api import openai_route as openai_route
from tests.unit.test_rest_chat_history import history_route as history_route


@pytest.fixture(params=["rest", "openai"])
def completion_route(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, client: Any) -> dict[str, Any]:
    # Declare the shared client before dynamic fixtures so override snapshots
    # cannot erase its session baseline after the first parametrized case.
    protocol = request.param
    calls: list[dict[str, Any]] = []
    saved = {"temperature": 0.7, "top_p": 0.3, "max_tokens": 64, "custom": {"keep": [1]}}
    dialog = Dialog(id="chat", tenant_id="tenant-unit", llm_id="saved-model", tenant_llm_id=11, llm_setting=deepcopy(saved), prompt_config={}, kb_ids=[])
    if protocol == "rest":
        client, route, state = request.getfixturevalue("history_route")
        monkeypatch.setattr(route.DialogService, "get_by_id", lambda *_: dialog)
        monkeypatch.setattr(route, "resolve_model_type", lambda *_: {"chat"})
        monkeypatch.setattr(route, "get_model_config_by_type_and_name", lambda *_: {"id": 22})
        monkeypatch.setattr(route, "_build_default_completion_dialog", lambda *_: Dialog(tenant_id="tenant-unit", llm_id="default", tenant_llm_id=33, llm_setting={}, prompt_config={}, kb_ids=[]))
        url = "/api/v1/chat/completions"
        body = {"chat_id": "chat", "session_id": "session", "question": "hi"}
    else:
        client, route, _, _ = request.getfixturevalue("openai_route")
        monkeypatch.setattr(route.DialogService, "query", classmethod(lambda *_a, **_kw: [dialog]))
        monkeypatch.setattr(route.TenantLLMService, "get_api_key", classmethod(lambda *_: SimpleNamespace(id=22)))
        url = "/api/v1/openai/chat/chat/completions"
        body = {"model": "model", "messages": [{"role": "user", "content": "hi"}]}
        state = {}

    async def provider(dia: Any, messages: Any, db: Any, stream: bool, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        assert isinstance(dia, Dialog)
        assert dia is not dialog
        calls.append({"config": deepcopy(dia.llm_setting), "model": dia.llm_id, "model_id": dia.tenant_llm_id, "kwargs": deepcopy(kwargs)})
        if "custom" in dia.llm_setting:
            dia.llm_setting["custom"]["keep"].append("provider-edit")
        dia.llm_setting["max_tokens"] = 1
        yield {"answer": "ok", "reference": {}, "final": True}

    monkeypatch.setattr(route, "async_chat", provider)
    return {"client": client, "url": url, "body": body, "calls": calls, "saved": saved, "dialog": dialog, "protocol": protocol, "state": state}


def _send(ctx: dict[str, Any], stream: bool, **extra: Any) -> Any:
    response = ctx["client"].post(ctx["url"], json={**ctx["body"], "stream": stream, **extra})
    assert response.status_code == 200, response.text
    if stream:
        assert "ok" in response.text
        assert response.text.endswith("data:[DONE]\n\n") if ctx["protocol"] == "openai" else '"data": true' in response.text
    else:
        assert response.json().get("code", 0) == 0, response.text
    assert ctx["dialog"].llm_setting == ctx["saved"]
    assert ctx["dialog"].llm_id == "saved-model" and ctx["dialog"].tenant_llm_id == 11
    return response


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("override_model", [False, True])
def test_zero_generation_merge_and_model_identity(completion_route: dict[str, Any], stream: bool, override_model: bool) -> None:
    ctx = completion_route
    extra = {"temperature": 0, "top_p": 0, "frequency_penalty": 0, "presence_penalty": 0, "max_tokens": 0}
    if override_model:
        extra["llm_id" if ctx["protocol"] == "rest" else "model"] = "override"
    _send(ctx, stream, **extra)
    call = ctx["calls"][-1]
    assert call["config"] == {**ctx["saved"], "temperature": 0, "top_p": 0, "frequency_penalty": 0, "presence_penalty": 0, "max_tokens": 0}
    assert (call["model"], call["model_id"]) == (("override", 22) if override_model else ("saved-model", 11))
    assert not set(extra).intersection(call["kwargs"])


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(
    "extra",
    [
        {},
        {"temperature": None, "max_tokens": None},
        {"temperature": 0, "temperatureEnabled": False},
        {"temperature": 0, "temperature_enabled": False},
        {"temperature": 0, "temperatureEnabled": True, "temperature_enabled": False},
        {"temperature": 0, "temperatureEnabled": False, "temperature_enabled": True},
    ],
)
def test_inherited_defaults_and_disabled_overrides(completion_route: dict[str, Any], stream: bool, extra: dict[str, Any]) -> None:
    ctx = completion_route
    _send(ctx, stream, **extra)
    assert ctx["calls"][-1]["config"] == ctx["saved"]
    assert not set(extra).intersection(ctx["calls"][-1]["kwargs"])


@pytest.mark.parametrize("stream", [False, True])
def test_enabled_bounds_and_negative_penalties(completion_route: dict[str, Any], stream: bool) -> None:
    ctx = completion_route
    _send(ctx, stream, temperature=2, temperature_enabled=True, top_p=1, frequency_penalty=-2, presence_penalty=2, max_tokens=1)
    assert ctx["calls"][-1]["config"] == {**ctx["saved"], "temperature": 2, "top_p": 1, "frequency_penalty": -2, "presence_penalty": 2, "max_tokens": 1}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("temperature", True),
        ("temperature", "0"),
        ("temperature", -0.1),
        ("temperature", 2.1),
        ("top_p", 1.1),
        ("top_p", -0.1),
        ("frequency_penalty", -2.1),
        ("presence_penalty", 2.1),
        ("max_tokens", True),
        ("max_tokens", 1.1),
        ("max_tokens", -1),
        ("temperatureEnabled", 1),
        ("temperature_enabled", "false"),
    ],
)
def test_invalid_generation_rejected_before_execution(completion_route: dict[str, Any], field: str, value: Any) -> None:
    ctx = completion_route
    response = ctx["client"].post(ctx["url"], json={**ctx["body"], field: value})
    assert response.status_code == 422
    assert not ctx["calls"]
    assert not ctx["state"].get("writes") and not ctx["state"].get("saved")


@pytest.mark.parametrize("field", ["temperature", "top_p", "frequency_penalty", "presence_penalty", "max_tokens"])
@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_nonfinite_generation_is_rejected(completion_route: dict[str, Any], field: str, value: str) -> None:
    import json

    ctx = completion_route
    raw = json.dumps(ctx["body"])[:-1] + f', "{field}": {value}' + "}"
    response = ctx["client"].post(ctx["url"], content=raw, headers={"Content-Type": "application/json"})
    assert response.status_code == 422
    assert not ctx["calls"]


@pytest.mark.parametrize(
    ("field", "camel"),
    [
        ("temperature", "temperatureEnabled"),
        ("top_p", "topPEnabled"),
        ("frequency_penalty", "frequencyPenaltyEnabled"),
        ("presence_penalty", "presencePenaltyEnabled"),
        ("max_tokens", "maxTokensEnabled"),
    ],
)
@pytest.mark.parametrize("camel_disabled", [False, True])
def test_all_enable_flags_resolve_conflicting_aliases(completion_route: dict[str, Any], field: str, camel: str, camel_disabled: bool) -> None:
    ctx = completion_route
    _send(ctx, False, **{field: 0, camel: not camel_disabled, field + "_enabled": camel_disabled})
    assert ctx["calls"][-1]["config"] == ctx["saved"]
    assert camel not in ctx["calls"][-1]["kwargs"]
    assert field + "_enabled" not in ctx["calls"][-1]["kwargs"]
