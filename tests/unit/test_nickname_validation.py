"""Registration/profile share trim, character and Unicode length boundaries."""

import sys
from typing import Any
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from api import apps  # noqa: F401
from api.db.services.user_service import UserService
from api.utils.nickname_validation import normalize_nickname

user_api = sys.modules["api.apps.restful_apis.user"]


@pytest.mark.parametrize("name", ["John Doe", "张三", "한국어", "O'Brien._-123", "Ⅻ²", "𐐀" * 100])
def test_valid_nickname_is_trimmed_by_codepoint(name: str) -> None:
    assert normalize_nickname("\ufeff \t" + name + "\n\u3000") == name


@pytest.mark.parametrize("name", [None, 123, True, [], {}, "", " \ufeff ", "a" * 101, "𐐀" * 101, "John\tDoe", "John\nDoe", "name!", "😀", "Jose\u0301", "\u0085张三\u0085", "\x1c张三\x1c"])
@pytest.mark.parametrize("method,path", [("POST", "/api/v1/users"), ("PATCH", "/api/v1/users/me")])
def test_invalid_nickname_has_no_service_or_password_io(client: TestClient, monkeypatch: pytest.MonkeyPatch, name: object, method: str, path: str) -> None:
    io = Mock(side_effect=AssertionError("invalid name must not reach SQL/password IO"))
    for member in ("query", "get_by_id", "update_by_id"):
        monkeypatch.setattr(UserService, member, io)
    monkeypatch.setattr(user_api, "user_register", io)
    monkeypatch.setattr(user_api, "decrypt", io)
    response = client.request(method, path, json={"email": "new@example.test", "password": "invalid-encryption", "nickname": name})
    assert response.status_code == 422, response.text
    assert response.json()["detail"][0]["loc"] == ["body", "nickname"]
    io.assert_not_called()


def test_registration_requires_nickname(client: TestClient) -> None:
    response = client.post("/api/v1/users", json={"email": "new@example.test", "password": "unused"})
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "nickname"]


@pytest.mark.parametrize("payload,expected", [({"timezone": "Asia/Shanghai"}, {"timezone": "Asia/Shanghai"}), ({"nickname": "\ufeff 张三 한국어 \ufeff"}, {"nickname": "张三 한국어"})])
def test_profile_omission_and_normalized_update(client: TestClient, monkeypatch: pytest.MonkeyPatch, payload: dict[str, str], expected: dict[str, str]) -> None:
    update = Mock(return_value=True)
    monkeypatch.setattr(UserService, "update_by_id", update)
    response = client.patch("/api/v1/users/me", json=payload)
    assert response.status_code == 200 and response.json()["data"] is True
    assert update.call_args.args[2] == expected


def test_registration_passes_trimmed_name_to_transaction(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from api.utils.crypt import crypt

    monkeypatch.setattr(user_api.settings, "REGISTER_ENABLED", 1)
    monkeypatch.setattr(UserService, "query", Mock(return_value=[]))
    register = Mock(return_value=[SimpleNamespace(email="new@example.test", to_dict=lambda **_: {"nickname": "张三"})])
    monkeypatch.setattr(user_api, "user_register", register)
    response = client.post("/api/v1/users", json={"email": "new@example.test", "password": crypt("password"), "nickname": "\ufeff 张三 \ufeff"})
    assert response.status_code == 200 and response.json()["retcode"] == 0
    values: dict[str, Any] = register.call_args.args[2]
    assert values["nickname"] == "张三"
