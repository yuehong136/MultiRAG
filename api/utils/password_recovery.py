"""Origin and cookie boundaries shared by Python password recovery endpoints."""

import ipaddress
import re
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, Response
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from common.app_config import get_app_config
from common.constants import RetCode

COOKIE = "multirag_py_recovery"
COOKIE_PATH = "/api/v1/auth/password"
RECOVERY_PATHS = frozenset(COOKIE_PATH + suffix for suffix in ("/forgot/captcha", "/forgot/otp", "/forgot/otp/verify", "/reset"))


def origin_identity(value: str) -> tuple[str, str, int]:
    """Require a serialized HTTP origin, never credentials or a URL path."""
    if not value.isascii() or len(value) > 2048 or any(ord(char) <= 32 for char in value):
        raise ValueError("Invalid recovery origin")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment or value != value.strip():
        raise ValueError("Invalid recovery origin")
    port = parsed.port if parsed.port is not None else (443 if parsed.scheme == "https" else 80)
    if port < 1:
        raise ValueError("Invalid recovery origin port")
    return parsed.scheme, parsed.hostname.lower(), port


def check_origin(request: Request) -> str | None:
    """Check before any challenge, proof, database or mail side effect."""
    origins = request.headers.getlist("origin")
    site = request.headers.get("sec-fetch-site", "")
    if site == "cross-site" or len(origins) > 1 or (not origins and site not in {"", "none"}):
        raise HTTPException(403, "Recovery origin denied")
    if not origins:
        return None
    origin = origins[0]
    try:
        candidate = origin_identity(origin)
        own = origin_identity(request.url.scheme + "://" + request.url.netloc)
        extra = get_app_config().authentication.model_extra or {}
        configured = extra.get("password_recovery_origins", [])
        if not isinstance(configured, list) or any(not isinstance(item, str) for item in configured):
            raise ValueError("Invalid recovery origin configuration")
        allowed = {origin_identity(item) for item in configured}
        # Preserve the repository's default loopback Vite -> API consumer.
        # An explicitly configured [] still disables all cross-origin recovery.
        if "password_recovery_origins" not in extra and own[0] == "http" and is_loopback(own[1]):
            allowed.add(("http", own[1], 5173))
        if candidate != own and candidate not in allowed:
            raise ValueError("Origin is not allowed")
    except ValueError as error:
        raise HTTPException(403, "Recovery origin denied") from error
    return origin


def request_token(request: Request) -> str | None:
    """Reject ambiguous/tossed cookies rather than choosing the first or last."""
    values = [part.strip().partition("=")[2] for header in request.headers.getlist("cookie") for part in header.split(";") if part.strip().partition("=")[0].strip() == COOKIE]
    if len(values) != 1 or re.fullmatch(r"[0-9a-f]{64}", values[0]) is None:
        return None
    return values[0]


def is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


def set_token(response: Response, request: Request, token: str) -> None:
    loopback = is_loopback(request.url.hostname or "")
    origin = check_origin(request)
    secure = request.url.scheme == "https" or bool(origin and origin_identity(origin)[0] == "https") or not loopback
    response.set_cookie(COOKIE, token, max_age=300, httponly=True, secure=secure, samesite="strict", path=COOKIE_PATH)


def clear_token(response: Response) -> None:
    response.delete_cookie(COOKIE, path=COOKIE_PATH, httponly=True, samesite="strict")


class RecoveryOriginMiddleware:
    """Keep recovery CORS outside the application's permissive general CORS."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") not in RECOVERY_PATHS:
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        try:
            origin = check_origin(request)
        except HTTPException:
            await JSONResponse({"retcode": RetCode.AUTHENTICATION_ERROR, "retmsg": "Recovery origin denied", "data": False}, status_code=403)(scope, receive, send)
            return

        async def secured_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [(key, value) for key, value in message.get("headers", []) if not key.lower().startswith(b"access-control-") and key.lower() != b"cache-control"]
                headers.append((b"cache-control", b"no-store"))
                if origin:
                    headers.extend([(b"access-control-allow-origin", origin.encode("ascii")), (b"access-control-allow-credentials", b"true"), (b"vary", b"Origin")])
                    if request.method == "OPTIONS":
                        headers.extend(
                            [(b"access-control-allow-methods", b"POST"), (b"access-control-allow-headers", request.headers.get("access-control-request-headers", "content-type").encode("ascii"))]
                        )
                message = {**message, "headers": headers}
            await send(message)

        if request.method == "OPTIONS":
            if request.headers.get("access-control-request-method") != "POST":
                await Response(status_code=403)(scope, receive, secured_send)
            else:
                await Response(status_code=204)(scope, receive, secured_send)
            return
        await self.app(scope, receive, secured_send)
