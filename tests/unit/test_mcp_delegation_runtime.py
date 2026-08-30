"""EIM-P3 application-lifecycle composition contracts."""

from __future__ import annotations

import ipaddress
import socket
import ssl
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from api.identity.mcp_delegation import runtime
from api.identity.mcp_delegation.service import BoundMcpCredentialProvider
from api.identity.run_context import RunContext
from common.app_config import AppConfigError


def _system_ca_pem() -> str:
    certificates = ssl.create_default_context().get_ca_certs(binary_form=True)
    assert certificates
    return ssl.DER_cert_to_PEM_cert(certificates[0])


def _write_tls_fixture(root: Path, *, name: str) -> tuple[Path, Path, Path]:
    now = datetime.now(UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"{name} CA")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    leaf_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    leaf_cert = (
        x509.CertificateBuilder()
        .subject_name(leaf_name)
        .issuer_name(ca_cert.subject)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                ],
            ),
            critical=False,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    ca_path = root / f"{name}-ca.pem"
    cert_path = root / f"{name}-cert.pem"
    key_path = root / f"{name}-key.pem"
    ca_path.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    cert_path.write_bytes(leaf_cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        leaf_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )
    ca_path.chmod(0o644)
    cert_path.chmod(0o644)
    key_path.chmod(0o600)
    return ca_path, cert_path, key_path


def _tls_handshake(
    client_context: ssl.SSLContext,
    *,
    cert_path: Path,
    key_path: Path,
    server_hostname: str,
) -> None:
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert_path, key_path)
    server_socket, client_socket = socket.socketpair()
    server_errors: list[BaseException] = []

    def serve() -> None:
        try:
            with server_context.wrap_socket(
                server_socket,
                server_side=True,
            ) as connection:
                connection.recv(1)
        except BaseException as exc:  # The negative paths intentionally alert.
            server_errors.append(exc)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        with client_context.wrap_socket(
            client_socket,
            server_hostname=server_hostname,
        ) as connection:
            connection.sendall(b"x")
    finally:
        client_socket.close()
        thread.join(timeout=2)
    assert not thread.is_alive()
    if not server_errors:
        return
    # A successful client handshake must also complete on the server.  During
    # expected client verification failures the server receives an alert.
    if not isinstance(server_errors[0], ssl.SSLError):
        raise server_errors[0]


def test_agent_resolution_does_not_read_configuration_before_lifespan_activation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime.reset_mcp_delegation_service()
    monkeypatch.setattr(
        runtime,
        "get_app_config",
        lambda: (_ for _ in ()).throw(AssertionError("configuration must not be read")),
    )

    assert runtime.resolve_mcp_credential_provider(mcp_server=object(), run_context=None) is None


def test_lifespan_activation_publishes_one_immutable_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime.reset_mcp_delegation_service()
    observed: list[tuple[object, object]] = []
    provider = object.__new__(BoundMcpCredentialProvider)

    class Service:
        def bind(self, *, mcp_server: object, run_context: object) -> object:
            observed.append((mcp_server, run_context))
            return provider

    service = Service()
    monkeypatch.setattr(
        runtime,
        "get_app_config",
        lambda: SimpleNamespace(
            identity=SimpleNamespace(
                mcp_delegation=SimpleNamespace(enabled=True),
            ),
        ),
    )
    monkeypatch.setattr(runtime, "get_mcp_delegation_service", lambda: service)
    try:
        runtime.activate_mcp_delegation()
        server = object()
        context = RunContext(tenant_id="tenant-a")

        assert (
            runtime.resolve_mcp_credential_provider(
                mcp_server=server,
                run_context=context,
            )
            is provider
        )
        assert observed == [(server, context)]
    finally:
        runtime._active_service = None


