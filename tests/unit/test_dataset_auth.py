"""
统一鉴权依赖 current_tenant_id 单元测试（Round 1，对标 ragflow `_load_user`）。
覆盖：web 会话 JWT 路径、SDK API-key fallback、无效凭证、缺失 Authorization。
"""

import types

import pytest
from starlette.requests import Request

import api.apps as apps
from api.utils import api_utils
from api.utils.api_utils import SDKAuthError, current_tenant_id


def _req(auth=None):
    """构造真实 starlette Request（beartype 会校验类型）。"""
    headers = []
    if auth is not None:
        headers.append((b"authorization", auth.encode()))
    return Request({"type": "http", "headers": headers})


def test_auth_missing_authorization(db):
    with pytest.raises(SDKAuthError):
        current_tenant_id(_req(None), db)


def test_auth_web_jwt_path(monkeypatch, db):
    monkeypatch.delenv("DISABLE_SDK", raising=False)
    monkeypatch.setattr(apps.manager, "_get_payload", lambda token: {"sub": "alice@example.com"})
    monkeypatch.setattr(apps, "load_user", lambda email, d: types.SimpleNamespace(id="tenant-web"))

    assert current_tenant_id(_req("Bearer jwt-token"), db) == "tenant-web"


def test_auth_api_key_fallback(monkeypatch, db):
    monkeypatch.delenv("DISABLE_SDK", raising=False)

    def _raise(token):
        raise ValueError("not a jwt")

    monkeypatch.setattr(apps.manager, "_get_payload", _raise)
    monkeypatch.setattr(api_utils.APIToken, "query", lambda d, **kw: [types.SimpleNamespace(tenant_id="tenant-sdk")])

    assert current_tenant_id(_req("Bearer api-key"), db) == "tenant-sdk"


def test_auth_invalid_credentials(monkeypatch, db):
    monkeypatch.delenv("DISABLE_SDK", raising=False)

    def _raise(token):
        raise ValueError("not a jwt")

    monkeypatch.setattr(apps.manager, "_get_payload", _raise)
    monkeypatch.setattr(api_utils.APIToken, "query", lambda d, **kw: [])

    with pytest.raises(SDKAuthError):
        current_tenant_id(_req("Bearer bad"), db)


def test_auth_disable_sdk_blocks_api_key(monkeypatch, db):
    """DISABLE_SDK 只挡 API-key 通道（不影响 web JWT）。"""
    monkeypatch.setenv("DISABLE_SDK", "1")

    def _raise(token):
        raise ValueError("not a jwt")

    monkeypatch.setattr(apps.manager, "_get_payload", _raise)
    monkeypatch.setattr(api_utils.APIToken, "query", lambda d, **kw: [types.SimpleNamespace(tenant_id="tenant-sdk")])

    with pytest.raises(SDKAuthError):
        current_tenant_id(_req("Bearer api-key"), db)
