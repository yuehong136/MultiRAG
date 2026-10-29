"""Real completion routes, controlled LLMs, and independently committed history."""

import json
from collections.abc import Iterator
from typing import Any
from uuid import uuid4

import pytest
import requests
import sqlalchemy as sa
from sqlalchemy.orm import Session

from api.db.db_models import APIToken
from common.constants import RetCode
from common.token_utils import num_tokens_from_string
from tests.support.rest_chat_contract import RestChatContract, sse_frames
from tests.support.rest_chat_contract import rest_chat_contract as rest_chat_contract
from tests.support.rest_chat_contract import runtime_upload_api as runtime_upload_api


@pytest.mark.parametrize("audio_output", [None, "configured", "none"])
@pytest.mark.parametrize("stream", [False, True])
def test_legacy_text_only_completion_preserves_saved_tts_and_history(rest_chat_contract: RestChatContract, monkeypatch: pytest.MonkeyPatch, audio_output: str | None, stream: bool) -> None:
    import core.llm as providers

    ctx = rest_chat_contract
    synthesis: list[str] = []

    class ControlledTTS:
        def __init__(self, key: str, model: str, **kwargs: Any) -> None:
            assert key == "controlled" and model == "tts"

        def tts(self, text: str) -> Iterator[bytes]:
            synthesis.append(text)
            # A text-only request must not wait for or be failed by this provider.
            if audio_output == "none":
                raise RuntimeError("TTS must not run for a text-only request")
            yield b"controlled-audio"

    monkeypatch.setitem(providers.TTSModel, ctx.factory, ControlledTTS)
    prompt_config = {"tts": True, "system": "Answer directly", "parameters": []}
    chat = ctx.create_chat(prompt_config=prompt_config)
    sid = ctx.request("POST", f"/chats/{chat['id']}/sessions", json={}).json()["data"]["id"]
    before = ctx.read_chat(chat["id"])
    body: dict[str, Any] = {"conversation_id": sid, "messages": [{"role": "user", "content": "Question"}], "stream": stream, "stream_output": "delta"}
    if audio_output is not None:
        body["audio_output"] = audio_output
    response = requests.post(f"{ctx.runtime['base']}/v1/conversation/completion", headers={"Authorization": f"Bearer {ctx.runtime['jwt']}"}, json=body, timeout=30)
    assert response.status_code == 200, response.text
    if stream:
        frames = sse_frames(response)
        assert frames[-1]["data"] is True
        assert all(frame["retcode"] == 0 for frame in frames), frames
        answers = [frame["data"] for frame in frames if isinstance(frame.get("data"), dict)]
    else:
        result = response.json()
        assert result["retcode"] == 0, result
        answers = [result["data"]]
    assert answers[-1]["answer"] == "Controlled answer"
    if audio_output == "none":
        assert synthesis == []
        assert all(answer.get("audio_binary") is None for answer in answers)
    else:
        assert synthesis == ["Controlled answer"]
        assert any(answer.get("audio_binary") == b"controlled-audio".hex() for answer in answers)
    # Independent sessions read back actual commits, including the original
    # application JSON; an in-memory identity-map assertion is insufficient.
    assert ctx.read_chat(chat["id"]) == before
    history = ctx.read_session(sid)
    assert history is not None
    assert history["message"][-1]["content"] == "Controlled answer"


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


