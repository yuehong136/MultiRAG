"""Real completion routes, controlled LLMs, and independently committed history."""

import json
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from api.db.db_models import APIToken
from common.constants import RetCode
from common.token_utils import num_tokens_from_string
from tests.support.rest_chat_contract import RestChatContract, sse_frames
from tests.support.rest_chat_contract import rest_chat_contract as rest_chat_contract
from tests.support.rest_chat_contract import runtime_upload_api as runtime_upload_api


@pytest.mark.parametrize("credential", ["jwt", "api_key"])
@pytest.mark.parametrize("stream", [False, True])
def test_three_completion_modes_and_committed_history(rest_chat_contract: RestChatContract, credential: str, stream: bool) -> None:
    ctx = rest_chat_contract
    chat = ctx.create_chat(credential=credential, prompt_config={"system": "Answer directly", "parameters": []})
    session = ctx.request("POST", f"/chats/{chat['id']}/sessions", credential=credential, json={}).json()["data"]
    for mode in ("session", "auto", "direct"):
        before = ctx.snapshot()
        message_id = uuid4().hex
        body: dict[str, Any] = {"messages": [{"id": message_id, "role": "user", "content": f"Question {mode}"}], "stream": stream, "user_id": "forged-session-owner"}
        if mode != "direct":
            body["chat_id"] = chat["id"]
        if mode == "session":
            body["session_id"] = session["id"]
        with ctx.request("POST", "/chat/completions", credential=credential, json=body, stream=True) as response:
            assert response.status_code == 200
            if stream:
                assert response.headers["content-type"].startswith("text/event-stream")
                frames = [json.loads(line[5:]) for line in response.iter_lines(decode_unicode=True) if line.startswith("data:")]
                assert frames[-1]["data"] is True and all(frame["code"] == 0 for frame in frames), frames
                answer = frames[-2]["data"]
            else:
                result = response.json()
                assert result["code"] == 0, result
                answer = result["data"]
            assert answer["answer"] == "Controlled answer"
            assert {"answer", "reference", "audio_binary", "id", "session_id"}.issubset(answer)
            assert answer["id"] == message_id
            if mode == "direct":
                assert not answer["session_id"] and ctx.snapshot() == before
            else:
                assert answer["chat_id"] == chat["id"] and answer["session_id"]
                if mode == "session":
                    assert answer["session_id"] == session["id"]
                persisted = ctx.read_session(answer["session_id"])
                assert persisted is not None and persisted["dialog_id"] == chat["id"]
                assert persisted["user_id"] == ctx.runtime["owners"][credential == "api_key"]
                assert persisted["user_id"] != body["user_id"]
                assert persisted["message"][-2]["id"] == message_id
                assert persisted["message"][-1]["content"] == answer["answer"]
                history = ctx.request("GET", f"/chats/{chat['id']}/sessions/{answer['session_id']}", credential=credential).json()
                assert history["code"] == 0 and history["data"]["messages"] == persisted["message"]
    assert len(ctx.provider["calls"]) == 3
    assert all(call["history"][-1]["content"].startswith("Question ") for call in ctx.provider["calls"])


@pytest.mark.parametrize("credential", ["jwt", "api_key"])
@pytest.mark.parametrize("stream", [False, True])
def test_completion_rejections_never_invoke_provider_or_write(rest_chat_contract: RestChatContract, credential: str, stream: bool) -> None:
    ctx = rest_chat_contract
    chat = ctx.create_chat(credential=credential)
    foreign = ctx.create_chat(credential="jwt" if credential == "api_key" else "api_key")
    other_session = ctx.request("POST", f"/chats/{foreign['id']}/sessions", credential="jwt" if credential == "api_key" else "api_key", json={}).json()["data"]["id"]
    before = ctx.snapshot()
    for extra, expected in (
        ({"session_id": "missing"}, RetCode.DATA_ERROR),
        ({"chat_id": chat["id"], "session_id": "missing"}, RetCode.DATA_ERROR),
        ({"chat_id": foreign["id"]}, RetCode.AUTHENTICATION_ERROR),
        ({"chat_id": chat["id"], "session_id": other_session}, RetCode.DATA_ERROR),
    ):
        response = ctx.request(
            "POST", "/chat/completions", credential=credential, json={"messages": [{"role": "user", "content": "denied"}], "stream": stream, "user_id": "forged-session-owner", **extra}
        )
        assert response.status_code == 200 and response.json()["code"] == expected, response.text
        assert ctx.snapshot() == before and ctx.provider["calls"] == []
    assert ctx.request("POST", "/chat/completions", credential=None, json={"messages": [{"role": "user", "content": "denied"}]}).status_code == 401
    assert ctx.snapshot() == before and ctx.provider["calls"] == []


