"""Authenticated encryption for persisted Channel provider credentials.

The control plane stores only the returned opaque ciphertext.  The tenant and
channel identifiers are bound as associated data so a database row cannot be
copied to another security boundary and still decrypt successfully.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Self

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_AAD_DOMAIN = b"multirag-channel-secret:v1"
_NONCE_BYTES = 12
_VERSION = "v1"


class ChannelSecretCipherError(RuntimeError):
    """A safe, non-sensitive error raised for Channel secret failures."""


@dataclass(frozen=True, slots=True)
class EncryptedChannelSecret:
    """Opaque value suitable for persistence in ``ChannelSecret``."""

    ciphertext: str
    key_id: str


def decode_channel_secret_key(encoded_key: str) -> bytes:
    """Decode and validate a URL-safe base64 AES-256 key.

    Padded and unpadded URL-safe base64 are accepted to make secret-manager
    integration straightforward.  The decoded key must be exactly 32 bytes.
    """

    candidate = encoded_key.strip()
    if not candidate:
        raise ChannelSecretCipherError("channel secret encryption key is not configured")

    padding = "=" * (-len(candidate) % 4)
    try:
        raw = base64.b64decode((candidate + padding).encode("ascii"), altchars=b"-_", validate=True)
    except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
        raise ChannelSecretCipherError("channel secret encryption key is invalid") from exc
    if len(raw) != 32:
        raise ChannelSecretCipherError("channel secret encryption key must contain 32 bytes")
    return raw


class ChannelSecretCipher:
    """AES-256-GCM codec with row-bound authenticated associated data."""

    def __init__(self, key: bytes) -> None:
        if len(key) != 32:
            raise ChannelSecretCipherError("channel secret encryption key must contain 32 bytes")
        self._key = bytes(key)
        self._aead = AESGCM(self._key)
        self._key_id = hashlib.sha256(self._key).hexdigest()[:16]

    @classmethod
    def from_base64_key(cls, encoded_key: str) -> Self:
        """Build a cipher from a secret-manager friendly encoded key."""

        return cls(decode_channel_secret_key(encoded_key))

    @property
    def key_id(self) -> str:
        """Return a non-secret fingerprint used to support future rotation."""

        return self._key_id

    def encrypt(
        self,
        *,
        tenant_id: str,
        channel_id: str,
        values: Mapping[str, str],
    ) -> EncryptedChannelSecret:
        """Encrypt a provider credential mapping for one tenant/channel row."""

        self._validate_boundary(tenant_id=tenant_id, channel_id=channel_id)
        normalized = self._normalize_values(values)
        plaintext = json.dumps(normalized, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
        nonce = os.urandom(_NONCE_BYTES)
        encrypted = self._aead.encrypt(nonce, plaintext, self._aad(tenant_id=tenant_id, channel_id=channel_id))
        payload = base64.urlsafe_b64encode(nonce + encrypted).decode("ascii").rstrip("=")
        return EncryptedChannelSecret(ciphertext=f"{_VERSION}.{payload}", key_id=self._key_id)

    def decrypt(
        self,
        *,
        tenant_id: str,
        channel_id: str,
        ciphertext: str,
    ) -> dict[str, str]:
        """Decrypt one row, rejecting tampering or cross-boundary replay."""

        self._validate_boundary(tenant_id=tenant_id, channel_id=channel_id)
        try:
            version, encoded_payload = ciphertext.split(".", maxsplit=1)
            if version != _VERSION or not encoded_payload:
                raise ValueError
            padding = "=" * (-len(encoded_payload) % 4)
            payload = base64.b64decode(
                (encoded_payload + padding).encode("ascii"),
                altchars=b"-_",
                validate=True,
            )
            if len(payload) <= _NONCE_BYTES:
                raise ValueError
            plaintext = self._aead.decrypt(
                payload[:_NONCE_BYTES],
                payload[_NONCE_BYTES:],
                self._aad(tenant_id=tenant_id, channel_id=channel_id),
            )
            decoded = json.loads(plaintext.decode("utf-8"))
            return self._normalize_values(decoded)
        except (AttributeError, UnicodeEncodeError, UnicodeDecodeError, binascii.Error, InvalidTag, ValueError, json.JSONDecodeError) as exc:
            raise ChannelSecretCipherError("channel secret could not be decrypted") from exc

    @staticmethod
    def _aad(*, tenant_id: str, channel_id: str) -> bytes:
        return b"\x00".join((_AAD_DOMAIN, tenant_id.encode("utf-8"), channel_id.encode("utf-8")))

    @staticmethod
    def _validate_boundary(*, tenant_id: str, channel_id: str) -> None:
        if not tenant_id.strip() or not channel_id.strip():
            raise ChannelSecretCipherError("channel secret boundary is invalid")

    @staticmethod
    def _normalize_values(values: Mapping[str, str] | object) -> dict[str, str]:
        if not isinstance(values, Mapping):
            raise ChannelSecretCipherError("channel secret payload is invalid")
        normalized: dict[str, str] = {}
        for key, value in values.items():
            if not isinstance(key, str) or not key.strip() or not isinstance(value, str):
                raise ChannelSecretCipherError("channel secret payload is invalid")
            normalized[key] = value
        return normalized