@pytest.mark.parametrize("stream", [False, True])
def test_saved_empty_and_explicit_model_resolution_without_config_mutation(rest_chat_contract: RestChatContract, monkeypatch: pytest.MonkeyPatch, stream: bool) -> None:
    import core.llm as providers
    from api.db.db_models import LLM, Dialog, Tenant, TenantLLM

    ctx = rest_chat_contract
    selected: list[str] = []

    class SelectedChat:
        def __init__(self, key: str, model: str, **kwargs: Any) -> None:
            assert key == "controlled"
            selected.append(model)

        async def async_chat(self, *args: Any, **kwargs: Any) -> tuple[str, int]:
            return "selected", 0

        async def async_chat_streamly(self, *args: Any, **kwargs: Any) -> Any:
            yield "selected"
            yield 0

    monkeypatch.setitem(providers.ChatModel, ctx.factory, SelectedChat)
    # The shared fixture already owns all rows for this exact factory/tenant.
    ctx.runtime["journal"].event("reserve-chat-models", factory=ctx.factory, owner=ctx.runtime["owners"][0], names=["saved", "override"])
    with Session(ctx.runtime["engine"]) as db:
        ids = {}
        for model in ("saved", "override"):
            db.add(LLM(fid=ctx.factory, llm_name=model, mdl_type="chat", tags="CHAT", max_tokens=8192, is_tools=False))
            row = TenantLLM(tenant_id=ctx.runtime["owners"][0], llm_factory=ctx.factory, llm_name=model, mdl_type="chat", api_key="controlled", api_base="", max_tokens=8192)
            db.add(row)
            db.flush()
            ids[model] = row.id
        default = db.scalar(sa.select(TenantLLM).where(TenantLLM.tenant_id == ctx.runtime["owners"][0], TenantLLM.llm_factory == ctx.factory, TenantLLM.llm_name == "chat"))
        assert default is not None
        tenant = db.get(Tenant, ctx.runtime["owners"][0])
        assert tenant is not None
        tenant.tenant_llm_id = default.id
        db.commit()
    chat = ctx.create_chat(llm_id=f"saved@{ctx.factory}", prompt_config={"system": "Answer directly", "parameters": [], "refine_multiturn": False})
    saved = ctx.read_chat(chat["id"])
    assert saved["tenant_llm_id"] == ids["saved"]
    # Numeric tenant defaults are authoritative even when the old name is stale.
    ctx.runtime["journal"].event("reserve-tenant-default-update", owner=ctx.runtime["owners"][0], fields=["llm_id"])
    with Session(ctx.runtime["engine"]) as db:
        db.execute(sa.update(Tenant).where(Tenant.id == ctx.runtime["owners"][0]).values(llm_id="stale-default"))
        db.commit()
    for override, expected in ((None, "saved"), (f"override@{ctx.factory}", "override")):
        body: dict[str, Any] = {"chat_id": chat["id"], "question": "select", "stream": stream}
        if override:
            body["llm_id"] = override
        response = ctx.request("POST", "/chat/completions", json=body)
        if stream:
            assert sse_frames(response)[-1]["data"] is True, response.text
        else:
            assert response.json()["code"] == 0, response.text
        assert selected[-1] == expected
        assert ctx.read_chat(chat["id"]) == saved
    before = ctx.snapshot()
    calls = len(selected)
    denied = ctx.request("POST", "/chat/completions", json={"chat_id": chat["id"], "question": "deny", "llm_id": f"unknown@{ctx.factory}", "stream": stream})
    assert denied.json()["code"] != 0 and len(selected) == calls
    assert ctx.snapshot() == before
    ctx.runtime["journal"].event("reserve-chat-update", chat_id=chat["id"], fields=["llm_id", "tenant_llm_id"])
    with Session(ctx.runtime["engine"]) as db:
        db.execute(sa.update(Dialog).where(Dialog.id == chat["id"]).values(llm_id="", tenant_llm_id=None))
        db.commit()
    empty = ctx.read_chat(chat["id"])
    response = ctx.request("POST", "/chat/completions", json={"chat_id": chat["id"], "question": "default", "stream": stream})
    if stream:
        assert sse_frames(response)[-1]["data"] is True, response.text
    else:
        assert response.json()["code"] == 0, response.text
    assert selected[-1] == "chat" and ctx.read_chat(chat["id"]) == empty
    before = ctx.snapshot()
    response = ctx.request("POST", "/chat/completions", json={"question": "numeric default direct", "stream": stream})
    if stream:
        assert sse_frames(response)[-1]["data"] is True, response.text
    else:
        assert response.json()["code"] == 0, response.text
    assert selected[-1] == "chat" and ctx.snapshot() == before


