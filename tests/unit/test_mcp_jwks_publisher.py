"""EIM-O5 public-only loopback JWKS publisher contracts."""

from __future__ import annotations

import inspect
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from api.apps.well_known import public_mcp_jwks
from api.identity import jwks_publisher
from api.identity.jwks_publisher import JwksPublisherError
from api.identity.mcp_issuer.keys import SigningKeyError, load_public_signing_key_snapshot
from api.identity.mcp_issuer.service import McpTokenIssuer


def _write_public_key(path: Path) -> None:
    private_key = ec.generate_private_key(ec.SECP256R1())
    path.write_bytes(
        private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ),
    )


def _write_manifest(path: Path, public_key: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "format": 1,
                "active_key_id": "p3-local",
                "jwks_cache_ttl_seconds": 300,
                "public_key_files": {"p3-local": str(public_key)},
            },
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)


def test_publisher_jwks_is_byte_identical_to_main_api_and_routes_are_minimal(tmp_path: Path) -> None:
    public_key = tmp_path / "p3-public.pem"
    _write_public_key(public_key)
    snapshot = load_public_signing_key_snapshot(
        active_key_id="p3-local",
        public_key_files={"p3-local": public_key},
    )
    app = jwks_publisher.create_jwks_publisher_app(
        snapshot=snapshot,
        cache_ttl_seconds=300,
    )

    issuer = object.__new__(McpTokenIssuer)
    issuer._profile = SimpleNamespace(jwks_cache_ttl_seconds=300)
    issuer._signing_keys = SimpleNamespace(snapshot=snapshot)

    with TestClient(app) as client:
        jwks_response = client.get("/.well-known/jwks.json")
        live_response = client.get("/livez")
        assert client.get("/docs").status_code == 404
        assert client.get("/openapi.json").status_code == 404
        assert client.get("/anything-else").status_code == 404

    assert jwks_response.status_code == 200
    assert jwks_response.content == public_mcp_jwks(issuer).body
    assert jwks_response.headers["cache-control"] == "public, max-age=300"
    assert live_response.json() == {"status": "live"}
    assert live_response.headers["cache-control"] == "no-store"


def test_publisher_manifest_loads_public_keys_without_touching_signing_private_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    public_key = tmp_path / "p3-public.pem"
    manifest = tmp_path / "public-keys.json"
    _write_public_key(public_key)
    _write_manifest(manifest, public_key)
    monkeypatch.setattr(
        "api.identity.mcp_issuer.keys._load_private_key",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("private key must not be read")),
    )

    app = jwks_publisher.load_jwks_publisher_app(str(manifest))

    with TestClient(app) as client:
        assert client.get("/.well-known/jwks.json").json()["keys"][0]["kid"] == "p3-local"


def test_publisher_manifest_rejects_private_or_unknown_fields(tmp_path: Path) -> None:
    public_key = tmp_path / "p3-public.pem"
    manifest = tmp_path / "public-keys.json"
    _write_public_key(public_key)
    _write_manifest(manifest, public_key)
    document = json.loads(manifest.read_text(encoding="utf-8"))
    document["private_key_file"] = str(tmp_path / "private.pem")
    manifest.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(JwksPublisherError, match="manifest is invalid"):
        jwks_publisher.load_jwks_publisher_app(str(manifest))


def test_publisher_module_has_no_application_or_private_signer_dependency() -> None:
    source = inspect.getsource(jwks_publisher)

    assert "common.app_config" not in source
    assert "common.bootstrap" not in source
    assert "api.db" not in source
    assert "FileSigningKeyProvider" not in source

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import api.identity.jwks_publisher; "
                "blocked=[name for name in sys.modules "
                "if name.startswith(('api.db', 'common.bootstrap', 'common.resources'))]; "
                "assert not blocked, blocked"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


def test_publisher_manifest_rejects_invalid_key_id_through_production_parser(tmp_path: Path) -> None:
    public_key = tmp_path / "p3-public.pem"
    manifest = tmp_path / "public-keys.json"
    _write_public_key(public_key)
    _write_manifest(manifest, public_key)
    document = json.loads(manifest.read_text(encoding="utf-8"))
    document["active_key_id"] = "bad kid"
    document["public_key_files"] = {"bad kid": str(public_key)}
    manifest.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(SigningKeyError):
        jwks_publisher.load_jwks_publisher_app(str(manifest))


def test_publisher_rejects_writable_public_key_material(tmp_path: Path) -> None:
    public_key = tmp_path / "p3-public.pem"
    manifest = tmp_path / "public-keys.json"
    _write_public_key(public_key)
    public_key.chmod(0o666)
    _write_manifest(manifest, public_key)

    with pytest.raises(SigningKeyError):
        jwks_publisher.load_jwks_publisher_app(str(manifest))


def test_public_publisher_owner_policy_accepts_only_process_or_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from api.identity.mcp_issuer import keys

    monkeypatch.setattr(keys, "_effective_user_id", lambda: 1000)

    assert keys._trusted_public_key_owner(1000) is True
    assert keys._trusted_public_key_owner(0) is True
    assert keys._trusted_public_key_owner(1001) is False


