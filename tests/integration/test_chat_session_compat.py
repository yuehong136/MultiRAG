"""Real HTTP authentication, session updates and independent SQL readback."""

from typing import Any
from uuid import uuid4

import pytest
import requests
import sqlalchemy as sa
from sqlalchemy.orm import Session

from api.db.db_models import Conversation, Dialog
from common.constants import RetCode
from tests.support.runtime_upload import runtime_upload_api as runtime_upload_api


@pytest.mark.parametrize("credential", ["jwt", "api_key"])
def test_session_patch_and_retired_put(runtime_upload_api: dict[str, Any], credential: str) -> None:
    env = runtime_upload_api
    owner = env["owners"][0 if credential == "jwt" else 1]
    foreign = env["owners"][1 if credential == "jwt" else 0]
    chat_id, other_chat_id, session_id, other_session_id = (uuid4().hex for _ in range(4))
    original_message = [{"role": "assistant", "content": "preserve me"}]
    original_reference = [{"chunks": [], "doc_aggs": []}]
    with Session(env["engine"]) as db:
        db.add_all([Dialog(id=chat_id, tenant_id=owner, name="Compatibility scratch", llm_id="unused"), Dialog(id=other_chat_id, tenant_id=foreign, name="Foreign scratch", llm_id="unused")])
        db.add_all(
            [
                Conversation(id=session_id, dialog_id=chat_id, user_id=owner, name="before", message=original_message, reference=original_reference),
                Conversation(id=other_session_id, dialog_id=other_chat_id, user_id=foreign, name="foreign", message=[], reference=[]),
            ]
        )
        db.commit()

    def readback() -> dict[str, Any]:
        with Session(env["engine"]) as db:
            return {row.id: row.to_dict() for row in db.scalars(sa.select(Conversation).where(Conversation.id.in_([session_id, other_session_id])))}

    def update(method: str, body: dict[str, Any], chat: str = chat_id, session: str = session_id, token: str | None = None) -> requests.Response:
        return requests.request(method, f"{env['base']}/api/v1/chats/{chat}/sessions/{session}", headers={"Authorization": f"Bearer {token or env[credential]}"}, json=body, timeout=30)

    try:
        for method, expected in (("PATCH", "first name"), ("PATCH", "current name")):
            result = update(method, {"name": f"  {expected}  ", "user_id": foreign, "dialog_id": other_chat_id}).json()
            assert result["code"] == 0 and result["data"]["name"] == expected
            actual = readback()[session_id]
            assert actual["name"] == expected and actual["dialog_id"] == chat_id and actual["user_id"] == owner
            assert actual["message"] == original_message and actual["reference"] == original_reference
            body = requests.get(f"{env['base']}/api/v1/chats/{chat_id}/sessions/{session_id}", headers={"Authorization": f"Bearer {env[credential]}"}, timeout=30).json()
            assert body["code"] == 0 and body["data"]["name"] == expected

        before = readback()
        for payload in ({"messages": []}, {"message": []}, {"reference": []}, {"name": " "}):
            response = update("PATCH", payload)
            assert response.status_code == 200 and response.json()["code"] == RetCode.DATA_ERROR
        assert update("PATCH", {"name": []}).status_code == 422
        assert update("PATCH", {"name": "denied"}, token="invalid-token").status_code == 401
        absent = requests.request("PATCH", f"{env['base']}/api/v1/chats/{chat_id}/sessions/{session_id}", json={"name": "denied"}, timeout=30)
        assert absent.status_code == 401
        assert update("PATCH", {"name": "denied"}, chat=other_chat_id, session=other_session_id).json()["code"] == RetCode.AUTHENTICATION_ERROR
        assert update("PATCH", {"name": "denied"}, session=other_session_id).json()["code"] == RetCode.DATA_ERROR
        assert update("PATCH", {"name": "denied"}, session=uuid4().hex).json()["code"] == RetCode.DATA_ERROR
        assert readback() == before

        for payload in ({"name": "retired"}, {"messages": []}, {"name": []}):
            response = update("PUT", payload)
            assert response.status_code == 405
            assert readback() == before
        unauthenticated_put = requests.put(f"{env['base']}/api/v1/chats/{chat_id}/sessions/{session_id}", json={"name": "retired"}, timeout=30)
        assert unauthenticated_put.status_code == 405
        assert readback() == before

        paths = requests.get(f"{env['base']}/openapi.json", timeout=30).json()["paths"]
        path = paths["/api/v1/chats/{chat_id}/sessions/{session_id}"]
        assert "put" not in path and not path["patch"].get("deprecated", False)
        for retired in (
            "/api/v1/file/list",
            "/api/v1/file/root_folder",
            "/api/v1/file/rename",
            "/api/v1/file/upload_info",
            "/v1/document/change_parser",
            "/v1/document/run",
            "/v1/document/image/{image_id}",
        ):
            assert retired not in paths
        assert "put" not in paths["/api/v1/datasets/{dataset_id}/documents/{document_id}/chunks/{chunk_id}"]
        objects_before = [item.object_name for item in env["storage"].list_objects(env["bucket"], recursive=True)]
        for method, retired in (("POST", "/v1/document/run"), ("POST", "/v1/document/change_parser"), ("GET", "/v1/document/image/test-image"), ("POST", "/api/v1/file/rename")):
            response = requests.request(
                method, f"{env['base']}{retired}", headers={"Authorization": f"Bearer {env[credential]}"}, json={"doc_ids": [uuid4().hex], "file_id": uuid4().hex, "name": "denied"}, timeout=30
            )
            assert response.status_code == 404 and response.json()["code"] == 404
        assert readback() == before
        assert [item.object_name for item in env["storage"].list_objects(env["bucket"], recursive=True)] == objects_before
    finally:
        with Session(env["engine"]) as db:
            db.execute(sa.delete(Conversation).where(Conversation.id.in_([session_id, other_session_id])))
            db.execute(sa.delete(Dialog).where(Dialog.id.in_([chat_id, other_chat_id])))
            db.commit()
        assert readback() == {}


