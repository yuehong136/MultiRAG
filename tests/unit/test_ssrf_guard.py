import logging
import socket

import pytest

from common import ssrf_guard


def _addrinfo(ip: str) -> list[tuple[int, int, int, str, tuple[str, int]]]:
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, 0))]


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/file.txt",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "http:///missing-host",
    ],
)
def test_assert_url_is_safe_rejects_invalid_url_without_dns(url, monkeypatch):
    resolver = lambda *_args, **_kwargs: pytest.fail("invalid URLs must be rejected before DNS")
    monkeypatch.setattr(ssrf_guard, "_ORIGINAL_GETADDRINFO", resolver)

    with pytest.raises(ValueError):
        ssrf_guard.assert_url_is_safe(url)


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",
        "10.0.0.1",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",
        "240.0.0.1",
        "::1",
        "::ffff:127.0.0.1",
        "::ffff:192.168.1.1",
    ],
)
def test_assert_url_is_safe_rejects_non_public_addresses(ip, monkeypatch):
    monkeypatch.setattr(ssrf_guard, "_ORIGINAL_GETADDRINFO", lambda *_args, **_kwargs: _addrinfo(ip))

    with pytest.raises(ValueError, match="non-public"):
        ssrf_guard.assert_url_is_safe("https://example.com/resource")


def test_assert_url_is_safe_rejects_mixed_public_and_private_dns(monkeypatch):
    monkeypatch.setattr(
        ssrf_guard,
        "_ORIGINAL_GETADDRINFO",
        lambda *_args, **_kwargs: _addrinfo("93.184.216.34") + _addrinfo("10.0.0.1"),
    )

    with pytest.raises(ValueError, match="non-public"):
        ssrf_guard.assert_url_is_safe("https://example.com/resource")


def test_assert_url_is_safe_returns_first_public_address(monkeypatch):
    monkeypatch.setattr(
        ssrf_guard,
        "_ORIGINAL_GETADDRINFO",
        lambda *_args, **_kwargs: _addrinfo("93.184.216.34") + _addrinfo("2606:2800:220:1:248:1893:25c8:1946"),
    )

    assert ssrf_guard.assert_url_is_safe("https://example.com/resource") == ("example.com", "93.184.216.34")


def test_assert_url_is_safe_redacts_credentials_and_query_from_logs(monkeypatch, caplog):
    monkeypatch.setattr(ssrf_guard, "_ORIGINAL_GETADDRINFO", lambda *_args, **_kwargs: _addrinfo("127.0.0.1"))

    with caplog.at_level(logging.WARNING), pytest.raises(ValueError):
        ssrf_guard.assert_url_is_safe("http://alice:secret@example.com/path?token=sensitive")

    assert "secret" not in caplog.text
    assert "sensitive" not in caplog.text


def test_pin_dns_prevents_second_resolution_and_restores_resolver(monkeypatch):
    calls: list[str] = []

    def resolver(host, *_args, **_kwargs):
        calls.append(host)
        return _addrinfo("127.0.0.1")

    monkeypatch.setattr(ssrf_guard, "_ORIGINAL_GETADDRINFO", resolver)

    with ssrf_guard.pin_dns("example.com", "93.184.216.34"):
        pinned = socket.getaddrinfo("example.com", 443)

    restored = socket.getaddrinfo("example.com", 443)

    assert pinned == [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443))]
    assert restored == _addrinfo("127.0.0.1")
    assert calls == ["example.com"]
