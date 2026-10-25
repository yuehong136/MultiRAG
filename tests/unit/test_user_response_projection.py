"""Actual user projections at generic and authenticated response boundaries."""

from importlib import import_module
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.db_models import User, get_async_db
from api.db.services.user_service import UserService
from api.utils.crypt import crypt


def actual_user(identifier: str = "user-unit", email: str | None = "unit@example.test") -> User:
    user = User(id=identifier, nickname="Profile", email=email, password="private-password-hash", access_token="private-access-token", is_active=True)
    user.future_private_field = "private-future-field"
    return user


@pytest.mark.parametrize("email", [None, "unit@example.test"])
def test_generic_whitelist_excludes_email_and_unknown_credentials(email: str | None) -> None:
    user = actual_user(email=email)
    generic = user.to_dict()
    own = user.to_dict(for_self=True)
    assert "email" not in generic and own["email"] == email
    for payload in (generic, own):
        assert "password" not in payload and "access_token" not in payload and "future_private_field" not in payload


@pytest.mark.parametrize("path", ["/api/v1/auth/login", "/api/v1/users", "/api/v1/users/me"])
def test_self_http_responses_keep_email_without_credentials(client: TestClient, client_user: Any, monkeypatch: pytest.MonkeyPatch, path: str) -> None:
    module = import_module("api.apps.restful_apis.user")
    user = actual_user(client_user.id)
    db = AsyncSession()
    monkeypatch.setattr(db, "commit", AsyncMock())
    monkeypatch.setattr(db, "add", Mock())
    client.app.dependency_overrides[get_async_db] = lambda: db
    monkeypatch.setattr(UserService, "get_by_id", classmethod(lambda *args: user))
    monkeypatch.setattr(UserService, "query_user", classmethod(lambda *args: user))
    monkeypatch.setattr(UserService, "query", classmethod(lambda *args, **kwargs: [] if path.endswith("/users") else [user]))
    monkeypatch.setattr(module, "user_register", lambda *args: [user])
    monkeypatch.setattr(module.settings, "REGISTER_ENABLED", 1)
    monkeypatch.setattr(module.settings, "DISABLE_PASSWORD_LOGIN", False)
    if path.endswith("/me"):
        response = client.get(path)
    else:
        payload = {"username": user.email, "password": crypt("plaintext")} if path.endswith("/login") else {"email": user.email, "nickname": user.nickname, "password": crypt("plaintext")}
        response = client.post(path, json=payload)
    assert response.status_code == 200 and response.json()["retcode"] == 0
    data = response.json()["data"]
    assert data["email"] == user.email and data["id"] == user.id
    assert "password" not in data and "access_token" not in data
    assert "private-password-hash" not in response.text and "private-access-token" not in response.text and "private-future-field" not in response.text


def test_reset_http_returns_boolean_without_user_or_token(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    module = import_module("api.apps.restful_apis.user")
    user = actual_user()
    monkeypatch.setattr(module, "REDIS_CONN", Mock(get=Mock(return_value="verified")))
    monkeypatch.setattr(UserService, "query_password_user_by_email", classmethod(lambda *args: user))
    update = Mock(return_value=True)
    monkeypatch.setattr(UserService, "update_user_password", update)
    response = client.post("/api/v1/auth/password/reset", json={"email": user.email, "new_password": "new", "confirm_new_password": "new"})
    assert response.status_code == 200 and response.json()["retcode"] == 0
    assert response.json()["data"] is True and "authorization" not in response.headers
    assert "private" not in response.text and user.email not in response.text
    update.assert_called_once()
