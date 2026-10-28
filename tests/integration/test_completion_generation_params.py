"""Real REST/OpenAI SDK requests reach controlled providers with saved settings intact."""

from typing import Any

import pytest

from tests.support.rest_chat_contract import RestChatContract, sse_frames
from tests.support.rest_chat_contract import rest_chat_contract as rest_chat_contract
from tests.support.rest_chat_contract import runtime_upload_api as runtime_upload_api


@pytest.mark.parametrize("stream", [False, True])
def test_rest_generation_modes_and_persisted_dialog(rest_chat_contract: RestChatContract, stream: bool) -> None:
    ctx = rest_chat_contract
    saved = {"temperature": 0.7, "top_p": 0.3, "frequency_penalty": 0.2, "presence_penalty": 0.4, "max_tokens": 64}
    chat = ctx.create_chat(llm_setting=saved, prompt_config={"system": "Answer directly", "parameters": [], "refine_multiturn": False})
    persisted = ctx.read_chat(chat["id"])
    session_id = ctx.request("POST", f"/chats/{chat['id']}/sessions", json={}).json()["data"]["id"]
    for mode in ("existing", "new", "direct"):
        for model in (None, f"chat@{ctx.factory}"):
            for extra in (
                {},
                {"temperature": None, "max_tokens": None},
                {"temperature": 0, "temperatureEnabled": False},
                {"temperature": 0, "top_p": 0, "frequency_penalty": 0, "presence_penalty": 0, "max_tokens": 0},
            ):
                body: dict[str, Any] = {"question": "generate", "stream": stream, **extra}
                if mode != "direct":
                    body["chat_id"] = chat["id"]
                if mode == "existing":
                    body["session_id"] = session_id
                if model:
                    body["llm_id"] = model
                response = ctx.request("POST", "/chat/completions", json=body)
                if stream:
                    assert sse_frames(response)[-1]["data"] is True, response.text
                else:
                    assert response.json()["code"] == 0, response.text
                ctx.runtime["journal"].event("generation-provider-witness", protocol="rest", mode=mode, model=model, stream=stream, request=extra, effective=ctx.provider["calls"][-1]["gen_conf"])
                expected = saved if mode != "direct" else {}
                if extra.get("top_p") == 0:
                    expected = {**expected, "temperature": 0, "top_p": 0, "frequency_penalty": 0, "presence_penalty": 0, "max_tokens": 0}
                assert ctx.provider["calls"][-1]["gen_conf"] == expected
                assert ctx.read_chat(chat["id"]) == persisted


@pytest.mark.parametrize("stream", [False, True])
def test_openai_sdk_generation_reaches_provider_and_keeps_sql(rest_chat_contract: RestChatContract, stream: bool) -> None:
    from openai import OpenAI

    ctx = rest_chat_contract
    saved = {"temperature": 0.7, "top_p": 0.3, "max_tokens": 64}
    chat = ctx.create_chat(credential="api_key", llm_setting=saved, prompt_config={"system": "Answer directly", "parameters": [], "refine_multiturn": False})
    before = ctx.snapshot()
    with OpenAI(api_key=ctx.runtime["api_key"], base_url=f"{ctx.runtime['base']}/api/v1/openai/{chat['id']}", max_retries=0) as sdk:
        for model in ("model", f"chat@{ctx.factory}"):
            for extra in (
                {},
                {"temperature": None, "max_tokens": None},
                {"temperature": 0, "temperature_enabled": False},
                {"temperature": 0, "top_p": 0, "frequency_penalty": 0, "presence_penalty": 0, "max_tokens": 0},
            ):
                result = sdk.chat.completions.create(model=model, messages=[{"role": "user", "content": "generate"}], stream=stream, extra_body=extra)
                if stream:
                    chunks = list(result)
                    assert chunks[-1].choices[0].finish_reason == "stop"
                    assert "".join(chunk.choices[0].delta.content or "" for chunk in chunks) == "Controlled answer"
                else:
                    assert result.choices[0].message.content == "Controlled answer"
                ctx.runtime["journal"].event("generation-provider-witness", protocol="openai-sdk", model=model, stream=stream, request=extra, effective=ctx.provider["calls"][-1]["gen_conf"])
                expected = {**saved, "temperature": 0, "top_p": 0, "frequency_penalty": 0, "presence_penalty": 0, "max_tokens": 0} if extra.get("top_p") == 0 else saved
                assert ctx.provider["calls"][-1]["gen_conf"] == expected
                assert ctx.snapshot() == before


@pytest.mark.parametrize("protocol", ["rest", "openai"])
def test_invalid_generation_has_no_provider_or_sql_side_effects(rest_chat_contract: RestChatContract, protocol: str) -> None:
    import json

    ctx = rest_chat_contract
    chat = ctx.create_chat(credential="api_key", llm_setting={"temperature": 0.7}, prompt_config={"system": "Answer directly", "parameters": [], "refine_multiturn": False})
    before = ctx.snapshot()
    body: dict[str, Any] = {"messages": [{"role": "user", "content": "reject"}], "stream": False}
    if protocol == "rest":
        path = "/chat/completions"
        body["chat_id"] = chat["id"]
    else:
        path = f"/openai/{chat['id']}/chat/completions"
        body["model"] = "model"
    for field, invalid in (("temperature", "NaN"), ("top_p", "Infinity"), ("max_tokens", "true"), ("frequency_penalty", "-3"), ("presencePenaltyEnabled", '"false"')):
        raw = json.dumps(body)[:-1] + f', "{field}": {invalid}' + "}"
        response = ctx.request("POST", path, credential="api_key", data=raw)
        assert response.status_code == 422, response.text
        assert ctx.provider["calls"] == []
        assert ctx.snapshot() == before
