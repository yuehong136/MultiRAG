"""Registered deletion APIs with scratch SQL and independently read MinIO objects."""

import asyncio
import json
import os
import subprocess
import sys
import threading
from collections.abc import AsyncIterator, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import requests
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import Session

from api.db.db_models import API4Conversation, Conversation, Dialog, UserTenant
from api.db.services.conversation_service import ConversationService
from common.constants import RetCode
from tests.support.runtime_upload import read_object
from tests.support.runtime_upload import runtime_upload_api as runtime_upload_api
from tests.support.web_consumer import tsx_runner


@pytest.fixture
def session_upload_api(runtime_upload_api: dict[str, Any]) -> Iterator[dict[str, Any]]:
    env = runtime_upload_api
    env["chats"] = [uuid4().hex for _ in range(3)]
    env["session_ids"] = []
    env["api4_ids"] = []
    with Session(env["engine"]) as db:
        for chat_id, owner in zip(env["chats"], [*env["owners"], env["owners"][0]], strict=True):
            db.add(Dialog(id=chat_id, tenant_id=owner, name="Session cleanup scratch", llm_id="controlled"))
        db.commit()
    try:
        yield env
    finally:
        with Session(env["engine"]) as db:
            db.execute(sa.delete(Conversation).where(Conversation.dialog_id.in_(env["chats"])))
            db.execute(sa.delete(API4Conversation).where(API4Conversation.id.in_(env["api4_ids"])))
            db.execute(sa.delete(Dialog).where(Dialog.id.in_(env["chats"])))
            db.commit()
        with env["engine"].connect() as db:
            assert db.scalar(sa.select(sa.func.count()).select_from(Conversation).where(Conversation.dialog_id.in_(env["chats"]))) == 0
            assert db.scalar(sa.select(sa.func.count()).select_from(API4Conversation).where(API4Conversation.id.in_(env["api4_ids"]))) == 0
            assert db.scalar(sa.select(sa.func.count()).select_from(Dialog).where(Dialog.id.in_(env["chats"]))) == 0
        env["record"]["session_rows_removed"] = True


def upload(env: dict[str, Any], credential: str = "jwt") -> dict[str, Any]:
    response = requests.post(
        env["base"] + "/api/v1/documents/upload",
        headers={"Authorization": f"Bearer {env[credential]}"},
        files={"file": ("owned.txt", b"session upload bytes", "text/plain")},
        timeout=30,
    )
    assert response.status_code == 200 and response.json()["code"] == 0, response.text
    descriptor = response.json()["data"]
    key = f"{descriptor['created_by']}-downloads/{descriptor['id']}"
    assert read_object(env["storage"], env["bucket"], key) == b"session upload bytes"
    assert json.loads(read_object(env["storage"], env["bucket"], key + ".upload.json")) == descriptor
    return descriptor


def seed_session(env: dict[str, Any], files: list[Any], *, chat: int = 0) -> str:
    session_id = uuid4().hex
    env["session_ids"].append(session_id)
    with Session(env["engine"]) as db:
        db.add(Conversation(id=session_id, dialog_id=env["chats"][chat], user_id=env["owners"][1 if chat == 1 else 0], message=[{"role": "user", "content": "Persisted upload", "files": files}]))
        db.commit()
    return session_id


def snapshot(env: dict[str, Any]) -> tuple[dict[str, Any], dict[str, bytes]]:
    with Session(env["engine"]) as db:
        rows = {item.id: item.to_dict() for item in db.scalars(sa.select(Conversation).where(Conversation.dialog_id.in_(env["chats"])))}
    objects = {item.object_name: read_object(env["storage"], env["bucket"], item.object_name) for item in env["storage"].list_objects(env["bucket"], recursive=True)}
    return rows, objects


