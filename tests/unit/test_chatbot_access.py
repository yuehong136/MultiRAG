"""Service callers cannot bypass the chatbot route authorization preflight."""

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from api.db.db_models import API4Conversation, Dialog
from api.db.services import conversation_service as service


@pytest.mark.parametrize(
    ("dialog_tenant", "status", "session_dialog", "session_owner"),
    [("foreign", "1", "dialog", "tenant"), ("tenant", "0", "dialog", "tenant"), ("tenant", "1", "other", "tenant"), ("tenant", "1", "dialog", "foreign"), ("tenant", "1", "dialog", "")],
)
async def test_sync_and_async_services_reject_before_execution_or_writes(
    monkeypatch: pytest.MonkeyPatch, db: Session, async_db: AsyncSession, dialog_tenant: str, status: str, session_dialog: str, session_owner: str
) -> None:
    dialog = Dialog(id="dialog", tenant_id=dialog_tenant, status=status)
    session = API4Conversation(id="session", dialog_id=session_dialog, user_id=session_owner, message=[])
    monkeypatch.setattr(service.DialogService, "get_by_id", lambda *args: dialog)
    monkeypatch.setattr(service.API4ConversationService, "get_by_id", lambda *args: session)

    def unexpected(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("Denied chatbot reached execution or persistence")

    monkeypatch.setattr(service, "chat", unexpected)
    monkeypatch.setattr(service, "async_chat", unexpected)
    monkeypatch.setattr(service.API4ConversationService, "save", unexpected)
    monkeypatch.setattr(service.API4ConversationService, "append_message", unexpected)
    with pytest.raises(PermissionError, match="no access"):
        next(service.iframe_completion(db, "dialog", "blocked", "session", tenant_id="tenant", user_id=session_owner))
    with pytest.raises(PermissionError, match="no access"):
        await anext(service.async_iframe_completion(async_db, "dialog", "blocked", "session", tenant_id="tenant", user_id=session_owner))
    assert session.message == []


async def test_services_recheck_after_preflight(monkeypatch: pytest.MonkeyPatch, db: Session, async_db: AsyncSession) -> None:
    dialog = Dialog(id="dialog", tenant_id="tenant", status="1")
    monkeypatch.setattr(service.DialogService, "get_by_id", lambda *args: dialog)
    assert service.validate_iframe_access(db, "dialog", "tenant") == (dialog, None)
    dialog.status = "0"
    with pytest.raises(PermissionError):
        next(service.iframe_completion(db, "dialog", "blocked", tenant_id="tenant"))
    with pytest.raises(PermissionError):
        await anext(service.async_iframe_completion(async_db, "dialog", "blocked", tenant_id="tenant"))
