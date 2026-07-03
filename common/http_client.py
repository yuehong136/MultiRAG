# Copyright 2025 The MultiRAG Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import asyncio
import logging
import os
import time
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx

logger = logging.getLogger(__name__)

# Default knobs; keep conservative to avoid unexpected behavioural changes.
DEFAULT_TIMEOUT = float(os.environ.get("HTTP_CLIENT_TIMEOUT", "15"))
# Align with requests default: follow redirects with a max of 30 unless overridden.
DEFAULT_FOLLOW_REDIRECTS = bool(
    int(os.environ.get("HTTP_CLIENT_FOLLOW_REDIRECTS", "1"))
)
DEFAULT_MAX_REDIRECTS = int(os.environ.get("HTTP_CLIENT_MAX_REDIRECTS", "30"))
DEFAULT_MAX_RETRIES = int(os.environ.get("HTTP_CLIENT_MAX_RETRIES", "2"))
DEFAULT_BACKOFF_FACTOR = float(os.environ.get("HTTP_CLIENT_BACKOFF_FACTOR", "0.5"))
DEFAULT_PROXY = os.environ.get("HTTP_CLIENT_PROXY")
DEFAULT_USER_AGENT = os.environ.get("HTTP_CLIENT_USER_AGENT", "multirag-http-client")


def _clean_headers(
    headers: dict[str, str] | None, auth_token: str | None = None
) -> dict[str, str] | None:
    merged_headers: dict[str, str] = {}
    if DEFAULT_USER_AGENT:
        merged_headers["User-Agent"] = DEFAULT_USER_AGENT
    if auth_token:
        merged_headers["Authorization"] = auth_token
    if headers is None:
        return merged_headers or None
    merged_headers.update({str(k): str(v) for k, v in headers.items() if v is not None})
    return merged_headers or None


def _get_delay(backoff_factor: float, attempt: int) -> float:
    return backoff_factor * (2**attempt)


# List of sensitive parameters to redact from URLs before logging
_SENSITIVE_QUERY_KEYS = {"client_secret", "secret", "code", "access_token", "refresh_token", "password", "token", "app_secret"}


def _redact_sensitive_url_params(url: str) -> str:
    """
    Return a version of the URL that is safe to log.

    We intentionally drop query parameters and userinfo to avoid leaking
    credentials or tokens via logs. Only scheme, host, port and path
    are preserved.
    """
    try:
        parsed = urlparse(url)
        # Remove any potential userinfo (username:password@)
        netloc = parsed.hostname or ""
        if parsed.port:
            netloc = f"{netloc}:{parsed.port}"
        # Reconstruct URL without query, params, fragment, or userinfo.
        safe_url = urlunparse(
            (
                parsed.scheme,
                netloc,
                parsed.path,
                "",  # params
                "",  # query
                "",  # fragment
            )
        )
        return safe_url
    except Exception:
        # If parsing fails, fall back to omitting the URL entirely.
        return "<redacted-url>"


def _is_sensitive_url(url: str) -> bool:
    """Return True if URL contains sensitive OAuth-related paths."""
    sensitive_patterns = [
        "/oauth/",
        "/token",
        "/access_token",
        "/authorize",
        "/login/oauth",
        "/authen/",
    ]
    url_lower = url.lower()
    return any(pattern in url_lower for pattern in sensitive_patterns)


