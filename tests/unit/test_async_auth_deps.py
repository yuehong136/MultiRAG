"""async 鉴权依赖真依赖路径测试（§11 Phase 2 任务 0）。

不经 dependency_overrides 遮蔽：以真实 Request + 未绑定 AsyncSession 直接调用依赖
本体，service 查询打桩，验证 run_sync 全链路与双认语义（web JWT 优先、SDK API-key
兜底、失败抛 SDKAuthError/401）。写法对齐同步版范本 test_dataset_auth.py /
test_api_utils_token_required.py。
"""

import types

import pytest
from fastapi import HTTPException
from starlette.requests import Request

import api.apps as apps
from api.db.services.api_service import APITokenService
from api.db.services.user_service import UserService
from api.identity import legacy_owner
from api.identity.principal import AuthenticationSource, IdentityAssurance
from api.utils.api_utils import (
    Principal,
    SDKAuthError,
    async_beta_token_required,
    async_current_tenant_id,
    async_current_user,
    async_token_required,
)


def _req(auth=None):
    """构造真实 starlette Request（beartype 会校验类型）。"""
    headers = []
    if auth is not None:
        headers.append((b"authorization", auth.encode()))
    return Request({"type": "http", "headers": headers})


def _token_rows(tenant_id="tenant-async"):
    return [types.SimpleNamespace(tenant_id=tenant_id)]


def _active_user(user_id: str, email: str | None, nickname: str):
    return types.SimpleNamespace(
        id=user_id,
        email=email,
        nickname=nickname,
        status="1",
        is_active=True,
        is_authenticated=True,
        is_anonymous=False,
    )


def _owner_tenant_ids(_session, user_id: str):
    return [user_id]


# ---------------------------------------------------------------------------
# async_token_required
# ---------------------------------------------------------------------------


async def test_async_token_required_returns_tenant_id(monkeypatch, async_db):
    monkeypatch.delenv("DISABLE_SDK", raising=False)
    monkeypatch.setattr(APITokenService, "query", lambda s, **kw: _token_rows() if kw.get("token") == "tok-1" else [])

    assert await async_token_required(_req("Bearer tok-1"), async_db) == "tenant-async"


async def test_async_token_required_rejects_invalid_api_key(monkeypatch, async_db):
    monkeypatch.delenv("DISABLE_SDK", raising=False)
    monkeypatch.setattr(APITokenService, "query", lambda s, **kw: [])

    with pytest.raises(SDKAuthError):
        await async_token_required(_req("Bearer bad"), async_db)


async def test_async_token_required_rejects_missing_authorization(monkeypatch, async_db):
    monkeypatch.delenv("DISABLE_SDK", raising=False)

    with pytest.raises(SDKAuthError):
        await async_token_required(_req(None), async_db)


async def test_async_token_required_disable_sdk(monkeypatch, async_db):
    monkeypatch.setenv("DISABLE_SDK", "1")

    with pytest.raises(SDKAuthError):
        await async_token_required(_req("Bearer tok-1"), async_db)


# ---------------------------------------------------------------------------
# async_beta_token_required
# ---------------------------------------------------------------------------


async def test_async_beta_token_required_returns_tenant_id(monkeypatch, async_db):
    monkeypatch.setattr(APITokenService, "query", lambda s, **kw: _token_rows("tenant-beta") if kw.get("beta") == "beta-1" else [])

    assert await async_beta_token_required(_req("Bearer beta-1"), async_db) == "tenant-beta"


async def test_async_beta_token_required_rejects_invalid(monkeypatch, async_db):
    monkeypatch.setattr(APITokenService, "query", lambda s, **kw: [])

    with pytest.raises(SDKAuthError):
        await async_beta_token_required(_req("Bearer bad"), async_db)


# ---------------------------------------------------------------------------
# async_current_tenant_id
# ---------------------------------------------------------------------------


async def test_async_current_tenant_id_web_jwt_path(monkeypatch, async_db):
    monkeypatch.delenv("DISABLE_SDK", raising=False)
    monkeypatch.setattr(apps.manager, "_get_payload", lambda token: {"sub": "alice@example.com"})
    monkeypatch.setattr(apps, "load_user", lambda email, d: types.SimpleNamespace(id="tenant-web"))

    assert await async_current_tenant_id(_req("Bearer jwt-token"), async_db) == "tenant-web"


