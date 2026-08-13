"""Vendor-neutral ES256 signing boundary and strict file-backed provider."""

from __future__ import annotations

import base64
import binascii
import os
import stat
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Protocol, runtime_checkable

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature


class SigningKeyErrorCode(StrEnum):
    PRIVATE_KEY_UNAVAILABLE = "private_key_unavailable"
    PRIVATE_KEY_PERMISSIONS_INVALID = "private_key_permissions_invalid"
    PRIVATE_KEY_INVALID = "private_key_invalid"
    PUBLIC_KEY_INVALID = "public_key_invalid"
    ACTIVE_KEY_MISMATCH = "active_key_mismatch"
    SIGNING_FAILED = "signing_failed"
    NEXT_KEY_NOT_PREPUBLISHED = "next_key_not_prepublished"
    ACTIVE_KEY_NOT_RETAINED = "active_key_not_retained"
    RETIRED_KEY_REMOVED_EARLY = "retired_key_removed_early"
    ROTATION_TIME_INVALID = "rotation_time_invalid"


class SigningKeyError(RuntimeError):
    """Stable signing failure that never includes filesystem or key material."""

    def __init__(self, code: SigningKeyErrorCode) -> None:
        self.code = code
        super().__init__("MCP signing key operation failed")


def _b64url_uint(value: int) -> str:
    raw = value.to_bytes(32, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


@dataclass(frozen=True, slots=True)
class PublicEcJwk:
    kid: str
    x: str
    y: str

    def __post_init__(self) -> None:
        if not _valid_key_id(self.kid):
            raise SigningKeyError(SigningKeyErrorCode.PUBLIC_KEY_INVALID)
        x = _decode_coordinate(self.x)
        y = _decode_coordinate(self.y)
        try:
            ec.EllipticCurvePublicNumbers(
                int.from_bytes(x, "big"),
                int.from_bytes(y, "big"),
                ec.SECP256R1(),
            ).public_key()
        except ValueError:
            raise SigningKeyError(SigningKeyErrorCode.PUBLIC_KEY_INVALID) from None

    def to_dict(self) -> dict[str, str]:
        return {
            "alg": "ES256",
            "crv": "P-256",
            "kid": self.kid,
            "kty": "EC",
            "use": "sig",
            "x": self.x,
            "y": self.y,
        }


@dataclass(frozen=True, slots=True)
class SigningKeySnapshot:
    active_kid: str
    keys: tuple[PublicEcJwk, ...]

    def __post_init__(self) -> None:
        key_ids = self.key_ids
        if not self.keys or len(self.keys) > 32 or len(key_ids) != len(self.keys) or self.active_kid not in key_ids:
            raise SigningKeyError(SigningKeyErrorCode.PUBLIC_KEY_INVALID)

    @property
    def key_ids(self) -> frozenset[str]:
        return frozenset(key.kid for key in self.keys)

    def to_jwks_document(self) -> dict[str, list[dict[str, str]]]:
        return {"keys": [key.to_dict() for key in sorted(self.keys, key=lambda item: item.kid)]}


@runtime_checkable
class SigningKeyProvider(Protocol):
    """KMS-compatible signing operation; private bytes never cross this seam."""

    @property
    def snapshot(self) -> SigningKeySnapshot: ...

    def sign_es256(self, signing_input: bytes) -> bytes: ...


def _public_jwk(key: ec.EllipticCurvePublicKey, kid: str) -> PublicEcJwk:
    if not isinstance(key.curve, ec.SECP256R1):
        raise SigningKeyError(SigningKeyErrorCode.PUBLIC_KEY_INVALID)
    numbers = key.public_numbers()
    return PublicEcJwk(kid=kid, x=_b64url_uint(numbers.x), y=_b64url_uint(numbers.y))


def _valid_key_id(value: object) -> bool:
    return type(value) is str and 1 <= len(value) <= 64 and value.isascii() and all(char.isalnum() or char in {"_", "-"} for char in value)


def _decode_coordinate(value: object) -> bytes:
    if type(value) is not str or len(value) != 43 or not value.isascii() or any(char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for char in value):
        raise SigningKeyError(SigningKeyErrorCode.PUBLIC_KEY_INVALID)
    try:
        decoded = base64.b64decode(value + "=", altchars=b"-_", validate=True)
    except (binascii.Error, ValueError):
        raise SigningKeyError(SigningKeyErrorCode.PUBLIC_KEY_INVALID) from None
    if len(decoded) != 32:
        raise SigningKeyError(SigningKeyErrorCode.PUBLIC_KEY_INVALID)
    return decoded


def _read_key_bytes(
    path: Path,
    *,
    failure_code: SigningKeyErrorCode,
    enforce_private_permissions: bool,
) -> bytes:
    if not path.is_absolute() or path.is_symlink():
        raise SigningKeyError(failure_code)
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        raise SigningKeyError(failure_code) from None
    try:
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size <= 0 or metadata.st_size > 65_536:
                raise SigningKeyError(failure_code)
            if enforce_private_permissions and os.name != "nt":
                mode = stat.S_IMODE(metadata.st_mode)
                if mode not in {0o400, 0o600} or metadata.st_uid != os.geteuid():
                    raise SigningKeyError(SigningKeyErrorCode.PRIVATE_KEY_PERMISSIONS_INVALID)
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                raw = stream.read(65_537)
        except OSError:
            raise SigningKeyError(failure_code) from None
    finally:
        os.close(descriptor)
    if not raw or len(raw) > 65_536:
        raise SigningKeyError(failure_code)
    return raw


def _load_private_key(path: Path) -> ec.EllipticCurvePrivateKey:
    raw = _read_key_bytes(
        path,
        failure_code=SigningKeyErrorCode.PRIVATE_KEY_UNAVAILABLE,
        enforce_private_permissions=True,
    )
    try:
        key = serialization.load_pem_private_key(raw, password=None)
    except (TypeError, UnsupportedAlgorithm, ValueError):
        raise SigningKeyError(SigningKeyErrorCode.PRIVATE_KEY_INVALID) from None
    if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
        raise SigningKeyError(SigningKeyErrorCode.PRIVATE_KEY_INVALID)
    return key


def _load_public_key(path: Path) -> ec.EllipticCurvePublicKey:
    raw = _read_key_bytes(
        path,
        failure_code=SigningKeyErrorCode.PUBLIC_KEY_INVALID,
        enforce_private_permissions=False,
    )
    try:
        key = serialization.load_pem_public_key(raw)
    except (TypeError, UnsupportedAlgorithm, ValueError):
        raise SigningKeyError(SigningKeyErrorCode.PUBLIC_KEY_INVALID) from None
    if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1):
        raise SigningKeyError(SigningKeyErrorCode.PUBLIC_KEY_INVALID)
    return key