def delete_sessions(env: dict[str, Any], ids: list[str], *, facade: bool = False, credential: str = "jwt", chat: int = 0) -> requests.Response:
    headers = {"Authorization": f"Bearer {env[credential]}"}
    if facade:
        return requests.post(env["base"] + "/v1/conversation/rm", headers=headers, json={"conversation_ids": ids}, timeout=30)
    return requests.delete(env["base"] + f"/api/v1/chats/{env['chats'][chat]}/sessions", headers=headers, json={"ids": ids}, timeout=30)


@pytest.mark.parametrize("credential", ["jwt", "api_key"])
@pytest.mark.parametrize("facade", [False, True])
def test_upload_completion_and_delete_real_routes(session_upload_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, credential: str, facade: bool) -> None:
    env = session_upload_api
    descriptor = upload(env, credential)
    chat = 0 if credential == "jwt" else 1
    headers = {"Authorization": f"Bearer {env[credential]}"}
    response = requests.post(env["base"] + f"/api/v1/chats/{env['chats'][chat]}/sessions", headers=headers, json={"name": "Upload lifecycle"}, timeout=30)
    assert response.status_code == 200 and response.json()["code"] == 0, response.text
    session_id = response.json()["data"]["id"]
    env["session_ids"].append(session_id)

    async def controlled_model(*args: Any, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        yield {"answer": "captured", "reference": {}, "final": True}

    monkeypatch.setattr(sys.modules["api.apps.restful_apis.chat"], "async_chat", controlled_model)
    response = requests.post(
        env["base"] + "/api/v1/chat/completions",
        headers=headers,
        json={"chat_id": env["chats"][chat], "session_id": session_id, "messages": [{"role": "user", "content": "Read this", "files": [descriptor, descriptor]}], "stream": False},
        timeout=30,
    )
    assert response.status_code == 200 and response.json()["code"] == 0, response.text
    assert snapshot(env)[0][session_id]["message"][0]["files"] == [descriptor, descriptor]
    calls: list[tuple[str, str]] = []
    original_rm = env["storage_adapter"].rm

    def observed_rm(namespace: str, key: str) -> Any:
        calls.append((namespace, key))
        return original_rm(namespace, key)

    monkeypatch.setattr(env["storage_adapter"], "rm", observed_rm)
    response = delete_sessions(env, [session_id, session_id], facade=facade, credential=credential, chat=chat)
    assert response.status_code == 200 and response.json()["retcode" if facade else "code"] == 0, response.text
    assert session_id not in snapshot(env)[0] and snapshot(env)[1] == {}
    namespace = f"{descriptor['created_by']}-downloads"
    assert calls == [(namespace, descriptor["id"]), (namespace, descriptor["id"] + ".upload.json")]
    before = snapshot(env)
    response = delete_sessions(env, [session_id], facade=facade, credential=credential, chat=chat)
    assert response.json()["retcode" if facade else "code"] != 0
    assert snapshot(env) == before and len(calls) == 2
    env["record"]["session_route_readback"] = {"credential": credential, "facade": facade, "session_deleted": True, "objects_deleted": True, "duplicates_removed_once": True}


@pytest.mark.parametrize("reference", ["same_chat", "other_chat", "api4_message", "api4_input"])
def test_other_session_references_are_preserved(session_upload_api: dict[str, Any], reference: str) -> None:
    env = session_upload_api
    descriptor = upload(env)
    session_id = seed_session(env, [descriptor])
    if reference in {"same_chat", "other_chat"}:
        other_id = seed_session(env, [descriptor], chat=0 if reference == "same_chat" else 2)
    else:
        other_id = uuid4().hex
        env["api4_ids"].append(other_id)
        message = [{"files": [descriptor]}] if reference == "api4_message" else []
        dsl = {"globals": {"runtime": {"value": [{"file_id": descriptor["id"]}]}}} if reference == "api4_input" else {}
        with Session(env["engine"]) as db:
            db.add(API4Conversation(id=other_id, dialog_id=uuid4().hex, user_id=env["owners"][0], message=message, dsl=dsl, source="agent"))
            db.commit()
    objects_before = snapshot(env)[1]
    response = delete_sessions(env, [session_id])
    assert response.json()["code"] == 0, response.text
    assert session_id not in snapshot(env)[0] and snapshot(env)[1] == objects_before
    if reference in {"same_chat", "other_chat"}:
        response = delete_sessions(env, [other_id], chat=0 if reference == "same_chat" else 2)
    else:
        with Session(env["engine"]) as db:
            db.execute(sa.delete(API4Conversation).where(API4Conversation.id == other_id))
            db.commit()
        response = delete_sessions(env, [seed_session(env, [descriptor])])
    assert response.json()["code"] == 0 and snapshot(env)[1] == {}, response.text


@pytest.mark.parametrize("facade", [False, True])
def test_denials_have_zero_deletions(session_upload_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, facade: bool) -> None:
    env = session_upload_api
    descriptor = upload(env)
    own = seed_session(env, [descriptor])
    foreign = seed_session(env, [descriptor], chat=1)
    before = snapshot(env)
    reads: list[str] = []
    original_read = env["storage_adapter"].get_bytes

    def observed_read(namespace: str, key: str) -> bytes | None:
        reads.append(key)
        return original_read(namespace, key)

    monkeypatch.setattr(env["storage_adapter"], "get_bytes", observed_read)
    path = "/v1/conversation/rm" if facade else f"/api/v1/chats/{env['chats'][0]}/sessions"
    body = {"conversation_ids" if facade else "ids": [own]}
    for headers in ({}, {"Authorization": "Bearer invalid"}):
        response = requests.request("POST" if facade else "DELETE", env["base"] + path, headers=headers, json=body, timeout=30)
        assert response.status_code == 401 and snapshot(env) == before and reads == []
    for ids in ([foreign], [uuid4().hex], [own, foreign] if facade else [foreign, foreign]):
        response = delete_sessions(env, ids, facade=facade)
        assert response.json()["retcode" if facade else "code"] != 0, response.text
        assert snapshot(env) == before and reads == []
    if not facade:
        response = delete_sessions(env, [foreign], chat=1)
        assert response.json()["code"] == RetCode.AUTHENTICATION_ERROR
        assert snapshot(env) == before and reads == []
    with Session(env["engine"]) as db:
        db.execute(sa.update(UserTenant).where(UserTenant.user_id == env["owners"][0]).values(status="0"))
        db.commit()
    response = delete_sessions(env, [own], facade=facade)
    assert response.status_code == 401 and snapshot(env) == before and reads == []
    env["record"]["denial_readback"] = {"facade": facade, "sql_and_objects_unchanged": True, "storage_calls": len(reads)}


def test_client_files_cannot_select_objects_to_delete(session_upload_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = session_upload_api
    descriptor = upload(env, "api_key")
    unregistered, malformed = uuid4().hex, uuid4().hex
    namespace = f"{env['owners'][0]}-downloads"
    env["storage_adapter"].put(namespace, unregistered, b"unregistered object")
    env["storage_adapter"].put(namespace, malformed, b"malformed descriptor object")
    env["storage_adapter"].put(namespace, malformed + ".upload.json", b'{"created_by":"forged"}')
    files = [
        descriptor,
        {"id": descriptor["id"], "created_by": env["owners"][0], "bucket": env["owners"][1] + "-downloads"},
        {"id": unregistered, "created_by": env["owners"][0]},
        {"id": malformed, "created_by": env["owners"][0]},
        {"id": "../arbitrary", "created_by": env["owners"][0]},
        None,
    ]
    session_id = seed_session(env, files)
    before = snapshot(env)[1]

    def forbid_rm(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Untrusted message metadata deleted an object")

    monkeypatch.setattr(env["storage_adapter"], "rm", forbid_rm)
    response = delete_sessions(env, [session_id])
    assert response.json()["code"] == 0 and session_id not in snapshot(env)[0] and snapshot(env)[1] == before, response.text


def test_delete_all_stays_within_chat_and_keeps_external_reference(session_upload_api: dict[str, Any]) -> None:
    env = session_upload_api
    descriptor = upload(env)
    ids = [seed_session(env, [descriptor]) for _ in range(2)]
    other_chat = seed_session(env, [descriptor], chat=2)
    foreign = seed_session(env, [], chat=1)
    before = snapshot(env)[1]
    path = env["base"] + f"/api/v1/chats/{env['chats'][0]}/sessions"
    headers = {"Authorization": f"Bearer {env['jwt']}"}
    response = requests.delete(path, headers=headers, json={}, timeout=30)
    assert response.json() == {"code": 0, "data": {}} and set(snapshot(env)[0]) == {*ids, other_chat, foreign}
    response = requests.delete(path, headers=headers, json={"delete_all": True}, timeout=30)
    assert response.json()["code"] == 0 and set(snapshot(env)[0]) == {other_chat, foreign} and snapshot(env)[1] == before
    assert delete_sessions(env, [other_chat], chat=2).json()["code"] == 0 and snapshot(env)[1] == {}


def test_legacy_invited_or_inactive_membership_cannot_delete(session_upload_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = session_upload_api
    descriptor = upload(env)
    foreign = seed_session(env, [descriptor], chat=1)
    membership_id = uuid4().hex
    with Session(env["engine"]) as db:
        db.add(UserTenant(id=membership_id, user_id=env["owners"][0], tenant_id=env["owners"][1], role="invite", status="1", invited_by=env["owners"][1]))
        db.commit()
    before = snapshot(env)

    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Unaccepted or inactive tenant membership reached storage")

    monkeypatch.setattr(env["storage_adapter"], "get_bytes", forbidden)
    monkeypatch.setattr(env["storage_adapter"], "rm", forbidden)
    for role, status in [("invite", "1"), ("normal", "0")]:
        with Session(env["engine"]) as db:
            db.execute(sa.update(UserTenant).where(UserTenant.id == membership_id).values(role=role, status=status))
            db.commit()
        response = delete_sessions(env, [foreign], facade=True)
        assert response.json()["retcode"] == RetCode.OPERATING_ERROR and snapshot(env) == before, response.text


@pytest.mark.parametrize("facade", [False, True])
@pytest.mark.parametrize("fault", ["payload", "descriptor", "silent", "transport", "commit"])
def test_storage_and_sql_failures_can_be_retried(session_upload_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch, facade: bool, fault: str) -> None:
    env = session_upload_api
    descriptor = upload(env)
    session_id = seed_session(env, [descriptor])
    original_rm = env["storage_adapter"].rm
    original_read = env["storage_adapter"].get_bytes
    original_commit = AsyncSession.commit
    removals: list[str] = []

    def failing_rm(namespace: str, key: str) -> Any:
        removals.append(key)
        if (key == descriptor["id"] and fault == "payload") or (key.endswith(".upload.json") and fault == "descriptor"):
            raise OSError("controlled storage failure")
        if key == descriptor["id"] and fault == "silent":
            return None
        return original_rm(namespace, key)

    def failing_read(namespace: str, key: str) -> bytes | None:
        if fault == "transport":
            raise OSError("controlled descriptor read failure")
        return original_read(namespace, key)

    async def failing_commit(db: AsyncSession) -> None:
        if fault == "commit" and any(isinstance(row, Conversation) for row in db.deleted):
            raise sa.exc.OperationalError("COMMIT", {}, RuntimeError("controlled commit failure"))
        await original_commit(db)

    with monkeypatch.context() as patch:
        patch.setattr(env["storage_adapter"], "rm", failing_rm)
        patch.setattr(env["storage_adapter"], "get_bytes", failing_read)
        patch.setattr(AsyncSession, "commit", failing_commit)
        response = delete_sessions(env, [session_id], facade=facade)
    assert response.json()["retcode" if facade else "code"] != 0, response.text
    rows, objects = snapshot(env)
    assert session_id in rows
    if fault != "commit":
        assert f"{descriptor['created_by']}-downloads/{descriptor['id']}.upload.json" in objects
    if fault in {"payload", "silent", "transport"}:
        assert len(objects) == 2
    if fault == "transport":
        assert removals == []
    response = delete_sessions(env, [session_id], facade=facade)
    assert response.json()["retcode" if facade else "code"] == 0, response.text
    assert snapshot(env) == ({}, {})
    env["record"]["failure_retry_readback"] = {"facade": facade, "fault": fault, "sql_retained_on_failure": True, "retry_removed_sql_and_objects": True}


def test_concurrent_final_references_clean_once(session_upload_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = session_upload_api
    descriptor = upload(env)
    ids = [seed_session(env, [descriptor]) for _ in range(2)]
    original_rm = env["storage_adapter"].rm
    calls: list[str] = []

    def observed_rm(namespace: str, key: str) -> Any:
        calls.append(key)
        return original_rm(namespace, key)

    monkeypatch.setattr(env["storage_adapter"], "rm", observed_rm)
    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(lambda sid: delete_sessions(env, [sid]), ids))
    assert all(response.json()["code"] == 0 for response in responses), [response.text for response in responses]
    assert snapshot(env) == ({}, {}) and calls == [descriptor["id"], descriptor["id"] + ".upload.json"]


async def test_cancellation_drains_cleanup_before_releasing_sql(session_upload_api: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    env = session_upload_api
    descriptor = upload(env)
    session_id = seed_session(env, [descriptor])
    started, release = threading.Event(), threading.Event()
    original_rm = env["storage_adapter"].rm

    def blocked_rm(namespace: str, key: str) -> Any:
        started.set()
        assert release.wait(timeout=10), "test did not release controlled storage operation"
        return original_rm(namespace, key)

    monkeypatch.setattr(env["storage_adapter"], "rm", blocked_rm)
    engine = create_async_engine(env["engine"].url)
    try:
        async with AsyncSession(engine) as db:
            pending = asyncio.create_task(ConversationService.delete_with_runtime_uploads(db, tenant_id=env["owners"][0], owner_id=env["owners"][0], chat_id=env["chats"][0], session_id=session_id))
            try:
                assert await asyncio.to_thread(started.wait, 5)
                pending.cancel()
                await asyncio.sleep(0.05)
                assert not pending.done()
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await pending
            finally:
                release.set()
                if not pending.done():
                    pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
        assert session_id in snapshot(env)[0] and snapshot(env)[1] == {}
        response = delete_sessions(env, [session_id])
        assert response.json()["code"] == 0 and snapshot(env) == ({}, {})
    finally:
        release.set()
        await engine.dispose()


@pytest.mark.external_consumer
def test_pinned_web_remove_conversation_client(session_upload_api: dict[str, Any]) -> None:
    env = session_upload_api
    root = Path(os.environ["WEB_SESSION_CLEANUP_CHECKOUT"])
    expected = json.loads(Path(".github/consumer-web.json").read_text())["ref"]
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip() == expected
    descriptor = upload(env)
    session_id = seed_session(env, [descriptor])
    script = Path(__file__).resolve().parents[1] / "support/session_upload_web_consumer.mts"
    result = subprocess.run(
        [str(tsx_runner(root)), "--tsconfig", str(root / "tsconfig.app.json"), str(script)],
        cwd=root,
        env={**os.environ, "SESSION_CLEANUP_BASE": env["base"], "SESSION_CLEANUP_TOKEN": env["jwt"], "SESSION_CLEANUP_ID": session_id},
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    evidence = json.loads(result.stdout.splitlines()[-1])
    assert evidence["passed"] is True and snapshot(env) == ({}, {})
    env["record"]["actual_web_consumer"] = evidence
