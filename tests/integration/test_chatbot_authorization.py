"""Beta chatbot HTTP authorization with real SQL and independent state readback."""

import json
from collections.abc import AsyncIterator, Iterator
from contextlib import ExitStack
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from api.db.db_models import API4Conversation, APIToken, Dialog, Tenant
from api.db.services import conversation_service
from common.constants import RetCode
from tests.support.agent_update_release import release_api as release_api


@pytest.fixture
def bots(release_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    env = release_api
    owner, foreign, inactive = env["owners"]
    dialogs = {name: uuid4().hex for name in ["owned", "other", "foreign", "deleted", "missing"]}
    sessions = {name: uuid4().hex for name in ["owned", "other", "foreign", "wrong_owner", "unbound", "missing"]}
    beta = {name: uuid4().hex for name in ["owned", "foreign", "inactive", "orphan", "ambiguous"]}
    calls: list[str] = []

    async def answer(dialog: Dialog, messages: list[dict[str, Any]], db: Any, stream: bool, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        calls.append(dialog.id)
        yield {"answer": "controlled answer", "reference": {}}

    monkeypatch.setattr(conversation_service, "async_chat", answer)
    with ExitStack() as cleanup:

        def remove_rows() -> None:
            with Session(env["engine"]) as db:
                db.execute(sa.delete(API4Conversation).where(API4Conversation.dialog_id.in_(list(dialogs.values()))))
                db.execute(sa.delete(Dialog).where(Dialog.id.in_(list(dialogs.values()))))
                db.execute(sa.delete(APIToken).where(APIToken.beta.in_(list(beta.values()))))
                db.commit()
            with Session(env["engine"]) as db:
                assert db.scalar(sa.select(sa.func.count()).select_from(API4Conversation).where(API4Conversation.dialog_id.in_(list(dialogs.values())))) == 0
                assert db.scalar(sa.select(sa.func.count()).select_from(Dialog).where(Dialog.id.in_(list(dialogs.values())))) == 0
                assert db.scalar(sa.select(sa.func.count()).select_from(APIToken).where(APIToken.beta.in_(list(beta.values())))) == 0

        cleanup.callback(remove_rows)
        with Session(env["engine"]) as db:
            db.execute(sa.update(Tenant).where(Tenant.id == inactive).values(status="0"))
            for name, tenant in [("owned", owner), ("foreign", foreign), ("inactive", inactive), ("orphan", uuid4().hex), ("ambiguous", owner), ("ambiguous", foreign)]:
                db.add(APIToken(tenant_id=tenant, token=uuid4().hex, beta=beta[name], name="chatbot-test"))
            for name in ["owned", "other", "foreign", "deleted"]:
                db.add(
                    Dialog(
                        id=dialogs[name],
                        tenant_id=foreign if name == "foreign" else owner,
                        name=name,
                        icon="avatar",
                        llm_id="",
                        prompt_config={"prologue": "Hello", "tavily_api_key": "configured"},
                        status="0" if name == "deleted" else "1",
                    )
                )
            for name in ["owned", "other", "foreign", "wrong_owner", "unbound"]:
                dialog = dialogs[name] if name in {"other", "foreign"} else dialogs["owned"]
                actor = "" if name == "unbound" else foreign if name in {"foreign", "wrong_owner"} else owner
                db.add(API4Conversation(id=sessions[name], dialog_id=dialog, user_id=actor, message=[{"role": "assistant", "content": "Hello"}], reference=[]))
            db.commit()
        yield dict(env, dialogs=dialogs, sessions=sessions, beta=beta, calls=calls)


def state(env: dict[str, Any]) -> Any:
    with Session(env["engine"]) as db:
        return [
            [dict(row._mapping) for row in db.execute(sa.select(model.__table__).where(condition).order_by(order))]
            for model, condition, order in [
                (Dialog, Dialog.id.in_(list(env["dialogs"].values())), Dialog.id),
                (API4Conversation, API4Conversation.dialog_id.in_(list(env["dialogs"].values())), API4Conversation.id),
            ]
        ]


def request(env: dict[str, Any], dialog: str, payload: dict[str, Any] | None = None, *, token: str = "owned", header: str | None = None) -> Any:
    headers = {"Authorization": header if header is not None else f"Bearer {env['beta'][token]}"}
    url = env["base"] + f"/api/v1/chatbots/{env['dialogs'][dialog]}"
    if payload is None:
        return env["client"].get(url + "/info", headers=headers, timeout=30)
    return env["client"].post(url + "/completions", headers=headers, json=payload, timeout=30)


def frames(text: str) -> list[dict[str, Any]]:
    return [json.loads(part.removeprefix("data:")) for part in text.split("\n\n") if part.startswith("data:")]


def test_denied_chatbot_dialogs_and_tokens_never_create_sse_or_write(bots: dict[str, Any]) -> None:
    env = bots
    before = state(env)
    for dialog, token in [("foreign", "owned"), ("owned", "foreign"), ("missing", "owned"), ("deleted", "owned"), ("owned", "inactive"), ("owned", "orphan"), ("owned", "ambiguous")]:
        for payload in [None, {"stream": True}, {"stream": False}]:
            response = request(env, dialog, payload, token=token)
            if token in {"inactive", "orphan", "ambiguous"}:
                assert response.status_code == 401 and response.json()["retcode"] == RetCode.AUTHENTICATION_ERROR, response.text
            else:
                assert response.status_code == 200 and response.json()["code"] == RetCode.AUTHENTICATION_ERROR, response.text
            assert response.headers["Content-Type"].startswith("application/json")
            assert state(env) == before
    for header in ["", "Bearer", "Bearer invalid", f"Basic {env['beta']['owned']}", f"Bearer {env['beta']['owned']} extra"]:
        for payload in [None, {"stream": True}]:
            response = request(env, "owned", payload, header=header)
            assert response.status_code == 401 and response.json()["retcode"] == RetCode.AUTHENTICATION_ERROR
            assert state(env) == before
    assert env["calls"] == []


def test_denied_sessions_cannot_use_request_user_id_as_actor(bots: dict[str, Any]) -> None:
    env = bots
    before = state(env)
    for session in ["other", "foreign", "wrong_owner", "unbound", "missing"]:
        for stream in [True, False]:
            response = request(env, "owned", {"session_id": env["sessions"][session], "stream": stream, "question": "blocked", "user_id": env["owners"][1], "tenant_id": env["owners"][1]})
            assert response.status_code == 200 and response.json()["code"] == RetCode.AUTHENTICATION_ERROR
            assert response.headers["Content-Type"].startswith("application/json")
            assert state(env) == before
    assert env["calls"] == []


def test_owned_chatbot_info_creation_and_resume_readback(bots: dict[str, Any]) -> None:
    env = bots
    info = request(env, "owned").json()
    assert info["code"] == 0 and info["data"] == {"title": "owned", "avatar": "avatar", "prologue": "Hello", "has_tavily_key": True}
    for stream in [True, False, None]:
        response = request(env, "owned", {"stream": stream, "user_id": env["owners"][1], "tenant_id": env["owners"][1]})
        assert response.status_code == 200
        prologue = frames(response.text if stream else response.json()["data"])[0]
        session_id = prologue["data"]["session_id"]
        with Session(env["engine"]) as db:
            session = db.get(API4Conversation, session_id)
            assert session is not None and session.user_id == env["owners"][0] and session.dialog_id == env["dialogs"]["owned"]
        resumed = request(env, "owned", {"stream": stream, "session_id": session_id, "question": "continue"})
        assert resumed.status_code == 200
        answer = frames(resumed.text)[0]["data"] if stream else resumed.json()["data"]
        assert answer["answer"] == "controlled answer" and answer["session_id"] == session_id
        with Session(env["engine"]) as db:
            saved = db.get(API4Conversation, session_id)
            assert saved is not None and any(message["content"] == "continue" for message in saved.message)
            assert saved.user_id == env["owners"][0]
    assert env["calls"] == [env["dialogs"]["owned"]] * 3