async def test_async_current_tenant_id_api_key_fallback(monkeypatch, async_db):
    monkeypatch.delenv("DISABLE_SDK", raising=False)

    def _raise(token):
        raise ValueError("not a jwt")

    monkeypatch.setattr(apps.manager, "_get_payload", _raise)
    monkeypatch.setattr(APITokenService, "query", lambda s, **kw: _token_rows("tenant-sdk"))

    assert await async_current_tenant_id(_req("Bearer api-key"), async_db) == "tenant-sdk"


async def test_async_current_tenant_id_rejects_invalid(monkeypatch, async_db):
    monkeypatch.delenv("DISABLE_SDK", raising=False)

    def _raise(token):
        raise ValueError("not a jwt")

    monkeypatch.setattr(apps.manager, "_get_payload", _raise)
    monkeypatch.setattr(APITokenService, "query", lambda s, **kw: [])

    with pytest.raises(SDKAuthError):
        await async_current_tenant_id(_req("Bearer bad"), async_db)


# ---------------------------------------------------------------------------
# async_current_user
# ---------------------------------------------------------------------------


async def test_async_current_user_web_jwt_path(monkeypatch, async_db):
    monkeypatch.setattr(apps.manager, "_get_payload", lambda token: {"sub": "alice@example.com"})
    monkeypatch.setattr(apps, "load_user", lambda email, d: _active_user("uid-1", "alice@example.com", "Alice"))
    monkeypatch.setattr(legacy_owner, "_load_personal_owner_tenant_ids", _owner_tenant_ids)

    principal = await async_current_user(_req("Bearer jwt-token"), async_db)

    assert isinstance(principal, Principal)
    assert principal.id == "uid-1"
    assert principal.tenant_id == "uid-1"
    assert principal.nickname == "Alice"
    assert principal.authentication.source is AuthenticationSource.WEB_SESSION
    assert principal.authentication.assurance is IdentityAssurance.AUTHENTICATED
    assert principal.authentication.authenticated_at is None
    assert principal.authentication.assurance_verified_at is None


async def test_async_current_user_api_token_fallback(monkeypatch, async_db):
    monkeypatch.delenv("DISABLE_SDK", raising=False)

    def _raise(token):
        raise apps.manager.not_authenticated_exception

    monkeypatch.setattr(apps.manager, "_get_payload", _raise)
    monkeypatch.setattr(APITokenService, "query", lambda s, **kw: _token_rows("uid-owner"))
    monkeypatch.setattr(UserService, "query", lambda s, **kw: [_active_user("uid-owner", "owner@example.com", "Owner")] if kw.get("id") == "uid-owner" else [])
    monkeypatch.setattr(legacy_owner, "_load_personal_owner_tenant_ids", _owner_tenant_ids)

    principal = await async_current_user(_req("Bearer raw-api-token"), async_db)

    assert isinstance(principal, Principal)
    assert principal.id == "uid-owner"
    assert principal.tenant_id == "uid-owner"
    assert principal.authentication.source is AuthenticationSource.SDK_API_TOKEN
    assert principal.authentication.assurance is IdentityAssurance.AUTHENTICATED
    assert principal.authentication.authenticated_at is None


async def test_async_current_user_honors_disable_sdk_for_api_token_fallback(
    monkeypatch,
    async_db,
):
    api_token_queries = []
    monkeypatch.setenv("DISABLE_SDK", "1")
    monkeypatch.setattr(
        apps.manager,
        "_get_payload",
        lambda token: (_ for _ in ()).throw(apps.manager.not_authenticated_exception),
    )
    monkeypatch.setattr(APITokenService, "query", lambda s, **kw: api_token_queries.append(kw) or _token_rows("uid-owner"))

    with pytest.raises(HTTPException):
        await async_current_user(_req("Bearer raw-api-token"), async_db)
    assert api_token_queries == []