def test_publisher_loader_rejects_foreign_owned_public_key_material(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from api.identity.mcp_issuer import keys

    public_key = tmp_path / "p3-public.pem"
    _write_public_key(public_key)
    monkeypatch.setattr(keys, "_trusted_public_key_owner", lambda _owner: False)

    with pytest.raises(SigningKeyError):
        load_public_signing_key_snapshot(
            active_key_id="p3-local",
            public_key_files={"p3-local": public_key},
            enforce_deployment_permissions=True,
        )


def test_publisher_deployment_owner_policy_accepts_only_process_or_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(jwks_publisher, "_effective_user_id", lambda: 1000)

    assert jwks_publisher._trusted_deployment_file_owner(1000) is True
    assert jwks_publisher._trusted_deployment_file_owner(0) is True
    assert jwks_publisher._trusted_deployment_file_owner(1001) is False


def test_publisher_rejects_foreign_owned_manifest_and_tls_certificate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    public_key = tmp_path / "p3-public.pem"
    manifest = tmp_path / "public-keys.json"
    certificate = tmp_path / "tls-cert.pem"
    _write_public_key(public_key)
    _write_manifest(manifest, public_key)
    certificate.write_text("fixture certificate", encoding="ascii")
    certificate.chmod(0o644)
    observed_owner_ids: list[int] = []

    def reject_owner(file_owner_id: int) -> bool:
        observed_owner_ids.append(file_owner_id)
        return False

    monkeypatch.setattr(jwks_publisher, "_trusted_deployment_file_owner", reject_owner)

    with pytest.raises(JwksPublisherError, match="manifest permissions or ownership") as manifest_error:
        jwks_publisher.load_jwks_publisher_app(str(manifest))
    with pytest.raises(JwksPublisherError, match="certificate permissions or ownership") as certificate_error:
        jwks_publisher._validate_tls_file(str(certificate), private=False)

    assert observed_owner_ids == [manifest.stat().st_uid, certificate.stat().st_uid]
    assert str(manifest) not in str(manifest_error.value)
    assert str(certificate) not in str(certificate_error.value)


def test_publisher_cli_requires_loopback_and_explicit_tls_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    certificate = tmp_path / "tls-cert.pem"
    private_key = tmp_path / "tls-key.pem"
    certificate.write_text("fixture certificate", encoding="ascii")
    certificate.chmod(0o644)
    private_key.write_text("fixture private key", encoding="ascii")
    private_key.chmod(0o600)
    app = object()
    observed: dict[str, Any] = {}

    def load_app(manifest: str) -> object:
        observed["manifest"] = manifest
        return app

    monkeypatch.setattr(
        jwks_publisher,
        "load_jwks_publisher_app",
        load_app,
    )
    monkeypatch.setattr(
        jwks_publisher.uvicorn,
        "run",
        lambda loaded_app, **kwargs: observed.update(app=loaded_app, **kwargs),
    )
    monkeypatch.setattr(
        jwks_publisher,
        "_validate_tls_pair",
        lambda cert_file, key_file: observed.update(validated_pair=(cert_file, key_file)),
    )

    jwks_publisher.main(
        [
            "--public-key-manifest",
            "/deployment/public-keys.json",
            "--cert-file",
            str(certificate),
            "--key-file",
            str(private_key),
        ],
    )

    assert observed["manifest"] == "/deployment/public-keys.json"
    assert observed["app"] is app
    assert observed["host"] == "127.0.0.1"
    assert observed["port"] == 9277
    assert observed["ssl_certfile"] == str(certificate)
    assert observed["ssl_keyfile"] == str(private_key)
    assert observed["validated_pair"] == (str(certificate), str(private_key))

    with pytest.raises(SystemExit):
        jwks_publisher.main(
            [
                "--host",
                "0.0.0.0",
                "--public-key-manifest",
                "/deployment/public-keys.json",
                "--cert-file",
                str(certificate),
                "--key-file",
                str(private_key),
            ],
        )
    with pytest.raises(SystemExit):
        jwks_publisher.main(
            [
                "--host",
                "localhost",
                "--public-key-manifest",
                "/deployment/public-keys.json",
                "--cert-file",
                str(certificate),
                "--key-file",
                str(private_key),
            ],
        )


def test_publisher_cli_rejects_missing_or_unsafe_tls_pair_without_path_leakage(
    tmp_path: Path,
) -> None:
    certificate = tmp_path / "tls-cert.pem"
    private_key = tmp_path / "tls-key.pem"
    certificate.write_text("fixture certificate", encoding="ascii")
    certificate.chmod(0o644)
    private_key.write_text("fixture private key", encoding="ascii")
    private_key.chmod(0o644)

    with pytest.raises(SystemExit):
        jwks_publisher.main(
            [
                "--public-key-manifest",
                "/deployment/public-keys.json",
                "--cert-file",
                str(certificate),
            ],
        )

    with pytest.raises(JwksPublisherError, match="permissions are invalid") as raised:
        jwks_publisher.main(
            [
                "--public-key-manifest",
                "/deployment/public-keys.json",
                "--cert-file",
                str(certificate),
                "--key-file",
                str(private_key),
            ],
        )
    assert str(private_key) not in str(raised.value)
    assert str(private_key) not in repr(raised.value)

    private_key.chmod(0o600)
    with pytest.raises(JwksPublisherError, match="certificate/key pair is invalid") as invalid_pair:
        jwks_publisher.main(
            [
                "--public-key-manifest",
                "/deployment/public-keys.json",
                "--cert-file",
                str(certificate),
                "--key-file",
                str(private_key),
            ],
        )
    assert str(certificate) not in str(invalid_pair.value)
    assert str(private_key) not in str(invalid_pair.value)
