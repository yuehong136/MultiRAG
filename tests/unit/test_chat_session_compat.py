"""Legacy session update uses the current validation, authorization and handler."""

from typing import Any

import pytest

from api.db.services.conversation_service import ConversationService
from api.db.services.dialog_service import DialogService
from common.constants import RetCode


@pytest.mark.parametrize("method", ["put", "patch"])
@pytest.mark.parametrize("payload", [{"messages": []}, {"message": []}, {"reference": []}, {"name": " "}])
def test_session_update_rejects_protected_fields(client: Any, monkeypatch: pytest.MonkeyPatch, method: str, payload: dict[str, Any]) -> None:
    monkeypatch.setattr(DialogService, "query", classmethod(lambda cls, db, **kw: [object()]))
    monkeypatch.setattr(ConversationService, "query", classmethod(lambda cls, db, **kw: [object()]))

    def forbid_write(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Rejected update wrote the session")

    monkeypatch.setattr(ConversationService, "update_by_id", forbid_write)
    response = getattr(client, method)("/api/v1/chats/chat-1/sessions/session-1", json=payload)
    assert response.status_code == 200
    assert response.json()["code"] == RetCode.DATA_ERROR


@pytest.mark.parametrize("method", ["put", "patch"])
def test_session_update_foreign_chat_does_not_query_session(client: Any, monkeypatch: pytest.MonkeyPatch, method: str) -> None:
    monkeypatch.setattr(DialogService, "query", classmethod(lambda cls, db, **kw: []))

    def forbid_read(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Unauthorized update accessed the session")

    monkeypatch.setattr(ConversationService, "query", forbid_read)
    response = getattr(client, method)("/api/v1/chats/foreign/sessions/session-1", json={"name": "changed"})
    assert response.status_code == 200
    assert response.json() == {"code": RetCode.AUTHENTICATION_ERROR, "message": "No authorization."}


def test_session_update_invalid_body_has_no_write(client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    def forbid_write(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Invalid body reached storage")

    monkeypatch.setattr(ConversationService, "update_by_id", forbid_write)
    for method in ("put", "patch"):
        response = getattr(client, method)("/api/v1/chats/chat-1/sessions/session-1", json={"name": ["invalid"]})
        assert response.status_code == 422
