import asyncio
import base64
import functools
import hashlib
import logging
import math
import os
import subprocess
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin

import httpx

from common.ssrf_guard import assert_url_is_safe

logger = logging.getLogger(__name__)
_OAUTH_AVATAR_MAX_BYTES = 5 * 1024 * 1024
_OAUTH_AVATAR_MAX_REDIRECTS = 5
_REDIRECT_STATUS = frozenset({301, 302, 303, 307, 308})


def get_uuid():
    return uuid.uuid1().hex


async def download_img(url: str | None) -> str:
    """Read a public image with per-hop DNS pinning and a bounded lifetime.

    The transport connects to the validated IP, retaining the original Host and
    TLS server name. No process-global DNS pin can affect a concurrent request.
    Proxies cannot enforce that destination binding, so configured proxies fail
    closed; ambient proxy variables are never inherited.
    """
    if not isinstance(url, str) or not url.strip():
        return ""
    try:
        request_timeout = float(os.environ.get("HTTP_CLIENT_TIMEOUT", "15"))
        if not math.isfinite(request_timeout) or request_timeout <= 0:
            raise ValueError("Invalid timeout")
        if os.environ.get("HTTP_CLIENT_PROXY"):
            logger.warning("download_img disabled: a proxy cannot guarantee DNS pinning")
            return ""
        async with asyncio.timeout(request_timeout):
            return await _download_public_image(url.strip(), request_timeout)
    except (ValueError, OSError, UnicodeError, httpx.HTTPError, httpx.InvalidURL, TimeoutError):
        # URLs, redirect targets and exception text can carry OAuth credentials.
        logger.warning("download_img rejected or failed to read a public image")
        return ""


async def _download_public_image(url: str, request_timeout: float) -> str:
    for hop in range(_OAUTH_AVATAR_MAX_REDIRECTS + 1):
        original = httpx.URL(url)
        if original.username or original.password:
            raise ValueError("URL credentials are not allowed")
        # DNS resolution is blocking; include it in the total wall-clock budget.
        hostname, pin_ip = await asyncio.to_thread(assert_url_is_safe, url)
        headers = {"Host": original.netloc.decode("ascii")}
        user_agent = os.environ.get("HTTP_CLIENT_USER_AGENT", "multirag-http-client")
        if user_agent:
            headers["User-Agent"] = user_agent
        request = httpx.Request(
            "GET",
            original.copy_with(host=pin_ip),
            headers=headers,
            extensions={"sni_hostname": hostname, "timeout": httpx.Timeout(request_timeout).as_dict()},
        )
        # Direct transport avoids the client URL logger and implicit redirects,
        # cookies, proxy discovery or connection reuse across DNS validations.
        async with httpx.AsyncHTTPTransport(trust_env=False, retries=0) as transport:
            response = await transport.handle_async_request(request)
            try:
                if response.status_code in _REDIRECT_STATUS:
                    location = response.headers.get("location")
                    if not location or hop == _OAUTH_AVATAR_MAX_REDIRECTS:
                        raise ValueError("Invalid redirect")
                    url = urljoin(url, location)
                    continue
                if response.status_code != 200:
                    raise ValueError("Image response was not successful")
                length = response.headers.get("content-length")
                if length is not None and (int(length) < 0 or int(length) > _OAUTH_AVATAR_MAX_BYTES):
                    raise ValueError("Image too large")
                content_type = response.headers.get("content-type", "image/jpeg").split(";", 1)[0].strip().lower()
                if content_type not in {"image/jpeg", "image/png", "image/gif", "image/webp", "image/avif", "image/bmp", "image/x-icon", "image/vnd.microsoft.icon"}:
                    raise ValueError("Unsupported image type")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > _OAUTH_AVATAR_MAX_BYTES:
                        raise ValueError("Image too large")
                    body.extend(chunk)
                return "data:" + content_type + ";base64," + base64.b64encode(body).decode("ascii")
            finally:
                await response.aclose()
    return ""  # pragma: no cover - every final hop returns or raises


def hash_str2int(line: str, mod: int = 10**8) -> int:
    return int(hashlib.sha1(line.encode("utf-8")).hexdigest(), 16) % mod


def convert_bytes(size_in_bytes: int) -> str:
    """
    Format size in bytes.
    """
    if size_in_bytes == 0:
        return "0 B"

    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    i = 0
    size = float(size_in_bytes)

    while size >= 1024 and i < len(units) - 1:
        size /= 1024
        i += 1

    if i == 0 or size >= 100:
        return f"{size:.0f} {units[i]}"
    elif size >= 10:
        return f"{size:.1f} {units[i]}"
    else:
        return f"{size:.2f} {units[i]}"


def once(func):
    """
    A thread-safe decorator that ensures the decorated function runs exactly once,
    caching and returning its result for all subsequent calls. This prevents
    race conditions in multi-thread environments by using a lock to protect
    the execution state.

    Args:
        func (callable): The function to be executed only once.

    Returns:
        callable: A wrapper function that executes `func` on the first call
                  and returns the cached result thereafter.

    Example:
        @once
        def compute_expensive_value():
            print("Computing...")
            return 42

        # First call: executes and prints
        # Subsequent calls: return 42 without executing
    """
    executed = False
    result = None
    lock = threading.Lock()

    def wrapper(*args, **kwargs):
        nonlocal executed, result
        with lock:
            if not executed:
                executed = True
                result = func(*args, **kwargs)
        return result

    return wrapper


@once
def pip_install_torch():
    device = os.getenv("DEVICE", "cpu")
    if device == "cpu":
        return
    logging.info("Installing pytorch")
    pkg_names = ["torch>=2.5.0,<3.0.0"]
    subprocess.check_call([sys.executable, "-m", "pip", "install", *pkg_names])


@once
def _thread_pool_executor():
    max_workers_env = os.getenv("THREAD_POOL_MAX_WORKERS", "128")
    try:
        max_workers = int(max_workers_env)
    except ValueError:
        max_workers = 128
    if max_workers < 1:
        max_workers = 1
    return ThreadPoolExecutor(max_workers=max_workers)


async def thread_pool_exec(func, *args, **kwargs):
    loop = asyncio.get_running_loop()
    if kwargs:
        func = functools.partial(func, *args, **kwargs)
        return await loop.run_in_executor(_thread_pool_executor(), func)
    return await loop.run_in_executor(_thread_pool_executor(), func, *args)
