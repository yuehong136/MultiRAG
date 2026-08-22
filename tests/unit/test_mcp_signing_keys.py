"""EIM-A2 file signing provider and staged-rotation contracts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from api.identity.mcp_issuer.keys import (
    FileSigningKeyProvider,
    PublicEcJwk,
    SigningKeyError,
    SigningKeyErrorCode,
    SigningKeyRotationGuard,
    SigningKeySnapshot,
)


def _write_keypair(root: Path, kid: str, *, mode: int = 0o600) -> tuple[Path, Path]:
    private_key = ec.generate_private_key(ec.SECP256R1())
    private_path = root / f"{kid}.private.pem"
    public_path = root / f"{kid}.public.pem"
    private_path.write_bytes(
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )
    private_path.chmod(mode)
    public_path.write_bytes(
        private_key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ),
    )
    return private_path, public_path


def _provider(
    private_path: Path,
    active_kid: str,
    public_paths: dict[str, Path],
) -> FileSigningKeyProvider:
    return FileSigningKeyProvider.from_files(
        active_key_id=active_kid,
        private_key_file=private_path,
        public_key_files=public_paths,
    )


def test_file_provider_publishes_only_sorted_public_p256_jwks_and_raw_es256_signature(tmp_path: Path) -> None:
    current_private, current_public = _write_keypair(tmp_path, "current")
    _, next_public = _write_keypair(tmp_path, "next")

    provider = _provider(
        current_private,
        "current",
        {"next": next_public, "current": current_public},
    )

    snapshot = provider.snapshot
    document = snapshot.to_jwks_document()
    assert snapshot.active_kid == "current"
    assert [key["kid"] for key in document["keys"]] == ["current", "next"]
    assert all(set(key) == {"alg", "crv", "kid", "kty", "use", "x", "y"} and key["alg"] == "ES256" and key["crv"] == "P-256" and key["kty"] == "EC" and key["use"] == "sig" for key in document["keys"])
    first_signature = provider.sign_es256(b"header.payload")
    assert len(first_signature) == 64
    assert provider.sign_es256(b"header.payload") == first_signature
    assert str(current_private) not in repr(provider)
    assert "PRIVATE KEY" not in repr(provider)


@pytest.mark.skipif(__import__("os").name == "nt", reason="POSIX permission contract")
def test_file_provider_rejects_group_readable_private_key_without_leaking_path(tmp_path: Path) -> None:
    private_path, public_path = _write_keypair(tmp_path, "current", mode=0o640)

    with pytest.raises(SigningKeyError) as raised:
        _provider(private_path, "current", {"current": public_path})

    assert raised.value.code is SigningKeyErrorCode.PRIVATE_KEY_PERMISSIONS_INVALID
    assert str(private_path) not in str(raised.value)
    assert str(private_path) not in repr(raised.value)


@pytest.mark.skipif(__import__("os").name == "nt", reason="POSIX ownership contract")
def test_file_provider_rejects_private_key_owned_by_another_uid_without_leaking_path(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import os
    import traceback

    private_path, public_path = _write_keypair(tmp_path, "current")
    monkeypatch.setattr(os, "geteuid", lambda: private_path.stat().st_uid + 1)

    with pytest.raises(SigningKeyError) as raised:
        _provider(private_path, "current", {"current": public_path})

    assert raised.value.code is SigningKeyErrorCode.PRIVATE_KEY_PERMISSIONS_INVALID
    assert str(private_path) not in "".join(traceback.format_exception(raised.value))


def test_file_provider_rejects_active_private_public_mismatch(tmp_path: Path) -> None:
    private_path, _ = _write_keypair(tmp_path, "current")
    _, unrelated_public = _write_keypair(tmp_path, "unrelated")

    with pytest.raises(SigningKeyError) as raised:
        _provider(private_path, "current", {"current": unrelated_public})

    assert raised.value.code is SigningKeyErrorCode.ACTIVE_KEY_MISMATCH


def test_public_snapshot_rejects_untrusted_provider_metadata(tmp_path: Path) -> None:
    private_path, public_path = _write_keypair(tmp_path, "current")
    valid = _provider(private_path, "current", {"current": public_path}).snapshot.keys[0]

    for invalid in (
        lambda: PublicEcJwk(kid="../current", x=valid.x, y=valid.y),
        lambda: PublicEcJwk(kid="current", x="not-base64url", y=valid.y),
        lambda: SigningKeySnapshot(
            active_kid="key-0",
            keys=tuple(PublicEcJwk(kid=f"key-{index}", x=valid.x, y=valid.y) for index in range(33)),
        ),
    ):
        with pytest.raises(SigningKeyError) as raised:
            invalid()
        assert raised.value.code is SigningKeyErrorCode.PUBLIC_KEY_INVALID


def test_rotation_guard_enforces_publish_switch_retain_remove_sequence(tmp_path: Path) -> None:
    old_private, old_public = _write_keypair(tmp_path, "old")
    new_private, new_public = _write_keypair(tmp_path, "new")
    old_only = _provider(old_private, "old", {"old": old_public}).snapshot
    published = _provider(old_private, "old", {"old": old_public, "new": new_public}).snapshot
    switched = _provider(new_private, "new", {"old": old_public, "new": new_public}).snapshot
    removed = _provider(new_private, "new", {"new": new_public}).snapshot
    now = datetime(2026, 8, 13, 8, 0, tzinfo=UTC)

    guard = SigningKeyRotationGuard(old_only, retention_seconds=630)
    with pytest.raises(SigningKeyError) as raised:
        guard.transition(switched, now=now)
    assert raised.value.code is SigningKeyErrorCode.NEXT_KEY_NOT_PREPUBLISHED

    guard.transition(published, now=now)
    guard.transition(switched, now=now + timedelta(seconds=1))
    with pytest.raises(SigningKeyError) as raised:
        guard.transition(removed, now=now + timedelta(seconds=630))
    assert raised.value.code is SigningKeyErrorCode.RETIRED_KEY_REMOVED_EARLY

    guard.transition(removed, now=now + timedelta(seconds=631))
    assert guard.snapshot.active_kid == "new"
