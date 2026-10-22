"""Message attribution comes from the verified credential boundary."""

from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from api.apps.services import memory_api_service
from api.identity.principal import (
    AuthenticatedActor,
    AuthenticationContext,
    AuthenticationSource,
    IdentityAssurance,
    TenantMembershipEvidence,
    build_principal_from_authenticated_actor,
)
from api.utils.api_utils import async_current_user


@pytest.mark.parametrize(
    ("source", "subject", "expected"),
    [
        (AuthenticationSource.WEB_SESSION, "victim", "user-unit"),
        (AuthenticationSource.WEB_SESSION, "", "user-unit"),
        (AuthenticationSource.SDK_API_TOKEN, " external-subject ", " external-subject "),
        (AuthenticationSource.SDK_API_TOKEN, "", ""),
        (AuthenticationSource.SDK_API_TOKEN, "   ", "   "),
    ],
)
def test_message_attribution_uses_principal_source(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    source: AuthenticationSource,
    subject: str,
    expected: str,
) -> None:
    principal = build_principal_from_authenticated_actor(
        actor=AuthenticatedActor(platform_user_id="user-unit"),
        membership=TenantMembershipEvidence(platform_user_id="user-unit", tenant_id="tenant-unit"),
        authentication=AuthenticationContext(source=source, assurance=IdentityAssurance.AUTHENTICATED, validated_at=datetime.now(UTC)),
    )
    monkeypatch.setitem(client.app.dependency_overrides, async_current_user, lambda: principal)
    messages: list[dict[str, Any]] = []

    async def capture(db: AsyncSession, memory_ids: list[str], message_dict: dict[str, Any], *, user_id: str) -> tuple[bool, str]:
        assert user_id == principal.id  # external subject never grants Memory permissions
        assert memory_ids == ["mem1"]
        messages.append(message_dict)
        return True, "ok"

    monkeypatch.setattr(memory_api_service, "add_message", capture)
    response = client.post(
        "/api/v1/messages",
        json={
            "memory_id": "mem1",
            "agent_id": "a",
            "session_id": "s",
            "user_input": "hi",
            "agent_response": "hello",
            "user_id": subject,
            "auth_via_api_token": True,
            "auth_type": "API",
            "authentication": {"source": "sdk_api_token"},
        },
    )
    assert response.status_code == 200
    assert response.json()["retcode"] == 0
    assert messages[0]["user_id"] == expected


@pytest.mark.parametrize("subject", [None, 42, ["subject"], {"id": "subject"}])
def test_invalid_message_subject_fails_validation_before_service(client: TestClient, monkeypatch: pytest.MonkeyPatch, subject: Any) -> None:
    async def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("invalid subject must not reach storage")

    monkeypatch.setattr(memory_api_service, "add_message", forbidden)
    response = client.post(
        "/api/v1/messages",
        json={"memory_id": "mem1", "agent_id": "a", "session_id": "s", "user_input": "hi", "agent_response": "hello", "user_id": subject},
    )
    assert response.status_code == 422