def test_delegation_tls_ca_builds_an_isolated_hostname_checking_context(tmp_path: Path) -> None:
    ca_bundle = tmp_path / "ca.pem"
    ca_bundle.write_text(_system_ca_pem(), encoding="ascii")
    ca_bundle.chmod(0o644)

    context = runtime._build_tls_ssl_context(str(ca_bundle))

    assert context is not None
    assert context.check_hostname is True
    assert context.verify_mode is ssl.CERT_REQUIRED
    assert context.minimum_version >= ssl.TLSVersion.TLSv1_2
    assert runtime._build_tls_ssl_context("") is None

    provider = object.__new__(BoundMcpCredentialProvider)
    object.__setattr__(provider, "_service", object())
    object.__setattr__(provider, "_binding", object())
    object.__setattr__(provider, "_principal", object())
    object.__setattr__(provider, "_agent_id", "agent")
    object.__setattr__(provider, "_agent_revision_id", "revision")
    object.__setattr__(provider, "_tls_ssl_context", context)
    assert repr(context) not in repr(provider)


def test_delegation_tls_ca_rejects_symlink_unsafe_mode_and_invalid_pem(tmp_path: Path) -> None:
    ca_bundle = tmp_path / "ca.pem"
    ca_bundle.write_text(_system_ca_pem(), encoding="ascii")
    ca_bundle.chmod(0o666)

    with pytest.raises(AppConfigError, match="untrusted principal") as unsafe:
        runtime._build_tls_ssl_context(str(ca_bundle))
    assert str(ca_bundle) not in str(unsafe.value)
    assert str(ca_bundle) not in repr(unsafe.value)

    ca_bundle.chmod(0o644)
    link = tmp_path / "linked-ca.pem"
    link.symlink_to(ca_bundle)
    with pytest.raises(AppConfigError, match="invalid") as linked:
        runtime._build_tls_ssl_context(str(link))
    assert str(link) not in str(linked.value)

    ca_bundle.write_text("not a certificate", encoding="ascii")
    with pytest.raises(AppConfigError, match="invalid") as invalid:
        runtime._build_tls_ssl_context(str(ca_bundle))
    assert str(ca_bundle) not in str(invalid.value)


def test_delegation_tls_ca_owner_policy_accepts_only_process_or_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime, "_effective_user_id", lambda: 1000)

    assert runtime._trusted_ca_bundle_owner(1000) is True
    assert runtime._trusted_ca_bundle_owner(0) is True
    assert runtime._trusted_ca_bundle_owner(1001) is False


def test_delegation_tls_context_performs_real_explicit_trust_handshakes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trusted_ca, certificate, private_key = _write_tls_fixture(
        tmp_path,
        name="trusted",
    )
    wrong_ca, _, _ = _write_tls_fixture(tmp_path, name="wrong")
    monkeypatch.setenv("SSL_CERT_FILE", str(wrong_ca))

    trusted_context = runtime._build_tls_ssl_context(str(trusted_ca))
    assert trusted_context is not None
    _tls_handshake(
        trusted_context,
        cert_path=certificate,
        key_path=private_key,
        server_hostname="localhost",
    )

    with pytest.raises(ssl.SSLCertVerificationError):
        _tls_handshake(
            trusted_context,
            cert_path=certificate,
            key_path=private_key,
            server_hostname="wrong.invalid",
        )

    wrong_context = runtime._build_tls_ssl_context(str(wrong_ca))
    assert wrong_context is not None
    with pytest.raises(ssl.SSLCertVerificationError):
        _tls_handshake(
            wrong_context,
            cert_path=certificate,
            key_path=private_key,
            server_hostname="localhost",
        )


def test_delegation_tls_ca_loader_rejects_foreign_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ca_bundle = tmp_path / "ca.pem"
    ca_bundle.write_text(_system_ca_pem(), encoding="ascii")
    ca_bundle.chmod(0o644)
    observed_owner_ids: list[int] = []

    def reject_owner(file_owner_id: int) -> bool:
        observed_owner_ids.append(file_owner_id)
        return False

    monkeypatch.setattr(runtime, "_trusted_ca_bundle_owner", reject_owner)

    with pytest.raises(AppConfigError, match="ownership permit an untrusted principal") as raised:
        runtime._build_tls_ssl_context(str(ca_bundle))

    assert observed_owner_ids == [ca_bundle.stat().st_uid]
    assert str(ca_bundle) not in str(raised.value)
