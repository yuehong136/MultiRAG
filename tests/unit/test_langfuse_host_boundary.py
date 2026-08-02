"""Langfuse host 的 SSRF 出网边界（validate_outbound_url + RESTful 端点）。

校验器：scheme/私网/环回/link-local（云 metadata）/保留段拒绝，白名单显式放行；
路由层：写入与读取路径都在任何出网探测（Langfuse 构造/auth_check）之前拦截。
"""

import socket
import sys

import pytest

from api.apps.services import langfuse_api_service
from api.utils.web_utils import validate_outbound_url


def _restful_route_module():
    return sys.modules["api.apps.restful_apis.langfuse"]


# ---------------------------------------------------------------------------
# 校验器（无网络：字面量 IP 直判，域名解析打桩）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:3000",
        "http://10.0.0.8",
        "http://192.168.188.195:8123",
        "http://169.254.169.254/latest/meta-data/",  # 云 metadata
        "http://[::1]:3000",
        "http://0.0.0.0",
    ],
)
def test_rejects_private_and_reserved_literals(url):
    with pytest.raises(ValueError, match="private or reserved"):
        validate_outbound_url(url)


def test_rejects_non_http_scheme_and_missing_host():
    with pytest.raises(ValueError, match="scheme must be http or https"):
        validate_outbound_url("ftp://example.com")
    with pytest.raises(ValueError, match="scheme must be http or https"):
        validate_outbound_url("cloud.langfuse.com")


def test_accepts_public_literal_ip():
    validate_outbound_url("https://93.184.216.34")


def test_hostname_resolution_blocks_any_private_answer(monkeypatch):
    def _fake_gai(host, port, proto=None):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 443)),  # rebinding 面
        ]

    monkeypatch.setattr(socket, "getaddrinfo", _fake_gai)
    with pytest.raises(ValueError, match="resolves to a private or reserved"):
        validate_outbound_url("https://evil.example")


def test_hostname_resolution_passes_public_answers(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, proto=None: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))])
    validate_outbound_url("https://cloud.langfuse.com")


def test_hostname_resolution_failure_is_rejected(monkeypatch):
    def _boom(host, port, proto=None):
        raise socket.gaierror("nx")

    monkeypatch.setattr(socket, "getaddrinfo", _boom)
    with pytest.raises(ValueError, match="Failed to resolve"):
        validate_outbound_url("https://no-such-host.example")


def test_allowlist_short_circuits_without_dns(monkeypatch):
    def _no_dns(*_a, **_k):
        raise AssertionError("allowlist 命中不得触发 DNS")

    monkeypatch.setattr(socket, "getaddrinfo", _no_dns)
    validate_outbound_url("http://langfuse.internal:3000", ["Langfuse.Internal"])  # 大小写不敏感

    with pytest.raises(ValueError, match="not in the configured allowlist"):
        validate_outbound_url("http://other.internal", ["langfuse.internal"])


# ---------------------------------------------------------------------------
# 路由层（出网探测前拦截；Langfuse 构造即炸证明零出网）
# ---------------------------------------------------------------------------


class _BombLangfuse:
    def __init__(self, *args, **kwargs):
        raise AssertionError("被拒 host 不得构造 Langfuse 客户端")


_KEYS = {"secret_key": "sk-x", "public_key": "pk-x", "host": "http://169.254.169.254"}


def test_restful_set_api_key_blocks_private_host_before_probe(client, monkeypatch):
    monkeypatch.setattr(langfuse_api_service, "Langfuse", _BombLangfuse)

    resp = client.post("/api/v1/langfuse/api-key", json=_KEYS)

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] != 0
    assert "Invalid Langfuse host" in body["message"]


def test_langfuse_restful_route_replaces_removed_legacy_route(client):
    schema = client.app.openapi()
    module = _restful_route_module()

    assert schema["paths"]["/api/v1/langfuse/api-key"]["post"].get("deprecated") is not True
    assert "/v1/langfuse/api_key" not in schema["paths"]
    assert not hasattr(module, "legacy")


def test_set_api_key_allowlisted_host_passes_boundary(client, monkeypatch):
    import types

    monkeypatch.setattr(
        langfuse_api_service,
        "get_app_config",
        lambda: types.SimpleNamespace(observability=types.SimpleNamespace(langfuse_allowed_hosts=["langfuse.internal"])),
    )

    class _OkLangfuse:
        def __init__(self, *args, **kwargs):
            pass

        def auth_check(self):
            return True

    from api.db.services.langfuse_service import TenantLangfuseService

    monkeypatch.setattr(langfuse_api_service, "Langfuse", _OkLangfuse)
    monkeypatch.setattr(TenantLangfuseService, "filter_by_tenant", classmethod(lambda cls, s, tenant_id: None))
    monkeypatch.setattr(TenantLangfuseService, "save", classmethod(lambda cls, s, **kw: None))

    resp = client.post("/api/v1/langfuse/api-key", json={**_KEYS, "host": "http://langfuse.internal:3000"})

    assert resp.status_code == 200
    assert resp.json()["retcode"] == 0


def test_get_api_key_blocks_stored_private_host(client, monkeypatch):
    from api.db.services.langfuse_service import TenantLangfuseService

    monkeypatch.setattr(langfuse_api_service, "Langfuse", _BombLangfuse)
    monkeypatch.setattr(TenantLangfuseService, "filter_by_tenant_with_info", classmethod(lambda cls, s, tenant_id: dict(_KEYS)))

    resp = client.get("/api/v1/langfuse/api-key")

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] != 0
    assert "Invalid Langfuse host" in body["message"]
