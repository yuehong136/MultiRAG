"""Password reset rejects incomplete arguments before Redis or SQL."""

from importlib import import_module
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from common.constants import RetCode


@pytest.mark.parametrize("field", ["email", "new_password", "confirm_new_password"])
@pytest.mark.parametrize("value", ["", None, "omitted"])
def test_password_reset_missing_input_has_no_io(client: TestClient, monkeypatch: pytest.MonkeyPatch, field: str, value: str | None) -> None:
    user_api = import_module("api.apps.restful_apis.user")
    payload: dict[str, Any] = {"email": "reset@example.test", "new_password": "password", "confirm_new_password": "password"}
    if value == "omitted":
        payload.pop(field)
    else:
        payload[field] = value
    redis = AsyncMock(side_effect=AssertionError("incomplete input must not access Redis"))
    query = AsyncMock(side_effect=AssertionError("incomplete input must not access SQL"))
    monkeypatch.setattr(user_api.recovery, "redis_eval", redis)
    monkeypatch.setattr(user_api.recovery, "account", query)
    response = client.post("/api/v1/auth/password/reset", json=payload)
    if value == "":
        assert response.status_code == 200
        assert response.json()["retcode"] == RetCode.ARGUMENT_ERROR
        assert response.json()["retmsg"] == "email and passwords are required"
    else:
        assert response.status_code == 422
    redis.assert_not_called()
    query.assert_not_called()


@pytest.mark.parametrize("password", ["password", "not-an-rsa-ciphertext!"])
def test_password_reset_without_cookie_has_no_io(client: TestClient, monkeypatch: pytest.MonkeyPatch, password: str) -> None:
    user_api = import_module("api.apps.restful_apis.user")
    forbidden = AsyncMock(side_effect=AssertionError("unverified reset must not query or write"))
    monkeypatch.setattr(user_api.recovery, "account", forbidden)
    monkeypatch.setattr(user_api.recovery, "redis_eval", forbidden)
    response = client.post("/api/v1/auth/password/reset", json={"email": "reset@example.test", "new_password": password, "confirm_new_password": password})
    assert response.json()["retcode"] == RetCode.AUTHENTICATION_ERROR
    assert response.json()["retmsg"] == "email not verified"
    forbidden.assert_not_called()
