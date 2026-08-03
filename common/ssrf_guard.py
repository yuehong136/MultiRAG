#
# Copyright 2025 The InfiniFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
"""Shared SSRF validation and DNS-pinning helpers."""

import ipaddress
import logging
import socket
import threading
from collections.abc import Collection, Iterator
from contextlib import contextmanager
from typing import Any, cast
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_DEFAULT_ALLOWED_SCHEMES = frozenset({"http", "https"})
_LOCAL_DNS_PINS = threading.local()
_GLOBAL_DNS_PINS: dict[str, str] = {}
_GLOBAL_PIN_LOCK = threading.Lock()
_ORIGINAL_GETADDRINFO = socket.getaddrinfo


def _pinned_addrinfo(ip: str, port: str | int | None) -> list[tuple[int, int, int, str, tuple[str, int]]]:
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, int(port or 0)))]


def _getaddrinfo_with_pins(
    host: str | bytes | None,
    port: str | int | None,
    *args: Any,
    **kwargs: Any,
) -> list[Any]:
    if isinstance(host, str):
        local_pins: dict[str, str] = getattr(_LOCAL_DNS_PINS, "dns_pins", {})
        if host in local_pins:
            return _pinned_addrinfo(local_pins[host], port)

        with _GLOBAL_PIN_LOCK:
            global_ip = _GLOBAL_DNS_PINS.get(host)
        if global_ip is not None:
            return _pinned_addrinfo(global_ip, port)

    return _ORIGINAL_GETADDRINFO(host, port, *args, **kwargs)


# requests/urllib3 ultimately resolve through this function. Pins are inactive
# unless a caller enters one of the context managers below.
socket.getaddrinfo = cast(Any, _getaddrinfo_with_pins)


@contextmanager
def pin_dns(hostname: str, ip: str) -> Iterator[None]:
    """Pin a hostname to a validated IP for synchronous work in this thread."""
    pins: dict[str, str] = _LOCAL_DNS_PINS.__dict__.setdefault("dns_pins", {})
    previous = pins.get(hostname)
    pins[hostname] = ip
    try:
        yield
    finally:
        if previous is None:
            pins.pop(hostname, None)
        else:
            pins[hostname] = previous


@contextmanager
def pin_dns_global(hostname: str, ip: str) -> Iterator[None]:
    """Pin a hostname across threads for crawlers that resolve in executors."""
    with _GLOBAL_PIN_LOCK:
        previous = _GLOBAL_DNS_PINS.get(hostname)
        _GLOBAL_DNS_PINS[hostname] = ip
    try:
        yield
    finally:
        with _GLOBAL_PIN_LOCK:
            if previous is None:
                _GLOBAL_DNS_PINS.pop(hostname, None)
            else:
                _GLOBAL_DNS_PINS[hostname] = previous


def _effective_ip(
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def _safe_url_label(url: str) -> str:
    """Return a log-safe scheme/host label without credentials, path, or query."""
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname or "(missing)"
        port = f":{parsed.port}" if parsed.port is not None else ""
        return f"{parsed.scheme or '(missing)'}://{hostname}{port}"
    except ValueError:
        return "(invalid URL)"


def assert_url_is_safe(
    url: str,
    *,
    allowed_schemes: Collection[str] = _DEFAULT_ALLOWED_SCHEMES,
) -> tuple[str, str]:
    """Validate an outbound URL and return its hostname and first public IP.

    Every DNS answer must be globally routable. Returning the resolved IP lets
    the caller pin the subsequent connection and close the DNS-rebinding gap.
    """
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
    except ValueError as exc:
        logger.warning("SSRF guard blocked malformed URL: url=%s", _safe_url_label(url))
        raise ValueError("URL is malformed.") from exc

    normalized_schemes = frozenset(scheme.lower() for scheme in allowed_schemes)
    if parsed.scheme.lower() not in normalized_schemes:
        logger.warning(
            "SSRF guard blocked URL scheme: scheme=%r url=%s",
            parsed.scheme,
            _safe_url_label(url),
        )
        raise ValueError(f"Disallowed URL scheme: {parsed.scheme!r}. Only {sorted(normalized_schemes)} are allowed.")
    if not hostname:
        logger.warning("SSRF guard blocked URL with missing host: url=%s", _safe_url_label(url))
        raise ValueError("URL is missing a host.")

    try:
        addr_infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        logger.warning("SSRF guard could not resolve hostname=%r", hostname)
        raise ValueError(f"Could not resolve hostname {hostname!r}.") from exc

    resolved_ip: str | None = None
    for _family, _type, _proto, _canonname, sockaddr in addr_infos:
        raw_address = sockaddr[0]
        if not isinstance(raw_address, str):
            raise ValueError("Hostname resolved to an unsupported address type.")
        raw_ip = ipaddress.ip_address(raw_address.split("%", maxsplit=1)[0])
        if not _effective_ip(raw_ip).is_global:
            logger.warning(
                "SSRF guard blocked URL: hostname=%r resolved to non-public address=%s",
                hostname,
                raw_ip,
            )
            raise ValueError(f"URL resolves to a non-public address ({raw_ip}), which is not allowed.")
        if resolved_ip is None:
            resolved_ip = str(raw_ip)

    if resolved_ip is None:
        logger.warning("SSRF guard blocked URL: hostname=%r resolved to no addresses", hostname)
        raise ValueError(f"Hostname {hostname!r} resolved to no addresses.")

    return hostname, resolved_ip