async def test_async_current_user_selects_only_the_legacy_personal_owner_tenant(monkeypatch, async_db):
    monkeypatch.setattr(apps.manager, "_get_payload", lambda token: {"sub": "alice@example.com"})
    monkeypatch.setattr(apps, "load_user", lambda email, d: _active_user("uid-1", "alice@example.com", "Alice"))
    monkeypatch.setattr(
        legacy_owner,
        "_load_personal_owner_tenant_ids",
        lambda _session, _user_id: ["uid-1"],
    )

    principal = await async_current_user(_req("Bearer jwt-token"), async_db)

    assert principal.tenant_id == "uid-1"


@pytest.mark.parametrize(
    "owner_tenant_ids",
    [[], ["other-tenant"], ["uid-1", "uid-1"]],
)
async def test_async_current_user_fails_closed_without_one_live_personal_owner_membership(
    monkeypatch,
    async_db,
    owner_tenant_ids,
):
    monkeypatch.setattr(apps.manager, "_get_payload", lambda token: {"sub": "alice@example.com"})
    monkeypatch.setattr(apps, "load_user", lambda email, d: _active_user("uid-1", "alice@example.com", "Alice"))
    monkeypatch.setattr(legacy_owner, "_load_personal_owner_tenant_ids", lambda _session, _user_id: owner_tenant_ids)
    monkeypatch.setattr(APITokenService, "query", lambda s, **kw: [])

    with pytest.raises(Exception):
        await async_current_user(_req("Bearer jwt-token"), async_db)


async def test_async_current_user_fails_closed_for_inactive_platform_user(monkeypatch, async_db):
    inactive = _active_user("uid-1", "alice@example.com", "Alice")
    inactive.is_active = False
    api_token_queries = []
    monkeypatch.setattr(apps.manager, "_get_payload", lambda token: {"sub": "alice@example.com"})
    monkeypatch.setattr(apps, "load_user", lambda email, d: inactive)
    monkeypatch.setattr(legacy_owner, "_load_personal_owner_tenant_ids", _owner_tenant_ids)
    monkeypatch.setattr(APITokenService, "query", lambda s, **kw: api_token_queries.append(kw) or [])

    with pytest.raises(Exception):
        await async_current_user(_req("Bearer jwt-token"), async_db)
    assert api_token_queries == []


async def test_async_current_user_rejects_ambiguous_api_token_ownership(monkeypatch, async_db):
    monkeypatch.delenv("DISABLE_SDK", raising=False)

    def _raise(token):
        raise apps.manager.not_authenticated_exception

    monkeypatch.setattr(apps.manager, "_get_payload", _raise)
    monkeypatch.setattr(
        APITokenService,
        "query",
        lambda s, **kw: _token_rows("uid-owner") + _token_rows("uid-other"),
    )

    with pytest.raises(Exception):
        await async_current_user(_req("Bearer duplicated-api-token"), async_db)


async def test_async_current_user_rejects_unknown_credentials(monkeypatch, async_db):
    monkeypatch.delenv("DISABLE_SDK", raising=False)

    def _raise(token):
        raise apps.manager.not_authenticated_exception

    monkeypatch.setattr(apps.manager, "_get_payload", _raise)
    monkeypatch.setattr(APITokenService, "query", lambda s, **kw: [])

    with pytest.raises(Exception):
        await async_current_user(_req("Bearer bad"), async_db)


async def test_async_current_user_does_not_reinterpret_internal_jwt_errors_as_api_tokens(
    monkeypatch,
    async_db,
):
    monkeypatch.delenv("DISABLE_SDK", raising=False)
    api_token_queries = []

    def _raise_unexpected(token):
        raise RuntimeError("jwt verifier invariant failed")

    monkeypatch.setattr(apps.manager, "_get_payload", _raise_unexpected)
    monkeypatch.setattr(APITokenService, "query", lambda s, **kw: api_token_queries.append(kw) or _token_rows("uid-owner"))

    with pytest.raises(RuntimeError, match="jwt verifier invariant failed"):
        await async_current_user(_req("Bearer raw-api-token"), async_db)
    assert api_token_queries == []


async def test_async_current_user_rejects_missing_authorization(async_db):
    with pytest.raises(HTTPException):  # fastapi_login 的 InvalidCredentialsException 是 401 实例
        await async_current_user(_req(None), async_db)