class FileSigningKeyProvider:
    """Strict P-256 PEM provider for the initial single-process issuer."""

    def __init__(
        self,
        *,
        active_kid: str,
        private_key: ec.EllipticCurvePrivateKey,
        snapshot: SigningKeySnapshot,
    ) -> None:
        self._active_kid = active_kid
        self._private_key = private_key
        self._snapshot = snapshot

    @classmethod
    def from_files(
        cls,
        *,
        active_key_id: str,
        private_key_file: Path,
        public_key_files: dict[str, Path],
    ) -> FileSigningKeyProvider:
        private_path = private_key_file
        if not private_path.is_absolute() or private_path.is_symlink():
            raise SigningKeyError(SigningKeyErrorCode.PRIVATE_KEY_UNAVAILABLE)
        private_key = _load_private_key(private_path)
        public_keys: dict[str, ec.EllipticCurvePublicKey] = {kid: _load_public_key(path) for kid, path in public_key_files.items()}
        active_public = public_keys.get(active_key_id)
        if active_public is None or active_public.public_numbers() != private_key.public_key().public_numbers():
            raise SigningKeyError(SigningKeyErrorCode.ACTIVE_KEY_MISMATCH)
        snapshot = SigningKeySnapshot(
            active_kid=active_key_id,
            keys=tuple(_public_jwk(key, kid) for kid, key in public_keys.items()),
        )
        return cls(active_kid=active_key_id, private_key=private_key, snapshot=snapshot)

    @property
    def snapshot(self) -> SigningKeySnapshot:
        return self._snapshot

    def sign_es256(self, signing_input: bytes) -> bytes:
        try:
            der_signature = self._private_key.sign(
                signing_input,
                ec.ECDSA(hashes.SHA256(), deterministic_signing=True),
            )
            r, s = decode_dss_signature(der_signature)
            return r.to_bytes(32, "big") + s.to_bytes(32, "big")
        except (TypeError, ValueError, OverflowError) as exc:
            raise SigningKeyError(SigningKeyErrorCode.SIGNING_FAILED) from exc

    def __repr__(self) -> str:
        return f"FileSigningKeyProvider(active_kid={self._active_kid!r}, public_key_count={len(self._snapshot.keys)})"


class SigningKeyRotationGuard:
    """In-memory contract guard for publish-before-sign and bounded retirement."""

    def __init__(self, snapshot: SigningKeySnapshot, *, retention_seconds: int) -> None:
        if type(retention_seconds) is not int or retention_seconds <= 0:
            raise SigningKeyError(SigningKeyErrorCode.ROTATION_TIME_INVALID)
        self._snapshot = snapshot
        self._retention_seconds = retention_seconds
        self._retirement_deadlines: dict[str, datetime] = {}

    @property
    def snapshot(self) -> SigningKeySnapshot:
        return self._snapshot

    def transition(self, successor: SigningKeySnapshot, *, now: datetime) -> None:
        if now.tzinfo is None or now.utcoffset() is None:
            raise SigningKeyError(SigningKeyErrorCode.ROTATION_TIME_INVALID)
        deadlines = dict(self._retirement_deadlines)
        current = self._snapshot
        if successor.active_kid != current.active_kid:
            if successor.active_kid not in current.key_ids:
                raise SigningKeyError(SigningKeyErrorCode.NEXT_KEY_NOT_PREPUBLISHED)
            if current.active_kid not in successor.key_ids:
                raise SigningKeyError(SigningKeyErrorCode.ACTIVE_KEY_NOT_RETAINED)
            deadlines[current.active_kid] = now + timedelta(seconds=self._retention_seconds)

        for key_id, deadline in tuple(deadlines.items()):
            if key_id not in successor.key_ids and now < deadline:
                raise SigningKeyError(SigningKeyErrorCode.RETIRED_KEY_REMOVED_EARLY)
            if now >= deadline:
                deadlines.pop(key_id, None)

        self._snapshot = successor
        self._retirement_deadlines = deadlines


__all__ = [
    "FileSigningKeyProvider",
    "PublicEcJwk",
    "SigningKeyError",
    "SigningKeyErrorCode",
    "SigningKeyProvider",
    "SigningKeyRotationGuard",
    "SigningKeySnapshot",
]
