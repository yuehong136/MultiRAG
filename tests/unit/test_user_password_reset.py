"""Password reset rejects incomplete arguments before Redis or SQL."""

from importlib import import_module
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

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
    redis = Mock(side_effect=AssertionError("incomplete input must not access Redis"))
    query = Mock(side_effect=AssertionError("incomplete input must not access SQL"))
    monkeypatch.setattr(user_api, "REDIS_CONN", SimpleNamespace(get=redis))
    monkeypatch.setattr(user_api.UserService, "query_password_user_by_email", query)
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
def test_password_reset_unverified_has_one_read_and_no_writes(client: TestClient, monkeypatch: pytest.MonkeyPatch, password: str) -> None:
    user_api = import_module("api.apps.restful_apis.user")
    redis_get = Mock(return_value=None)
    forbidden = Mock(side_effect=AssertionError("unverified reset must not query or write"))
    monkeypatch.setattr(user_api, "REDIS_CONN", SimpleNamespace(get=redis_get, delete=forbidden))
    monkeypatch.setattr(user_api.UserService, "query_password_user_by_email", forbidden)
    monkeypatch.setattr(user_api, "decrypt", forbidden)
    response = client.post("/api/v1/auth/password/reset", json={"email": "reset@example.test", "new_password": password, "confirm_new_password": password})
    assert response.json()["retcode"] == RetCode.AUTHENTICATION_ERROR
    assert response.json()["retmsg"] == "email not verified"
    redis_get.assert_called_once_with(user_api.verified_key("reset@example.test"))
    forbidden.assert_not_called()
