"""RESTful user route registration and compatibility-boundary contracts."""

from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import parse_qs, urlparse

from sqlalchemy.orm import Session

from api.db.services.user_service import UserService
from tests.unit.conftest import iter_api_routes


def test_restful_user_routes_replace_legacy_web_routes(client):
    routes = {(method, route.path) for route in iter_api_routes(client.app) for method in route.methods}

    expected = {
        ("POST", "/api/v1/auth/login"),
        ("GET", "/api/v1/auth/login/channels"),
        ("GET", "/api/v1/auth/login/{channel}"),
        ("GET", "/api/v1/auth/oauth/{channel}/callback"),
        ("POST", "/api/v1/auth/logout"),
        ("GET", "/api/v1/users/me"),
        ("PATCH", "/api/v1/users/me"),
        ("POST", "/api/v1/users"),
        ("GET", "/api/v1/users/me/models"),
        ("PATCH", "/api/v1/users/me/models"),
        ("POST", "/api/v1/auth/password/forgot/captcha"),
        ("POST", "/api/v1/auth/password/forgot/otp"),
        ("POST", "/api/v1/auth/password/forgot/otp/verify"),
        ("POST", "/api/v1/auth/password/reset"),
    }
    legacy = {
        ("POST", "/v1/user/login"),
        ("GET", "/v1/user/login/channels"),
        ("GET", "/v1/user/login/{channel}"),
        ("GET", "/v1/user/oauth/callback/{channel}"),
        ("GET", "/v1/user/logout"),
        ("GET", "/v1/user/info"),
        ("POST", "/v1/user/setting"),
        ("POST", "/v1/user/register"),
        ("GET", "/v1/user/tenant_info"),
        ("POST", "/v1/user/set_tenant_info"),
    }

    assert expected <= routes
    assert routes.isdisjoint(legacy)


def test_login_channels_uses_the_restful_envelope(client, monkeypatch):
    from api.apps.restful_apis import user_api

    monkeypatch.setattr(
        user_api.settings,
        "OAUTH_CONFIG",
        {"oidc": {"display_name": "Company SSO", "icon": "sso"}},
    )

    response = client.get("/api/v1/auth/login/channels")

    assert response.status_code == 200
    assert response.json() == {
        "retcode": 0,
        "retmsg": "success",
        "data": [{"channel": "oidc", "display_name": "Company SSO", "icon": "sso"}],
    }


def test_user_profile_uses_the_request_async_session(client, client_user, monkeypatch):
    sessions: list[Session] = []
    profile = {
        "id": client_user.id,
        "email": client_user.email,
        "nickname": client_user.nickname,
    }

    def _get_by_id(cls, db, user_id):
        sessions.append(db)
        assert user_id == client_user.id
        return SimpleNamespace(to_dict=lambda: profile)

    monkeypatch.setattr(UserService, "get_by_id", classmethod(_get_by_id))

    response = client.get("/api/v1/users/me")

    assert response.status_code == 200
    assert response.json()["data"] == profile
    assert sessions and all(isinstance(db, Session) for db in sessions)


def test_user_profile_patch_updates_only_profile_fields(client, client_user, monkeypatch):
    updates: list[dict] = []

    def _update_by_id(cls, db, user_id, values):
        assert isinstance(db, Session)
        assert user_id == client_user.id
        updates.append(values)
        return True

    monkeypatch.setattr(UserService, "update_by_id", classmethod(_update_by_id))

    response = client.patch("/api/v1/users/me", json={"nickname": "Updated", "status": "disabled"})

    assert response.status_code == 200
    assert response.json()["data"] is True
    assert updates == [{"nickname": "Updated"}]


def test_external_user_profile_returns_null_email_without_server_error(client, client_user, monkeypatch):
    from api.db import UserAccountKind
    from api.db.db_models import User

    external_user = User(
        id=client_user.id,
        nickname="External User",
        email=None,
        password=None,
        account_kind=UserAccountKind.EXTERNAL.value,
    )
    monkeypatch.setattr(
        UserService,
        "get_by_id",
        classmethod(lambda cls, db, user_id: external_user),
    )

    response = client.get("/api/v1/users/me")

    assert response.status_code == 200
    assert response.json()["data"]["email"] is None
    assert response.json()["data"]["account_kind"] == UserAccountKind.EXTERNAL.value
    assert "password" not in response.json()["data"]
    assert "access_token" not in response.json()["data"]