@pytest.mark.parametrize("stream", [False, True])
def test_openai_usage_shape_including_legal_zero(rest_chat_contract: RestChatContract, stream: bool) -> None:
    ctx = rest_chat_contract
    chat = ctx.create_chat(credential="api_key", prompt_config={"system": "Answer directly", "parameters": []})
    before = ctx.snapshot()
    for prompt, answer in (("Question", "Controlled answer"), ("", "Controlled answer" if stream else "")):
        ctx.provider["answer"] = answer
        response = ctx.request("POST", f"/openai/{chat['id']}/chat/completions", credential="api_key", json={"model": "model", "messages": [{"role": "user", "content": prompt}], "stream": stream})
        assert response.status_code == 200
        if stream:
            frames = sse_frames(response)
            assert frames[-1] == "[DONE]" and not any(isinstance(frame, dict) and "error" in frame for frame in frames), frames
            final = frames[-2]
            assert final["choices"][0]["finish_reason"] == "stop"
            usage = final["usage"]
        else:
            body = response.json()
            assert body["object"] == "chat.completion" and body["choices"][0]["message"]["content"] == answer, body
            usage = body["usage"]
        assert usage["prompt_tokens"] == num_tokens_from_string(prompt)
        assert usage["completion_tokens"] == num_tokens_from_string(answer)
        assert usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"]
        assert all(type(usage[key]) is int and usage[key] >= 0 for key in ("prompt_tokens", "completion_tokens", "total_tokens"))
        assert ctx.snapshot() == before


@pytest.mark.parametrize("stream", [False, True])
def test_provider_failure_is_an_error_without_saved_assistant(rest_chat_contract: RestChatContract, stream: bool) -> None:
    ctx = rest_chat_contract
    chat = ctx.create_chat()
    sid = ctx.request("POST", f"/chats/{chat['id']}/sessions", json={}).json()["data"]["id"]
    before = ctx.read_session(sid)
    ctx.provider["failure"] = True
    response = ctx.request("POST", "/chat/completions", json={"chat_id": chat["id"], "session_id": sid, "messages": [{"role": "user", "content": "failure"}], "stream": stream})
    assert response.status_code == 200
    if stream:
        frames = sse_frames(response)
        assert frames[-1]["code"] != 0 and not any(frame.get("data") is True for frame in frames), frames
    else:
        assert response.json()["code"] != 0
    assert ctx.read_session(sid) == before


def test_related_questions_uses_real_beta_auth_and_controlled_model(rest_chat_contract: RestChatContract) -> None:
    ctx = rest_chat_contract
    beta = uuid4().hex
    ctx.runtime["beta"] = beta
    ctx.runtime["record"]["rest_chat"]["beta_token_owner"] = ctx.runtime["owners"][1]
    ctx.runtime["record_path"].write_text(json.dumps(ctx.runtime["record"]))
    with Session(ctx.runtime["engine"]) as db:
        db.execute(sa.update(APIToken).where(APIToken.token == ctx.runtime["api_key"]).values(beta=beta))
        db.commit()
    ctx.provider["answer"] = "1. related one\n2. related two"
    before = ctx.snapshot()
    response = ctx.request("POST", "/searchbots/related_questions", credential="beta", json={"question": "contract", "industry": "search"})
    assert response.status_code == 200 and response.json()["retcode"] == 0, response.text
    assert response.json()["data"] == ["related one", "related two"]
    assert len(ctx.provider["calls"]) == 1 and ctx.snapshot() == before
    response = ctx.request("POST", "/searchbots/related_questions", credential="beta", json={"industry": "search"})
    assert response.status_code == 422 and len(ctx.provider["calls"]) == 1
    ctx.runtime["invalid_beta"] = "invalid-fixture-beta"
    for credential in (None, "invalid_beta"):
        response = ctx.request("POST", "/searchbots/related_questions", credential=credential, json={"question": "contract"})
        assert response.status_code == 401 and len(ctx.provider["calls"]) == 1
        assert ctx.snapshot() == before
