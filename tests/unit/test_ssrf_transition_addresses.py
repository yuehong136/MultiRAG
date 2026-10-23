"""Both outbound boundaries validate embedded IPv4, without losing DNS pins."""

import ipaddress
import socket
from typing import Any

import pytest
import requests

from api.apps.services import agent_db_probe
from common import ssrf_guard


def addresses(*ips: str) -> list[Any]:
    return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0)) for ip in ips]


def wrapped(ip: str, prefix: str) -> str:
    return str(ipaddress.IPv6Address(int(ipaddress.IPv6Address(prefix)) | int(ipaddress.IPv4Address(ip))))


@pytest.mark.parametrize("prefix", ["64:ff9b::", "::", "::ffff:0:0"])
@pytest.mark.parametrize("ip", ["0.0.0.0", "10.0.0.1", "100.64.0.1", "127.0.0.1", "169.254.169.254", "172.16.0.1", "192.168.1.1", "240.0.0.1"])
def test_private_wrappers_are_rejected_by_url_and_raw_host(monkeypatch: pytest.MonkeyPatch, prefix: str, ip: str) -> None:
    target = wrapped(ip, prefix)
    monkeypatch.setattr(ssrf_guard, "_ORIGINAL_GETADDRINFO", lambda *_a, **_k: addresses("1.1.1.1", target))
    with pytest.raises(ValueError, match="public"):
        ssrf_guard.assert_host_is_safe("db.example")
    with pytest.raises(ValueError, match="non-public"):
        ssrf_guard.assert_url_is_safe("https://db.example/query")


@pytest.mark.parametrize("prefix", ["64:ff9b::", "::", "::ffff:0:0"])
def test_public_wrappers_keep_original_transport_ip(monkeypatch: pytest.MonkeyPatch, prefix: str) -> None:
    target = wrapped("1.1.1.1", prefix)
    monkeypatch.setattr(ssrf_guard, "_ORIGINAL_GETADDRINFO", lambda *_a, **_k: addresses(target))
    assert ssrf_guard.assert_host_is_safe("db.example") == target
    assert ssrf_guard.assert_url_is_safe("https://db.example/query") == ("db.example", target)


@pytest.mark.parametrize("db_type", ["mysql", "mariadb", "oceanbase", "postgres", "mssql", "IBM DB2", "trino"])
@pytest.mark.parametrize("target", ["64:ff9b::7f00:1", "::127.0.0.1"])
def test_probe_refuses_wrapped_private_before_any_driver_or_http(monkeypatch: pytest.MonkeyPatch, db_type: str, target: str) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("denied destination reached driver/HTTP transport")

    monkeypatch.setattr(ssrf_guard, "_ORIGINAL_GETADDRINFO", lambda *_a, **_k: addresses(target))
    monkeypatch.setattr(agent_db_probe, "create_engine", forbidden)
    monkeypatch.setattr(agent_db_probe, "import_module", forbidden)
    monkeypatch.setattr(requests.Session, "send", forbidden)
    with pytest.raises(agent_db_probe.DatabaseProbePolicyError, match="public"):
        agent_db_probe.probe_database(db_type=db_type, database="catalog.schema", username="user", host="db.example", port=5432, password="secret")