def test_retrieval_and_thinking_survive_completion_and_history_readback(
    runtime_upload_api: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json
    import sys
    from collections.abc import AsyncIterator

    from api.db.services.dialog_service import _deep_research_event_payload

    env = runtime_upload_api
    chat_id, session_id = uuid4().hex, uuid4().hex
    owner = env["owners"][0]
    trace = "<retrieving>lookup<br/></retrieving>"
    expected = trace + "<think>model reasoning</think>Answer [ID:0]"
    references = {"chunks": [{"chunk_id": "chunk-1", "doc_id": "doc-1", "docnm_kwd": "Fixture source", "content_with_weight": "Evidence"}], "doc_aggs": []}

    async def controlled_chat(*args: Any, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        for message in ("<START_DEEP_RESEARCH><br/>", "lookup<br/>", "<END_DEEP_RESEARCH><br/>"):
            yield _deep_research_event_payload(message)
        yield {"answer": "", "start_to_think": True, "reference": {}, "final": False}
        yield {"answer": "model reasoning", "reference": {}, "final": False}
        yield {"answer": "", "end_to_think": True, "reference": {}, "final": False}
        yield {"answer": "Answer", "reference": {}, "final": False}
        yield {"answer": expected, "reference": references, "final": True}

    # register_page loads the active router under this canonical module name.
    monkeypatch.setattr(sys.modules["api.apps.restful_apis.chat"], "async_chat", controlled_chat)
    with Session(env["engine"]) as db:
        db.add(Dialog(id=chat_id, tenant_id=owner, name="Retrieval scratch", llm_id="controlled"))
        db.add(Conversation(id=session_id, dialog_id=chat_id, user_id=owner, name="Replay scratch", message=[], reference=[]))
        db.commit()
    headers = {"Authorization": f"Bearer {env['jwt']}"}
    try:
        response = requests.post(
            f"{env['base']}/api/v1/chat/completions",
            headers=headers,
            json={"chat_id": chat_id, "session_id": session_id, "messages": [{"role": "user", "content": "Fixture question"}], "stream": True},
            timeout=30,
        )
        assert response.status_code == 200 and response.headers["content-type"].startswith("text/event-stream")
        frames = [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith("data:")]
        assert all(frame["code"] == 0 for frame in frames)
        assert frames[-1]["data"] is True
        assert [frame["data"]["answer"] for frame in frames[:3]] == ["<retrieving>", "lookup<br/>", "</retrieving>"]
        assert frames[-2]["data"]["answer"] == expected
        assert frames[-2]["data"]["final"] is True
        assert frames[-2]["data"]["reference"]["chunks"][0]["document_id"] == "doc-1"
        with Session(env["engine"]) as db:
            persisted = db.get(Conversation, session_id)
            assert persisted is not None
            assert persisted.message[-1]["content"] == expected
            assert persisted.reference[-1]["chunks"][0]["document_id"] == "doc-1"
        history = requests.get(f"{env['base']}/api/v1/chats/{chat_id}/sessions/{session_id}", headers=headers, timeout=30).json()
        assert history["code"] == 0
        assert history["data"]["messages"][-1]["content"] == expected
        assert history["data"]["reference"][-1]["chunks"][0]["document_id"] == "doc-1"
    finally:
        with Session(env["engine"]) as db:
            db.execute(sa.delete(Conversation).where(Conversation.id == session_id))
            db.execute(sa.delete(Dialog).where(Dialog.id == chat_id))
            db.commit()
        with Session(env["engine"]) as db:
            assert db.get(Conversation, session_id) is None
            assert db.get(Dialog, chat_id) is None
