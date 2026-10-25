"""Public-image transport rejects unsafe destinations without reading bytes."""

import asyncio
import base64
import socket
from collections.abc import AsyncIterator, Callable
from typing import Any
from unittest.mock import Mock

import httpx
import pytest

from common import misc_utils, ssrf_guard


class Body(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes], *, stall: bool = False) -> None:
        self.chunks = chunks
        self.stall = stall
        self.closed = False
        self.reads = 0
        self.started = asyncio.Event()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        self.started.set()
        if self.stall:
            await asyncio.Event().wait()
        for chunk in self.chunks:
            self.reads += 1
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


def public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ssrf_guard.socket, "getaddrinfo", lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 0))])


def transport(monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]) -> Mock:
    factory = Mock(side_effect=lambda **kwargs: httpx.MockTransport(handler))
    monkeypatch.setattr(misc_utils.httpx, "AsyncHTTPTransport", factory)
    monkeypatch.delenv("HTTP_CLIENT_PROXY", raising=False)
    return factory


@pytest.mark.parametrize(
    "url",
    [
        None,
        "",
        "  ",
        "file:///etc/passwd",
        "http://[invalid",
        "http://127.0.0.1/x",
        "http://169.254.169.254/x",
        "http://[::1]/x",
        "http://[::ffff:127.0.0.1]/x",
        "https://secret:token@public.example/x",
    ],
)
async def test_unsafe_url_never_opens_transport(monkeypatch: pytest.MonkeyPatch, url: str | None) -> None:
    factory = transport(monkeypatch, lambda _: pytest.fail("unsafe request"))
    assert await misc_utils.download_img(url) == ""
    factory.assert_not_called()


async def test_mixed_dns_answers_block_before_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ssrf_guard.socket, "getaddrinfo", lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0)) for ip in ["8.8.8.8", "10.0.0.1"]])
    factory = transport(monkeypatch, lambda _: pytest.fail("mixed DNS request"))
    assert await misc_utils.download_img("https://avatar.example/x") == ""
    factory.assert_not_called()


async def test_each_redirect_is_pinned_with_original_host_and_tls_name(monkeypatch: pytest.MonkeyPatch) -> None:
    public_dns(monkeypatch)
    requests: list[httpx.Request] = []
    bodies = [Body([]), Body([]), Body([b"image", b" bytes"])]

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.host == "8.8.8.8"
        assert request.headers["host"] == ("avatar.example:8443" if len(requests) == 1 else "cdn.example")
        assert request.extensions["sni_hostname"] == ("avatar.example" if len(requests) == 1 else "cdn.example")
        if len(requests) == 1:
            return httpx.Response(302, headers={"location": "https://cdn.example/image"}, stream=bodies[0])
        if len(requests) == 2:
            return httpx.Response(307, headers={"location": "next"}, stream=bodies[1])
        assert request.url.path == "/next"
        return httpx.Response(200, headers={"content-type": "image/png; charset=binary"}, stream=bodies[2])

    factory = transport(monkeypatch, handle)
    assert await misc_utils.download_img("https://avatar.example:8443/start?token=secret") == "data:image/png;base64," + base64.b64encode(b"image bytes").decode()
    assert all(body.closed for body in bodies)
    assert [body.reads for body in bodies] == [0, 0, 2]
    assert factory.call_count == 3
    assert all(call.kwargs == {"trust_env": False, "retries": 0} for call in factory.call_args_list)


async def test_redirect_private_target_is_not_contacted(monkeypatch: pytest.MonkeyPatch) -> None:
    real_assert = ssrf_guard.assert_url_is_safe
    monkeypatch.setattr(misc_utils, "assert_url_is_safe", lambda url: real_assert(url) if "127.0.0.1" in url else ("avatar.example", "8.8.8.8"))
    body = Body([])
    factory = transport(monkeypatch, lambda _: httpx.Response(302, headers={"location": "http://127.0.0.1/admin"}, stream=body))
    assert await misc_utils.download_img("https://avatar.example/x") == ""
    assert body.closed and factory.call_count == 1


@pytest.mark.parametrize("case", ["redirect_limit", "redirect_missing", "status", "type", "length", "stream_size", "empty"])
async def test_response_failures_and_bounds_close_every_body(monkeypatch: pytest.MonkeyPatch, case: str) -> None:
    public_dns(monkeypatch)
    monkeypatch.setattr(misc_utils, "_OAUTH_AVATAR_MAX_REDIRECTS", 1)
    monkeypatch.setattr(misc_utils, "_OAUTH_AVATAR_MAX_BYTES", 4)
    bodies: list[Body] = []

    def handle(request: httpx.Request) -> httpx.Response:
        body = Body([b"1234", b"5", b"unread"] if case == "stream_size" else [])
        bodies.append(body)
        status, headers = 200, {"content-type": "image/png"}
        if case.startswith("redirect"):
            status = 302
            if case == "redirect_limit":
                headers["location"] = "/again"
        elif case == "status":
            status = 404
        elif case == "type":
            headers["content-type"] = "text/html"
        elif case == "length":
            headers["content-length"] = "5"
        return httpx.Response(status, headers=headers, stream=body)

    transport(monkeypatch, handle)
    result = await misc_utils.download_img("https://avatar.example/x")
    assert result == ("data:image/png;base64," if case == "empty" else "")
    assert all(body.closed for body in bodies)
    assert len(bodies) == (2 if case == "redirect_limit" else 1)
    assert sum(body.reads for body in bodies) == (2 if case == "stream_size" else 0)