@pytest.mark.parametrize("stream", [False, True])
def test_actual_openai_sdk_text_parts_rejections_and_sql_readback(rest_chat_contract: RestChatContract, stream: bool) -> None:
    from openai import OpenAI

    ctx = rest_chat_contract
    chat = ctx.create_chat(credential="api_key", prompt_config={"system": "Answer directly", "parameters": []})
    before = ctx.snapshot()
    with OpenAI(api_key=ctx.runtime["api_key"], base_url=f"{ctx.runtime['base']}/api/v1/openai/{chat['id']}", max_retries=0) as sdk:
        result = sdk.chat.completions.create(model="model", messages=[{"role": "user", "content": [{"type": "text", "text": "Hello"}, {"type": "text", "text": "World"}]}], stream=stream)
        if stream:
            chunks = list(result)
            assert chunks[-1].choices[0].finish_reason == "stop"
            assert "".join(chunk.choices[0].delta.content or "" for chunk in chunks) == "Controlled answer"
        else:
            assert result.choices[0].message.content == "Controlled answer"
    assert ctx.provider["calls"][-1]["history"][-1]["content"] == "Hello\nWorld"
    assert ctx.snapshot() == before
    count = len(ctx.provider["calls"])
    for bad in (None, 0, {}, [None], [{"type": "text", "text": 0}], [{"type": "text", "text": "keep"}, {"type": "image_url", "image_url": {"url": "http://example.invalid/image.png"}}]):
        response = ctx.request("POST", f"/openai/{chat['id']}/chat/completions", credential="api_key", json={"model": "model", "messages": [{"role": "user", "content": bad}], "stream": stream})
        assert response.json()["code"] != 0 and len(ctx.provider["calls"]) == count
        assert ctx.snapshot() == before
    ctx.provider["failure"] = True
    response = ctx.request(
        "POST",
        f"/openai/{chat['id']}/chat/completions",
        credential="api_key",
        json={"model": "model", "messages": [{"role": "user", "content": [{"type": "text", "text": "failure"}]}], "stream": stream},
    )
    if stream:
        frames = sse_frames(response)
        assert frames[-1] == "[DONE]" and "error" in frames[-2]
        assert not any(isinstance(frame, dict) and frame.get("choices", [{}])[0].get("finish_reason") == "stop" for frame in frames[:-1])
    else:
        assert response.json()["code"] != 0
    assert ctx.snapshot() == before


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("protocol", ["rest", "openai"])
def test_chat_numeric_wire_boundary_and_independent_sql_readback(rest_chat_contract: RestChatContract, monkeypatch: pytest.MonkeyPatch, stream: bool, protocol: str) -> None:
    import sys

    import numpy as np

    ctx = rest_chat_contract
    chat = ctx.create_chat(credential="api_key", prompt_config={"system": "Answer directly", "parameters": []})
    before = ctx.snapshot()
    route = sys.modules[f"api.apps.restful_apis.{'chat' if protocol == 'rest' else 'openai'}"]
    source = {
        "answer": "numeric answer",
        "reference": {
            "chunks": [
                {
                    "chunk_id": "c",
                    "document_id": "doc",
                    "similarity": np.float32("nan"),
                    "vector_similarity": float("-inf"),
                    "term_similarity": np.float16(0),
                    "positions": [[np.int64(0), np.int64(2)]],
                }
            ]
        },
        "final": True,
    }

    async def controlled_chat(*args: Any, **kwargs: Any) -> Any:
        yield source

    def reject_constant(value: str) -> Any:
        raise AssertionError(f"Invalid JSON numeric token {value}")

    # This case controls the orchestration output, not a real retrieval engine.
    # Authentication, dialog lookup, HTTP serialization and SQL readback are real.
    monkeypatch.setattr(route, "async_chat", controlled_chat)
    path = "/chat/completions" if protocol == "rest" else f"/openai/{chat['id']}/chat/completions"
    response = ctx.request("POST", path, credential="api_key", json={"model": "model", "messages": [{"role": "user", "content": "numeric"}], "reference": True, "stream": stream})
    assert response.status_code == 200
    if stream:
        frames = [json.loads(line[5:], parse_constant=reject_constant) if line[5:] != "[DONE]" else "[DONE]" for line in response.text.splitlines() if line.startswith("data:")]
        if protocol == "rest":
            assert frames[-1]["data"] is True
            references = frames[-2]["data"]["reference"]["chunks"]
        else:
            assert frames[-1] == "[DONE]" and frames[-2]["choices"][0]["finish_reason"] == "stop"
            references = frames[-2]["choices"][0]["delta"]["reference"]
    else:
        body = json.loads(response.text, parse_constant=reject_constant)
        references = body["data"]["reference"]["chunks"] if protocol == "rest" else body["choices"][0]["message"]["reference"]
    assert references[0]["similarity"] is None and references[0]["vector_similarity"] is None
    assert references[0]["term_similarity"] == 0 and references[0]["positions"] == [[0, 2]]
    assert np.isnan(source["reference"]["chunks"][0]["similarity"])
    assert ctx.snapshot() == before
