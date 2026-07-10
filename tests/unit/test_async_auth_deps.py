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
    monkeypatch.setattr(apps, "load_user", lambda email, d: types.SimpleNamespace(id="uid-1", email="alice@example.com", nickname="Alice"))

    principal = await async_current_user(_req("Bearer jwt-token"), async_db)

    assert principal == Principal(id="uid-1", email="alice@example.com", nickname="Alice")


async def test_async_current_user_api_token_fallback(monkeypatch, async_db):
    def _raise(token):
        raise ValueError("not a jwt")

    monkeypatch.setattr(apps.manager, "_get_payload", _raise)
    monkeypatch.setattr(APITokenService, "query", lambda s, **kw: _token_rows("uid-owner"))
    monkeypatch.setattr(UserService, "query", lambda s, **kw: [types.SimpleNamespace(id="uid-owner", email="owner@example.com", nickname="Owner")] if kw.get("id") == "uid-owner" else [])

    principal = await async_current_user(_req("Bearer raw-api-token"), async_db)

    assert principal == Principal(id="uid-owner", email="owner@example.com", nickname="Owner")


async def test_async_current_user_rejects_unknown_credentials(monkeypatch, async_db):
    def _raise(token):
        raise ValueError("not a jwt")

    monkeypatch.setattr(apps.manager, "_get_payload", _raise)
    monkeypatch.setattr(APITokenService, "query", lambda s, **kw: [])

    with pytest.raises(Exception):
        await async_current_user(_req("Bearer bad"), async_db)


async def test_async_current_user_rejects_missing_authorization(async_db):
    with pytest.raises(HTTPException):  # fastapi_login 的 InvalidCredentialsException 是 401 实例
        await async_current_user(_req(None), async_db)
