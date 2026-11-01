"""Reject alternate origin/cookie representations at the shared boundary."""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Request, Response

from api.utils import password_recovery as boundary


def request(*headers: tuple[str, str], scheme: str = "http") -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": boundary.COOKIE_PATH + "/reset",
            "scheme": scheme,
            "server": ("localhost", 8000),
            "headers": [(b"host", b"localhost:8000"), *((k.encode(), v.encode()) for k, v in headers)],
        }
    )


@pytest.mark.parametrize(
    "origin",
    [
        "null",
        "http://attacker.test",
        "http://localhost:5173",
        "http://localhost:8000/",
        "http://user@localhost:8000",
        "http://localhost:8000?x=1",
        "http://localhost:8000#x",
        " http://localhost:8000",
        "http://localhost:0",
        "http://localhost:99999",
    ],
)
def test_untrusted_origin_is_rejected(origin: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(boundary, "get_app_config", lambda: SimpleNamespace(authentication=SimpleNamespace(model_extra={"password_recovery_origins": []})))
    with pytest.raises(HTTPException) as denied:
        boundary.check_origin(request(("origin", origin)))
    assert denied.value.status_code == 403


def test_explicit_same_site_cross_port_and_same_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(boundary, "get_app_config", lambda: SimpleNamespace(authentication=SimpleNamespace(model_extra={"password_recovery_origins": ["http://localhost:5173"]})))
    assert boundary.check_origin(request(("origin", "http://localhost:5173"), ("sec-fetch-site", "same-site"))) == "http://localhost:5173"
    assert boundary.check_origin(request(("origin", "http://localhost:8000"))) == "http://localhost:8000"
    for headers in [
        (("origin", "http://localhost:5173"), ("sec-fetch-site", "cross-site")),
        (("origin", "http://localhost:8000"), ("origin", "http://localhost:8000")),
        (("sec-fetch-site", "same-site"),),
    ]:
        with pytest.raises(HTTPException):
            boundary.check_origin(request(*headers))


@pytest.mark.parametrize(
    "cookie",
    [
        "",
        boundary.COOKIE + "=1",
        boundary.COOKIE + "=" + "a" * 63,
        boundary.COOKIE + "=" + "g" * 64,
        boundary.COOKIE + "=" + "a" * 64 + "; " + boundary.COOKIE + "=" + "b" * 64,
        boundary.COOKIE + "=" + "a" * 64 + "; " + boundary.COOKIE + " =" + "b" * 64,
    ],
)
def test_cookie_is_exact_and_unambiguous(cookie: str) -> None:
    assert boundary.request_token(request(("cookie", cookie))) is None


def test_host_only_httponly_cookie_and_https(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(boundary, "get_app_config", lambda: SimpleNamespace(authentication=SimpleNamespace(model_extra={})))
    token = "a" * 64
    assert boundary.request_token(request(("cookie", boundary.COOKIE + "=" + token))) == token
    for scheme, secure in [("http", False), ("https", True)]:
        response = Response()
        boundary.set_token(response, request(scheme=scheme), token)
        value = response.headers["set-cookie"]
        assert "HttpOnly" in value and "SameSite=strict" in value and "Max-Age=300" in value and "Domain=" not in value
        assert ("Secure" in value) is secure


def test_default_loopback_vite_origin_remains_usable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(boundary, "get_app_config", lambda: SimpleNamespace(authentication=SimpleNamespace(model_extra={})))
    assert boundary.check_origin(request(("origin", "http://localhost:5173"))) == "http://localhost:5173"
    with pytest.raises(HTTPException):
        boundary.check_origin(request(("origin", "http://localhost:5174")))
