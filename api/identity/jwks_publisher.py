"""Loopback-only TLS publisher for the MultiRAG P3 public JWKS.

This process deliberately assembles a :class:`SigningKeySnapshot` from public
PEM files only.  It does not initialize MultiRAG resources, load the P3 signing
private key, or connect to the database.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import ssl
import stat
from collections.abc import Sequence
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from api.identity.mcp_issuer.keys import SigningKeySnapshot, load_public_signing_key_snapshot

_MAX_TLS_FILE_BYTES = 65_536
_MAX_PUBLIC_MANIFEST_BYTES = 1_048_576


class JwksPublisherError(RuntimeError):
    """Stable startup refusal that omits deployment paths and file content."""


def create_jwks_publisher_app(
    *,
    snapshot: SigningKeySnapshot,
    cache_ttl_seconds: int,
) -> FastAPI:
    """Create an input-free app whose JWKS bytes match the main API route."""

    app = FastAPI(
        title="MultiRAG JWKS publisher",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.get("/.well-known/jwks.json", include_in_schema=False)
    def public_jwks() -> JSONResponse:
        return JSONResponse(
            content=snapshot.to_jwks_document(),
            headers={
                "Cache-Control": f"public, max-age={cache_ttl_seconds}",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.get("/livez", include_in_schema=False)
    def livez() -> JSONResponse:
        return JSONResponse(
            content={"status": "live"},
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    return app


def _read_public_key_manifest(manifest_file: str) -> tuple[str, int, dict[str, Path]]:
    path = Path(manifest_file)
    if not manifest_file or manifest_file != manifest_file.strip() or not path.is_absolute() or path.is_symlink():
        raise JwksPublisherError("JWKS public-key manifest is invalid")
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError:
        raise JwksPublisherError("JWKS public-key manifest is unavailable") from None
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size <= 0 or metadata.st_size > _MAX_PUBLIC_MANIFEST_BYTES:
            raise JwksPublisherError("JWKS public-key manifest is invalid")
        if os.name != "nt":
            mode = stat.S_IMODE(metadata.st_mode)
            if mode & 0o022 or not _trusted_deployment_file_owner(metadata.st_uid):
                raise JwksPublisherError("JWKS public-key manifest permissions or ownership permit an untrusted principal")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            raw = stream.read(_MAX_PUBLIC_MANIFEST_BYTES + 1)
        if len(raw) > _MAX_PUBLIC_MANIFEST_BYTES:
            raise JwksPublisherError("JWKS public-key manifest is invalid")
    except OSError:
        raise JwksPublisherError("JWKS public-key manifest is unavailable") from None
    finally:
        os.close(descriptor)
    try:
        document = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError):
        raise JwksPublisherError("JWKS public-key manifest is invalid") from None
    expected_keys = {
        "active_key_id",
        "format",
        "jwks_cache_ttl_seconds",
        "public_key_files",
    }
    if type(document) is not dict or set(document) != expected_keys or document.get("format") != 1:
        raise JwksPublisherError("JWKS public-key manifest is invalid")
    active_key_id = document.get("active_key_id")
    cache_ttl_seconds = document.get("jwks_cache_ttl_seconds")
    public_key_files = document.get("public_key_files")
    if (
        type(active_key_id) is not str
        or type(cache_ttl_seconds) is not int
        or not 1 <= cache_ttl_seconds <= 86_400
        or type(public_key_files) is not dict
        or not 1 <= len(public_key_files) <= 32
        or any(type(key_id) is not str or type(public_path) is not str or not public_path or not Path(public_path).is_absolute() for key_id, public_path in public_key_files.items())
    ):
        raise JwksPublisherError("JWKS public-key manifest is invalid")
    return (
        active_key_id,
        cache_ttl_seconds,
        {key_id: Path(public_path) for key_id, public_path in public_key_files.items()},
    )


def load_jwks_publisher_app(public_key_manifest: str) -> FastAPI:
    """Load only public P3 material from a dedicated deployment manifest."""

    active_key_id, cache_ttl_seconds, public_key_files = _read_public_key_manifest(
        public_key_manifest,
    )
    snapshot = load_public_signing_key_snapshot(
        active_key_id=active_key_id,
        public_key_files=public_key_files,
        enforce_deployment_permissions=True,
    )
    return create_jwks_publisher_app(
        snapshot=snapshot,
        cache_ttl_seconds=cache_ttl_seconds,
    )


def _is_loopback_host(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _effective_user_id() -> int:
    getter = getattr(os, "geteuid", None)
    if getter is None:
        raise JwksPublisherError("JWKS publisher TLS private-key ownership cannot be verified")
    return int(getter())


def _trusted_deployment_file_owner(file_owner_id: int) -> bool:
    return file_owner_id in {0, _effective_user_id()}


def _validate_tls_file(path_value: str, *, private: bool) -> str:
    path = Path(path_value)
    if not path_value or path_value != path_value.strip() or not path.is_absolute() or path.is_symlink():
        raise JwksPublisherError("JWKS publisher TLS file is invalid")
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError:
        raise JwksPublisherError("JWKS publisher TLS file is unavailable") from None
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size <= 0 or metadata.st_size > _MAX_TLS_FILE_BYTES:
            raise JwksPublisherError("JWKS publisher TLS file is invalid")
        if os.name != "nt":
            mode = stat.S_IMODE(metadata.st_mode)
            if private:
                if mode not in {0o400, 0o600} or metadata.st_uid != _effective_user_id():
                    raise JwksPublisherError("JWKS publisher TLS private-key permissions are invalid")
            elif mode & 0o022 or not _trusted_deployment_file_owner(metadata.st_uid):
                raise JwksPublisherError("JWKS publisher TLS certificate permissions or ownership permit an untrusted principal")
    finally:
        os.close(descriptor)
    return str(path)


def _validate_tls_pair(cert_file: str, key_file: str) -> None:
    """Fail with a stable error before uvicorn can render file details."""

    try:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(certfile=cert_file, keyfile=key_file)
    except (OSError, ValueError, ssl.SSLError):
        raise JwksPublisherError("JWKS publisher TLS certificate/key pair is invalid") from None


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Publish the MultiRAG public P3 JWKS over loopback TLS")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9277)
    parser.add_argument("--public-key-manifest", required=True)
    parser.add_argument("--cert-file", required=True)
    parser.add_argument("--key-file", required=True)
    args = parser.parse_args(argv)

    if not _is_loopback_host(args.host):
        parser.error("--host must resolve literally to a loopback address")
    if not 1 <= args.port <= 65_535:
        parser.error("--port must be between 1 and 65535")

    cert_file = _validate_tls_file(args.cert_file, private=False)
    key_file = _validate_tls_file(args.key_file, private=True)
    _validate_tls_pair(cert_file, key_file)
    app = load_jwks_publisher_app(args.public_key_manifest)
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        ssl_certfile=cert_file,
        ssl_keyfile=key_file,
        access_log=False,
    )


if __name__ == "__main__":
    main()


__all__ = [
    "JwksPublisherError",
    "create_jwks_publisher_app",
    "load_jwks_publisher_app",
    "main",
]
