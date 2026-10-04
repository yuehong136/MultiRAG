"""Current session PATCH validates requests; retired PUT cannot reach storage."""

from typing import Any

import pytest

from api.db.services.conversation_service import ConversationService
from api.db.services.dialog_service import DialogService
from common.constants import RetCode


@pytest.mark.parametrize("payload", [{"messages": []}, {"message": []}, {"reference": []}, {"name": " "}])
def test_session_update_rejects_protected_fields(client: Any, monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]) -> None:
    monkeypatch.setattr(DialogService, "query", classmethod(lambda cls, db, **kw: [object()]))
    monkeypatch.setattr(ConversationService, "query", classmethod(lambda cls, db, **kw: [object()]))

    def forbid_write(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Rejected update wrote the session")

    monkeypatch.setattr(ConversationService, "update_by_id", forbid_write)
    response = client.patch("/api/v1/chats/chat-1/sessions/session-1", json=payload)
    assert response.status_code == 200
    assert response.json()["code"] == RetCode.DATA_ERROR


def test_session_update_foreign_chat_does_not_query_session(client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(DialogService, "query", classmethod(lambda cls, db, **kw: []))

    def forbid_read(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Unauthorized update accessed the session")

    monkeypatch.setattr(ConversationService, "query", forbid_read)
    response = client.patch("/api/v1/chats/foreign/sessions/session-1", json={"name": "changed"})
    assert response.status_code == 200
    assert response.json() == {"code": RetCode.AUTHENTICATION_ERROR, "message": "No authorization."}


def test_session_update_invalid_body_has_no_write(client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    def forbid_write(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Invalid body reached storage")

    monkeypatch.setattr(ConversationService, "update_by_id", forbid_write)
    response = client.patch("/api/v1/chats/chat-1/sessions/session-1", json={"name": ["invalid"]})
    assert response.status_code == 422


def test_retired_session_put_does_not_access_storage(client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    def forbid_access(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Retired PUT reached storage")

    monkeypatch.setattr(DialogService, "query", forbid_access)
    monkeypatch.setattr(ConversationService, "query", forbid_access)
    monkeypatch.setattr(ConversationService, "update_by_id", forbid_access)
    response = client.put("/api/v1/chats/chat-1/sessions/session-1", json={"name": "retired"})
    assert response.status_code == 405