def test_oauth_callback_requires_state_and_provider_subject_binding(
    client,
    monkeypatch,
) -> None:
    from api.apps.auth.oauth import OAuthClient
    from api.apps.restful_apis import user_api

    async def _exchange(self, code: str) -> dict[str, str]:
        assert code == "code"
        return {"access_token": "test-token"}

    async def _fetch(self, access_token: str, *, id_token=None):
        assert access_token == "test-token"
        assert id_token is None
        return SimpleNamespace(
            email="collision@example.test",
            nickname="Collision",
            avatar_url="",
        )

    monkeypatch.setattr(
        user_api.settings,
        "OAUTH_CONFIG",
        {
            "oidc": {
                "type": "oauth2",
                "client_id": "client",
                "client_secret": "secret",
                "authorization_url": "https://idp.example/authorize",
                "token_url": "https://idp.example/token",
                "userinfo_url": "https://idp.example/userinfo",
                "redirect_uri": "https://multirag.example/callback",
            }
        },
    )
    monkeypatch.setattr(OAuthClient, "async_exchange_code_for_token", _exchange)
    monkeypatch.setattr(OAuthClient, "async_fetch_user_info", _fetch)
    query = Mock(side_effect=AssertionError("email must not become an identity credential"))
    monkeypatch.setattr(
        UserService,
        "query",
        classmethod(lambda cls, db, **kwargs: query(**kwargs)),
    )

    missing_state = client.get(
        "/api/v1/auth/oauth/oidc/callback?code=code",
        follow_redirects=False,
    )
    assert missing_state.headers["location"] == "/?error=invalid_state"

    login_response = client.get("/api/v1/auth/login/oidc", follow_redirects=False)
    state = parse_qs(urlparse(login_response.headers["location"]).query)["state"][0]
    response = client.get(
        f"/api/v1/auth/oauth/oidc/callback?code=code&state={state}",
        follow_redirects=False,
    )

    assert response.status_code in {302, 307}
    assert response.headers["location"] == "/?error=oauth_identity_binding_required"
    query.assert_not_called()


def test_registration_failure_does_not_echo_persistence_parameters(
    client,
    monkeypatch,
) -> None:
    from fastapi import HTTPException

    from api.apps.restful_apis import user_api
    from api.utils.crypt import crypt

    leaked_detail = "bcrypt-hash access-token collision@example.test"
    monkeypatch.setattr(user_api.settings, "REGISTER_ENABLED", 1)
    monkeypatch.setattr(
        UserService,
        "query",
        classmethod(lambda cls, db, **kwargs: []),
    )
    monkeypatch.setattr(
        UserService,
        "save",
        classmethod(lambda cls, db, **kwargs: (_ for _ in ()).throw(HTTPException(status_code=500, detail=leaked_detail))),
    )

    response = client.post(
        "/api/v1/users",
        json={
            "email": "collision@example.test",
            "nickname": "Collision",
            "password": crypt("secret-password"),
        },
    )

    payload = response.json()
    assert payload["retmsg"] == "User registration failed."
    assert leaked_detail not in response.text
    assert "collision@example.test" not in payload["retmsg"]


def test_password_recovery_rejects_external_only_account_before_redis(client, monkeypatch):
    from api.apps.restful_apis import user_api

    monkeypatch.setattr(
        UserService,
        "query_password_user_by_email",
        classmethod(lambda cls, db, email: None),
    )
    redis_get = Mock(side_effect=AssertionError("external account must not enter password recovery"))
    monkeypatch.setattr(user_api, "REDIS_CONN", SimpleNamespace(get=redis_get))

    response = client.post(
        "/api/v1/auth/password/forgot/otp",
        json={"email": "external@example.test", "captcha": "ABCD"},
    )

    assert response.status_code == 200
    assert response.json()["retmsg"] == "invalid email"
    redis_get.assert_not_called()
