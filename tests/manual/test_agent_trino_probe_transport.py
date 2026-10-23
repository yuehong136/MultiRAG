"""Explicit optional-driver acceptance: run with an isolated pinned Trino install.

python -m pytest tests/manual/test_agent_trino_probe_transport.py -q
No production Trino or external destination is used. The initial host validator
is replaced only to admit the fixture-owned TLS endpoint; all subsequent URL,
DNS, redirect, proxy, authentication and cleanup enforcement remains real.
"""

import base64
import datetime
import importlib.util
import ipaddress
import json
import os
import socket
import ssl
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import requests
import trino
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from common import ssrf_guard


@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[dict[str, Any]]:
    module_path = os.environ.get("TRINO_PROBE_BASELINE_FILE") or str(Path(__file__).resolve().parents[2] / "api/apps/services/agent_db_probe.py")
    spec = importlib.util.spec_from_file_location("probe_under_test", module_path)
    assert spec is not None and spec.loader is not None
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    state: dict[str, Any] = {"mode": "success", "requests": [], "victim_requests": [], "closed_sessions": [], "dns": []}
    expected_auth = "Basic " + base64.b64encode(b"probe-user:synthetic-password").decode()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            pass

        def answer(self) -> None:
            victim = self.server is state["victim"]
            record = {"method": self.command, "path": self.path, "host": self.headers.get("Host"), "authenticated": self.headers.get("Authorization") == expected_auth}
            state["victim_requests" if victim else "requests"].append(record)
            length = int(self.headers.get("Content-Length", "0"))
            if length:
                assert self.rfile.read(length) == b"SELECT 1"
            mode = state["mode"]
            if not victim and mode == f"redirect_{self.command.lower()}":
                self.send_response(307)
                self.send_header("Location", state["target"])
                self.end_headers()
                return
            if self.command == "DELETE":
                self.send_response(500 if mode == "spool_cleanup_failure" else 204)
                self.end_headers()
                return
            payload: dict[str, Any] = {
                "id": "probe-query",
                "infoUri": state["origin"],
                "stats": {"state": "RUNNING"},
                "columns": [{"name": "one", "type": "bigint", "typeSignature": {"rawType": "bigint", "arguments": []}}],
            }
            if victim:
                payload["data"] = [[1]]
            elif mode == f"foreign_{self.command.lower()}":
                payload["nextUri"] = state["target"]
            elif mode in {f"spool_{self.command.lower()}", "spool_cleanup_failure"} or (mode == "redirect_delete" and self.command == "POST"):
                payload.update(
                    nextUri=state["origin"] + "/v1/statement/cancel",
                    data={"encoding": "json", "segments": [{"type": "spooled", "uri": state["target"], "ackUri": state["target"], "metadata": {"rowOffset": 0, "rowsCount": 1, "segmentSize": 5}}]},
                )
            elif self.command == "POST":
                payload["nextUri"] = state["origin"] + "/v1/statement/page"
            else:
                payload["data"] = [[0 if mode == "bad_result" else 1]]
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_POST = do_GET = do_DELETE = answer

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "coordinator.test")])
    now = datetime.datetime.now(datetime.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("coordinator.test"), x509.DNSName("xn--tst-qla.test"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / "ca.pem", tmp_path / "key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    state["victim"] = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(cert_path, key_path)
    server.socket = tls.wrap_socket(server.socket, server_side=True)
    state["host"] = "coordinator.test"
    state["origin"] = f"https://coordinator.test:{server.server_port}"
    state["target"] = f"http://127.0.0.1:{state['victim'].server_port}/victim"
    threads = [threading.Thread(target=item.serve_forever, daemon=True) for item in [server, state["victim"]]]
    for thread in threads:
        thread.start()
    monkeypatch.setenv("TRINO_USE_TLS", "1")
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(cert_path))
    monkeypatch.setattr(probe, "assert_host_is_safe", lambda _: "127.0.0.1")
    original_close = requests.Session.close

    def track_close(self: requests.Session) -> None:
        original_close(self)
        state["closed_sessions"].append(id(self))

    monkeypatch.setattr(requests.Session, "close", track_close)
    original_dns = ssrf_guard._ORIGINAL_GETADDRINFO

    def observe_dns(host: Any, *args: Any, **kwargs: Any) -> Any:
        state["dns"].append(str(host))
        if str(host).lower().rstrip(".") in {"coordinator.test", "xn--tst-qla.test"}:
            # An attacker-controlled second answer would direct TLS to the
            # victim listener. A correct pin never enters this resolver.
            return original_dns("127.0.0.1", *args, **kwargs)
        return original_dns(host, *args, **kwargs)

    monkeypatch.setattr(ssrf_guard, "_ORIGINAL_GETADDRINFO", observe_dns)
    state.update(probe=probe, port=server.server_port, certificate=str(cert_path))
    try:
        yield state
    finally:
        for item in [server, state["victim"]]:
            item.shutdown()
            item.server_close()
        for thread in threads:
            thread.join(5)
            assert not thread.is_alive()
        ports = [server.server_port, state["victim"].server_port]
        for port in ports:
            with socket.socket() as sock:
                assert sock.connect_ex(("127.0.0.1", port)) != 0
        cert_path.unlink()
        key_path.unlink()
        assert not cert_path.exists() and not key_path.exists()
        if evidence := os.environ.get("MULTIRAG_D_EVIDENCE_DIR"):
            Path(evidence).mkdir(parents=True, exist_ok=True)
            Path(evidence, f"trino-{uuid4().hex}.resources.json").write_text(
                json.dumps(
                    {
                        "mode": state["mode"],
                        "ports": ports,
                        "ports_closed": True,
                        "threads_stopped": True,
                        "tls_files_removed": True,
                        "requests": state["requests"],
                        "victim_requests": state["victim_requests"],
                        "closed_sessions": len(set(state["closed_sessions"])),
                        "dns": state["dns"],
                        "trino_version": trino.__version__,
                        "requests_version": requests.__version__,
                    },
                    indent=2,
                )
            )


def run(state: dict[str, Any]) -> None:
    state["probe"].probe_database(db_type="trino", database="catalog.schema", username="probe-user", password="synthetic-password", host=state["host"], port=state["port"])


@pytest.mark.parametrize("host", ["coordinator.test", "CoOrDiNaToR.TeSt", "coordinator.test.", "TÄST.test"])
def test_actual_driver_https_same_origin_pagination_dns_auth_and_proxy(transport: dict[str, Any], monkeypatch: pytest.MonkeyPatch, host: str) -> None:
    transport["host"] = host
    canonical = requests.Request("GET", f"https://{host}:{transport['port']}/").prepare().url
    assert canonical is not None
    transport["origin"] = canonical.rstrip("/")
    monkeypatch.setenv("HTTPS_PROXY", transport["target"])
    monkeypatch.setenv("ALL_PROXY", transport["target"])
    monkeypatch.setattr(trino.client, "PROXIES", {"http": transport["target"], "https": transport["target"]})
    run(transport)
    assert [(r["method"], r["path"]) for r in transport["requests"]] == [("POST", "/v1/statement"), ("GET", "/v1/statement/page")]
    assert all(r["authenticated"] and r["host"] == transport["origin"].replace("https://", "").replace(".:", ":") for r in transport["requests"])
    assert not transport["victim_requests"] and transport["closed_sessions"] and not transport["dns"]


@pytest.mark.parametrize("mode", ["foreign_post", "foreign_get", "redirect_post", "redirect_get", "spool_post", "spool_get", "spool_cleanup_failure", "redirect_delete"])
def test_actual_driver_refuses_continuation_redirect_and_spooling(transport: dict[str, Any], mode: str) -> None:
    transport["mode"] = mode
    with pytest.raises(transport["probe"].DatabaseProbePolicyError):
        run(transport)
    assert not transport["victim_requests"] and transport["closed_sessions"]
    if mode in {"foreign_get", "redirect_get", "spool_post", "spool_get", "spool_cleanup_failure", "redirect_delete"}:
        assert any(r["method"] == "DELETE" for r in transport["requests"])


def test_actual_driver_does_not_accept_a_non_one_query_result(transport: dict[str, Any]) -> None:
    transport["mode"] = "bad_result"
    with pytest.raises(RuntimeError, match="unexpected result"):
        run(transport)
    assert not transport["victim_requests"] and transport["closed_sessions"]


@pytest.mark.parametrize("host", ["CoOrDiNaToR.TeSt", "coordinator.test.", "TÄST.test"])
def test_canonical_hostname_is_pinned_for_cancellation(transport: dict[str, Any], host: str) -> None:
    transport["host"] = host
    canonical = requests.Request("GET", f"https://{host}:{transport['port']}/").prepare().url
    assert canonical is not None
    transport["origin"] = canonical.rstrip("/")
    transport["mode"] = "spool_get"
    with pytest.raises(transport["probe"].DatabaseProbePolicyError):
        run(transport)
    assert any(r["method"] == "DELETE" for r in transport["requests"])
    assert not transport["dns"] and not transport["victim_requests"] and transport["closed_sessions"]


def test_actual_driver_preserves_curl_ca_bundle_fallback(transport: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
    monkeypatch.setenv("CURL_CA_BUNDLE", transport["certificate"])
    run(transport)
    assert transport["closed_sessions"] and not transport["victim_requests"]
