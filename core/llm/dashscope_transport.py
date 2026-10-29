"""Request-local native DashScope endpoints shared by its adapters."""

from urllib.parse import urlsplit, urlunsplit


def native_http_api_url(base_url: str | None) -> str | None:
    """Map known compatible roots; explicit custom endpoints must use the native API."""
    normalized = (base_url or "").strip().rstrip("/")
    if not normalized:
        return None
    error = "Tongyi-Qianwen base_url must be a native /api/v1 URL or a supported DashScope root"
    try:
        parsed = urlsplit(normalized)
        # Accessing port also rejects malformed or out-of-range ports.
        _ = parsed.port
    except ValueError:
        raise ValueError(error) from None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or any(c.isspace() for c in normalized)
    ):
        # Never include the configured URL: userinfo and queries may contain credentials.
        raise ValueError(error)
    path = parsed.path.rstrip("/")
    if path.endswith("/api/v1"):
        return urlunsplit(parsed._replace(path=path))
    if parsed.hostname in {"dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com"} and path in {"", "/compatible-mode/v1"}:
        return urlunsplit(parsed._replace(path="/api/v1"))
    raise ValueError(error)


def native_websocket_api_url(base_url: str | None) -> str:
    """Use the same selected host for the SDK's native speech protocol."""
    parsed = urlsplit(native_http_api_url(base_url) or "https://dashscope.aliyuncs.com/api/v1")
    return urlunsplit(parsed._replace(scheme="wss" if parsed.scheme == "https" else "ws", path=parsed.path.removesuffix("/api/v1") + "/api-ws/v1/inference"))