async def async_request(
    method: str,
    url: str,
    *,
    request_timeout: float | httpx.Timeout | None = None,
    follow_redirects: bool | None = None,
    max_redirects: int | None = None,
    headers: dict[str, str] | None = None,
    auth_token: str | None = None,
    retries: int | None = None,
    backoff_factor: float | None = None,
    proxy: Any = None,
    **kwargs: Any,
) -> httpx.Response:
    """Lightweight async HTTP wrapper using httpx.AsyncClient with safe defaults."""
    timeout = request_timeout if request_timeout is not None else DEFAULT_TIMEOUT
    follow_redirects = (
        DEFAULT_FOLLOW_REDIRECTS if follow_redirects is None else follow_redirects
    )
    max_redirects = DEFAULT_MAX_REDIRECTS if max_redirects is None else max_redirects
    retries = DEFAULT_MAX_RETRIES if retries is None else max(retries, 0)
    backoff_factor = (
        DEFAULT_BACKOFF_FACTOR if backoff_factor is None else backoff_factor
    )
    headers = _clean_headers(headers, auth_token=auth_token)
    proxy = DEFAULT_PROXY if proxy is None else proxy

    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=follow_redirects,
        max_redirects=max_redirects,
        proxy=proxy,
    ) as client:
        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            try:
                start = time.monotonic()
                response = await client.request(
                    method=method, url=url, headers=headers, **kwargs
                )
                duration = time.monotonic() - start
                if not _is_sensitive_url(url):
                    log_url = _redact_sensitive_url_params(url)
                    logger.debug(
                        f"async_request {method} {_redact_sensitive_url_params(url)} -> {response.status_code} in {duration:.3f}s"
                    )
                return response
            except httpx.RequestError as exc:
                last_exc = exc
                if attempt >= retries:
                    if not _is_sensitive_url(url):
                        log_url = _redact_sensitive_url_params(url)
                        logger.warning(f"async_request exhausted retries for {method} {log_url}")
                    raise
                delay = _get_delay(backoff_factor, attempt)
                if not _is_sensitive_url(url):
                    log_url = _redact_sensitive_url_params(url)
                    logger.warning(
                        f"async_request attempt {attempt + 1}/{retries + 1} failed for {method} {log_url}; retrying in {delay:.2f}s"
                    )
                await asyncio.sleep(delay)
        raise last_exc  # pragma: no cover


def sync_request(
    method: str,
    url: str,
    *,
    timeout: float | httpx.Timeout | None = None,
    follow_redirects: bool | None = None,
    max_redirects: int | None = None,
    headers: dict[str, str] | None = None,
    auth_token: str | None = None,
    retries: int | None = None,
    backoff_factor: float | None = None,
    proxy: Any = None,
    **kwargs: Any,
) -> httpx.Response:
    """Synchronous counterpart to async_request, for CLI/tests or sync contexts."""
    timeout = timeout if timeout is not None else DEFAULT_TIMEOUT
    follow_redirects = (
        DEFAULT_FOLLOW_REDIRECTS if follow_redirects is None else follow_redirects
    )
    max_redirects = DEFAULT_MAX_REDIRECTS if max_redirects is None else max_redirects
    retries = DEFAULT_MAX_RETRIES if retries is None else max(retries, 0)
    backoff_factor = (
        DEFAULT_BACKOFF_FACTOR if backoff_factor is None else backoff_factor
    )
    headers = _clean_headers(headers, auth_token=auth_token)
    proxy = DEFAULT_PROXY if proxy is None else proxy

    with httpx.Client(
        timeout=timeout,
        follow_redirects=follow_redirects,
        max_redirects=max_redirects,
        proxy=proxy,
    ) as client:
        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            try:
                start = time.monotonic()
                response = client.request(
                    method=method, url=url, headers=headers, **kwargs
                )
                duration = time.monotonic() - start
                if not _is_sensitive_url(url):
                    logger.debug(
                        f"sync_request {method} {_redact_sensitive_url_params(url)} -> {response.status_code} in {duration:.3f}s"
                    )
                return response
            except httpx.RequestError as exc:
                last_exc = exc
                if attempt >= retries:
                    if not _is_sensitive_url(url):
                        log_url = _redact_sensitive_url_params(url)
                        logger.warning(f"sync_request exhausted retries for {method} {log_url}")
                    raise
                delay = _get_delay(backoff_factor, attempt)
                if not _is_sensitive_url(url):
                    log_url = _redact_sensitive_url_params(url)
                    logger.warning(
                        f"sync_request attempt {attempt + 1}/{retries + 1} failed for {method} {log_url}; retrying in {delay:.2f}s"
                    )
                time.sleep(delay)
        raise last_exc  # pragma: no cover


__all__ = [
    "DEFAULT_BACKOFF_FACTOR",
    "DEFAULT_FOLLOW_REDIRECTS",
    "DEFAULT_MAX_REDIRECTS",
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_PROXY",
    "DEFAULT_TIMEOUT",
    "DEFAULT_USER_AGENT",
    "async_request",
    "sync_request",
]
