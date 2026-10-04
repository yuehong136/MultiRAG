"""Real HTTP authentication, session updates and independent SQL readback."""

from typing import Any
from uuid import uuid4

import pytest
import requests
import sqlalchemy as sa
from sqlalchemy.orm import Session

from api.db.db_models import Conversation, Dialog
from common.constants import RetCode
from tests.integration.test_runtime_document_upload import runtime_upload_api as runtime_upload_api


@pytest.mark.parametrize("credential", ["jwt", "api_key"])
def test_legacy_session_update_and_denials(runtime_upload_api: dict[str, Any], credential: str) -> None:
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
        for method, expected in (("PUT", "legacy name"), ("PATCH", "current name")):
            result = update(method, {"name": f"  {expected}  ", "user_id": foreign, "dialog_id": other_chat_id}).json()
            assert result["code"] == 0 and result["data"]["name"] == expected
            actual = readback()[session_id]
            assert actual["name"] == expected and actual["dialog_id"] == chat_id and actual["user_id"] == owner
            assert actual["message"] == original_message and actual["reference"] == original_reference
            body = requests.get(f"{env['base']}/api/v1/chats/{chat_id}/sessions/{session_id}", headers={"Authorization": f"Bearer {env[credential]}"}, timeout=30).json()
            assert body["code"] == 0 and body["data"]["name"] == expected

        before = readback()
        for method in ("PUT", "PATCH"):
            for payload in ({"messages": []}, {"message": []}, {"reference": []}, {"name": " "}):
                response = update(method, payload)
                assert response.status_code == 200 and response.json()["code"] == RetCode.DATA_ERROR
            assert update(method, {"name": []}).status_code == 422
            assert update(method, {"name": "denied"}, token="invalid-token").status_code == 401
            absent = requests.request(method, f"{env['base']}/api/v1/chats/{chat_id}/sessions/{session_id}", json={"name": "denied"}, timeout=30)
            assert absent.status_code == 401
            assert update(method, {"name": "denied"}, chat=other_chat_id, session=other_session_id).json()["code"] == RetCode.AUTHENTICATION_ERROR
            assert update(method, {"name": "denied"}, session=other_session_id).json()["code"] == RetCode.DATA_ERROR
            assert update(method, {"name": "denied"}, session=uuid4().hex).json()["code"] == RetCode.DATA_ERROR
            assert readback() == before

        paths = requests.get(f"{env['base']}/openapi.json", timeout=30).json()["paths"]
        path = paths["/api/v1/chats/{chat_id}/sessions/{session_id}"]
        assert path["put"]["deprecated"] is True and not path["patch"].get("deprecated", False)
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
