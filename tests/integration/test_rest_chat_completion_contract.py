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


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("full", [False, True])
def test_latest_rest_history_and_explicit_branch_are_committed(rest_chat_contract: RestChatContract, stream: bool, full: bool) -> None:
    ctx = rest_chat_contract
    chat = ctx.create_chat(prompt_config={"system": "Answer directly", "parameters": []})
    first = ctx.request("POST", "/chat/completions", json={"chat_id": chat["id"], "question": "server question", "stream": False}).json()
    assert first["code"] == 0, first
    sid = first["data"]["session_id"]
    before = ctx.read_session(sid)
    submitted = [
        {"role": "user", "content": "submitted branch question", "id": "branch"},
        {"role": "assistant", "content": "submitted branch answer", "id": "branch"},
        {"role": "user", "content": "latest question", "id": "latest"},
    ]
    response = ctx.request("POST", "/chat/completions", json={"chat_id": chat["id"], "conversation_id": sid, "messages": submitted, "stream": stream, "pass_all_history_messages": full})
    assert response.status_code == 200
    if stream:
        frames = sse_frames(response)
        assert frames[-1]["data"] is True and all(frame["code"] == 0 for frame in frames), frames
    else:
        assert response.json()["code"] == 0, response.text
    captured = str(ctx.provider["calls"][-1]["history"])
    assert ("submitted branch question" in captured) is full
    assert ("server question" in captured) is not full
    saved = ctx.read_session(sid)
    assert saved is not None and before is not None
    assert saved["message"][-2] == submitted[-1]
    assert saved["message"][-1]["role"] == "assistant"
    assert ("server question" in str(saved["message"])) is not full
    assert ctx.request("GET", f"/chats/{chat['id']}/sessions/{sid}").json()["data"]["messages"] == saved["message"]


@pytest.mark.parametrize("stream", [False, True])
def test_openai_session_hints_leave_existing_session_unchanged(rest_chat_contract: RestChatContract, stream: bool) -> None:
    ctx = rest_chat_contract
    chat = ctx.create_chat(credential="api_key", prompt_config={"system": "Answer directly", "parameters": []})
    sid = ctx.request("POST", f"/chats/{chat['id']}/sessions", credential="api_key", json={}).json()["data"]["id"]
    before = ctx.snapshot()
    for nested in (False, True):
        body: dict[str, Any] = {"model": "model", "messages": [{"role": "user", "content": "do not save"}], "stream": stream}
        body.update({"extra_body": {"session_id": sid}} if nested else {"session_id": sid})
        response = ctx.request("POST", f"/openai/{chat['id']}/chat/completions", credential="api_key", json=body)
        assert response.status_code == 200 and '"session_id"' not in response.text
        if stream:
            frames = sse_frames(response)
            assert frames[-1] == "[DONE]" and frames[-2]["choices"][0]["finish_reason"] == "stop", frames
        else:
            assert response.json()["object"] == "chat.completion", response.text
        assert ctx.snapshot() == before
    assert len(ctx.provider["calls"]) == 2


def test_invalid_rest_contract_has_no_committed_or_provider_side_effects(rest_chat_contract: RestChatContract) -> None:
    ctx = rest_chat_contract
    chat = ctx.create_chat()
    sid = ctx.request("POST", f"/chats/{chat['id']}/sessions", json={}).json()["data"]["id"]
    before = ctx.snapshot()
    for invalid in (
        {"pass_all_history_messages": "false"},
        {"session_id": sid, "conversation_id": "conflicting"},
        {"messages": [{"role": "user", "content": 1}]},
        {"files": ["invalid"]},
        {"files": None},
    ):
        response = ctx.request("POST", "/chat/completions", json={"chat_id": chat["id"], "session_id": sid, "question": "deny", "stream": False, **invalid})
        assert response.status_code == 422, response.text
        assert ctx.snapshot() == before and ctx.provider["calls"] == []
    missing = ctx.request(
        "POST", "/chat/completions", json={"chat_id": chat["id"], "session_id": sid, "question": "deny", "stream": False, "files": [{"id": uuid4().hex, "created_by": ctx.runtime["owners"][1]}]}
    )
    assert missing.status_code == 200 and missing.json()["code"] != 0, missing.text
    assert ctx.snapshot() == before and ctx.provider["calls"] == []


def test_actual_admin_client_appends_two_latest_messages(rest_chat_contract: RestChatContract, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    import importlib
    from pathlib import Path
    from urllib.parse import urlsplit

    ctx = rest_chat_contract
    chat = ctx.create_chat(prompt_config={"system": "Answer directly", "parameters": []})
    sid = ctx.request("POST", f"/chats/{chat['id']}/sessions", json={}).json()["data"]["id"]
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "admin/client"))
    admin, http = importlib.import_module("multirag_client"), importlib.import_module("http_client")
    address = urlsplit(ctx.runtime["base"])
    transport = http.HttpClient(host=address.hostname, port=address.port)
    # CLI web auth stores the complete Authorization header returned by login.
    transport.login_token = f"Bearer {ctx.runtime['jwt']}"
    client = admin.MultiRAGClient(transport, "user")
    for question in ("CLI first", "CLI second"):
        client.chat_on_session({"chat_name": chat["name"], "session_id": sid, "message": question})
        assert "Controlled answer" in capsys.readouterr().out
    saved = ctx.read_session(sid)
    assert saved is not None
    assert [message["content"] for message in saved["message"] if message["role"] == "user"] == ["CLI first", "CLI second"]


def test_question_attachment_uses_uploaded_authority_and_committed_descriptor(rest_chat_contract: RestChatContract) -> None:
    ctx = rest_chat_contract
    chat = ctx.create_chat(prompt_config={"system": "Answer directly", "parameters": []})
    uploaded = ctx.request("POST", "/documents/upload", files={"file": ("authority.txt", b"owned attachment content", "text/plain")}).json()
    assert uploaded["code"] == 0, uploaded
    descriptor = uploaded["data"]
    forged = {**descriptor, "created_by": "foreign-user", "name": "forged.txt", "mime_type": "image/png"}
    response = ctx.request("POST", "/chat/completions", json={"chat_id": chat["id"], "question": "read attached", "files": [forged], "stream": False})
    assert response.status_code == 200 and response.json()["code"] == 0, response.text
    saved = ctx.read_session(response.json()["data"]["session_id"])
    assert saved is not None and saved["message"][-2]["files"] == [descriptor]
    assert "owned attachment content" in ctx.provider["calls"][-1]["system"]
    before, count = ctx.snapshot(), len(ctx.provider["calls"])
    denied = ctx.request("POST", "/chat/completions", credential="api_key", json={"question": "read foreign", "files": [descriptor], "stream": False})
    assert denied.status_code == 200 and denied.json()["code"] != 0, denied.text
    assert ctx.snapshot() == before and len(ctx.provider["calls"]) == count
