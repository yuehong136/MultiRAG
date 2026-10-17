"""Authentication switches must deny before account reads and writes."""

import asyncio

import pytest
from fastapi import HTTPException
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from common import settings


@pytest.mark.parametrize(("raw", "expected"), [("false", False), ("0", False), ("true", True), ("1", True)])
def test_auth_switch_explicit_environment_overrides_config(monkeypatch: pytest.MonkeyPatch, raw: str, expected: bool) -> None:
    monkeypatch.setenv("DISABLE_PASSWORD_LOGIN", raw)
    assert settings._auth_switch("DISABLE_PASSWORD_LOGIN", not expected) is expected


def test_invalid_auth_switch_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REGISTER_ENABLED", "invalid")
    with pytest.raises(ValueError, match="REGISTER_ENABLED"):
        settings._auth_switch("REGISTER_ENABLED", True)


def test_disabled_password_login_does_not_touch_database(monkeypatch: pytest.MonkeyPatch) -> None:
    from api import apps
    from api.apps.restful_apis import user_api

    monkeypatch.setattr(settings, "DISABLE_PASSWORD_LOGIN", True)
    response = asyncio.run(user_api.login(user_api.LoginRequest(username="owned@example.invalid", password="invalid"), db=AsyncSession()))
    assert response.status_code == 200 and b"Password login is disabled" in response.body
    with pytest.raises(HTTPException) as denied:
        apps.login(data=OAuth2PasswordRequestForm(username="owned@example.invalid", password="invalid"), db=Session())
    assert denied.value.status_code == 403
