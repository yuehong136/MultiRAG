"""Authenticated encryption for persisted MCP interaction payloads."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Self

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from common.mcp_interactions import decode_interaction_payload_key

_AAD_DOMAIN = b"multirag.mcp-interaction.payload:v1"
_NONCE_BYTES = 12
_VERSION = "v1"
_PURPOSES = frozenset(
    {
        "input_requests",
        "input_response",
        "channel_callback",
        "form_mapping",
        "original_arguments",
        "output_schema",
        "principal_evidence",
        "request_state",
        "result",
    }
)


class InteractionPayloadCipherError(RuntimeError):
    """Stable, non-sensitive interaction encryption failure."""


@dataclass(frozen=True, slots=True)
class EncryptedInteractionPayload:
    ciphertext: str = field(repr=False)
    key_id: str


class InteractionPayloadCipher:
    """AES-256-GCM key ring with row/revision/purpose-bound AAD."""

    def __init__(self, keys: Sequence[bytes]) -> None:
        if not keys or any(len(key) != 32 for key in keys):
            raise InteractionPayloadCipherError("interaction payload key ring is invalid")
        keyed: dict[str, AESGCM] = {}
        for key in keys:
            immutable = bytes(key)
            key_id = hashlib.sha256(immutable).hexdigest()[:16]
            if key_id in keyed:
                raise InteractionPayloadCipherError("interaction payload keys must be unique")
            keyed[key_id] = AESGCM(immutable)
        self._keys = keyed
        self._active_key_id = next(iter(keyed))

    @classmethod
    def from_base64_keyring(cls, encoded_keys: Sequence[str]) -> Self:
        try:
            return cls([decode_interaction_payload_key(value) for value in encoded_keys])
        except ValueError as exc:
            raise InteractionPayloadCipherError("interaction payload key is invalid") from exc

    def encrypt(
        self,
        *,
        tenant_id: str,
        interaction_id: str,
        revision: int,
        purpose: str,
        value: Any,
    ) -> EncryptedInteractionPayload:
        aad = self._aad(
            tenant_id=tenant_id,
            interaction_id=interaction_id,
            revision=revision,
            purpose=purpose,
        )
        try:
            plaintext = json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        except (TypeError, ValueError) as exc:
            raise InteractionPayloadCipherError("interaction payload is invalid") from exc
        nonce = os.urandom(_NONCE_BYTES)
        ciphertext = self._keys[self._active_key_id].encrypt(nonce, plaintext, aad)
        encoded = base64.urlsafe_b64encode(nonce + ciphertext).decode().rstrip("=")
        return EncryptedInteractionPayload(
            ciphertext=f"{_VERSION}.{encoded}",
            key_id=self._active_key_id,
        )

    def decrypt(
        self,
        *,
        tenant_id: str,
        interaction_id: str,
        revision: int,
        purpose: str,
        encrypted: EncryptedInteractionPayload,
    ) -> Any:
        aead = self._keys.get(encrypted.key_id)
        if aead is None:
            raise InteractionPayloadCipherError("interaction payload key is unavailable")
        aad = self._aad(
            tenant_id=tenant_id,
            interaction_id=interaction_id,
            revision=revision,
            purpose=purpose,
        )
        try:
            version, encoded = encrypted.ciphertext.split(".", maxsplit=1)
            if version != _VERSION or not encoded:
                raise ValueError
            padding = "=" * (-len(encoded) % 4)
            payload = base64.b64decode(
                (encoded + padding).encode("ascii"),
                altchars=b"-_",
                validate=True,
            )
            if len(payload) <= _NONCE_BYTES:
                raise ValueError
            plaintext = aead.decrypt(payload[:_NONCE_BYTES], payload[_NONCE_BYTES:], aad)
            return json.loads(plaintext.decode())
        except (
            AttributeError,
            UnicodeEncodeError,
            UnicodeDecodeError,
            binascii.Error,
            InvalidTag,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            raise InteractionPayloadCipherError("interaction payload could not be decrypted") from exc

    @staticmethod
    def _aad(
        *,
        tenant_id: str,
        interaction_id: str,
        revision: int,
        purpose: str,
    ) -> bytes:
        if not tenant_id.strip() or not interaction_id.strip() or type(revision) is not int or revision <= 0 or purpose not in _PURPOSES:
            raise InteractionPayloadCipherError("interaction payload boundary is invalid")
        return b"\x00".join(
            (
                _AAD_DOMAIN,
                tenant_id.encode(),
                interaction_id.encode(),
                str(revision).encode(),
                purpose.encode(),
            )
        )


__all__ = [
    "EncryptedInteractionPayload",
    "InteractionPayloadCipher",
    "InteractionPayloadCipherError",
]