async def test_wall_timeout_and_cancellation_close_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    public_dns(monkeypatch)
    body = Body([], stall=True)
    transport(monkeypatch, lambda _: httpx.Response(200, stream=body))
    monkeypatch.setenv("HTTP_CLIENT_TIMEOUT", "0.02")
    assert await misc_utils.download_img("https://avatar.example/x") == "" and body.closed
    body.closed = False
    monkeypatch.setenv("HTTP_CLIENT_TIMEOUT", "2")
    task = asyncio.create_task(misc_utils.download_img("https://avatar.example/x"))
    body.started.clear()
    await body.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert body.closed


async def test_timeout_covers_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    async def slow_dns(*args: Any) -> None:
        await asyncio.Event().wait()

    transport(monkeypatch, lambda _: pytest.fail("DNS did not complete"))
    monkeypatch.setattr(misc_utils.asyncio, "to_thread", slow_dns)
    monkeypatch.setenv("HTTP_CLIENT_TIMEOUT", "0.01")
    assert await misc_utils.download_img("https://avatar.example/x") == ""


@pytest.mark.parametrize("timeout", ["0", "-1", "nan", "inf", "invalid"])
async def test_invalid_timeout_never_connects(monkeypatch: pytest.MonkeyPatch, timeout: str) -> None:
    factory = transport(monkeypatch, lambda _: pytest.fail("invalid timeout"))
    monkeypatch.setenv("HTTP_CLIENT_TIMEOUT", timeout)
    assert await misc_utils.download_img("https://avatar.example/x") == ""
    factory.assert_not_called()


async def test_configured_proxy_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    factory = transport(monkeypatch, lambda _: pytest.fail("proxy bypass"))
    monkeypatch.setenv("HTTP_CLIENT_PROXY", "http://private-proxy:8080")
    assert await misc_utils.download_img("https://avatar.example/x") == ""
    factory.assert_not_called()


async def test_failures_do_not_log_url_secrets(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    public_dns(monkeypatch)

    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("private-path?token=private-token", request=request)

    transport(monkeypatch, fail)
    assert await misc_utils.download_img("https://avatar.example/private-path?token=private-token") == ""
    assert "download_img" in caplog.text
    assert "private-token" not in caplog.text and "private-path" not in caplog.text


async def test_concurrent_same_host_uses_request_scoped_pins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(misc_utils, "assert_url_is_safe", lambda url: ("avatar.example", "8.8.8.8" if url.endswith("/one") else "1.1.1.1"))
    monkeypatch.delenv("HTTP_CLIENT_PROXY", raising=False)
    both_connected = asyncio.Event()
    seen: list[str] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if len(seen) == 2:
            both_connected.set()
        await both_connected.wait()
        assert request.url.host == ("8.8.8.8" if request.url.path == "/one" else "1.1.1.1")
        assert request.headers["host"] == "avatar.example" and request.extensions["sni_hostname"] == "avatar.example"
        return httpx.Response(200, content=b"image")

    monkeypatch.setattr(misc_utils.httpx, "AsyncHTTPTransport", lambda **kwargs: httpx.MockTransport(handle))
    results = await asyncio.gather(*(misc_utils.download_img("https://avatar.example/" + suffix) for suffix in ("one", "two")))
    assert results == ["data:image/jpeg;base64,aW1hZ2U="] * 2


async def test_real_httpx_httpcore_transport_binds_tcp_and_tls(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    import logging
    import ssl

    from httpcore._backends.auto import AutoBackend
    from httpcore._backends.mock import AsyncMockStream

    public_dns(monkeypatch)
    monkeypatch.delenv("HTTP_CLIENT_PROXY", raising=False)
    caplog.set_level(logging.INFO)
    writes: list[bytes] = []
    tls_names: list[str | None] = []

    class Stream(AsyncMockStream):
        async def write(self, buffer: bytes, timeout: float | None = None) -> None:
            writes.append(buffer)

        async def start_tls(self, ssl_context: ssl.SSLContext, server_hostname: str | None = None, timeout: float | None = None) -> "Stream":
            assert ssl_context.verify_mode == ssl.CERT_REQUIRED and ssl_context.check_hostname
            tls_names.append(server_hostname)
            return self

    stream = Stream([b"HTTP/1.1 200 OK\r\nContent-Type: image/png\r\nContent-Length: 5\r\n\r\nimage"])

    async def connect(self: AutoBackend, host: str, port: int, **kwargs: Any) -> Stream:
        assert host == "8.8.8.8" and port == 8443
        return stream

    monkeypatch.setattr(AutoBackend, "connect_tcp", connect)
    assert await misc_utils.download_img("https://avatar.example:8443/private-path?token=private-token") == "data:image/png;base64,aW1hZ2U="
    assert tls_names == ["avatar.example"] and stream._closed
    assert b"Host: avatar.example:8443\r\n" in b"".join(writes)
    assert "private-token" not in caplog.text and "private-path" not in caplog.text
