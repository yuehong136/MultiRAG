"""RESTful user route registration and compatibility-boundary contracts."""

from types import SimpleNamespace

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
