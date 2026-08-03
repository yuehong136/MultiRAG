"""Credential encryption boundary for the channel control plane."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from common.app_config import get_app_config
from common.channel_secret_crypto import ChannelSecretCipher, ChannelSecretCipherError


class SecretStoreUnavailable(RuntimeError):
    """Raised when no approved channel credential cipher is configured."""


@dataclass(frozen=True, slots=True)
class EncryptedSecret:
    ciphertext: str
    key_id: str
    version: int


class SecretStore(Protocol):
    async def encrypt(
        self,
        *,
        tenant_id: str,
        channel_id: str,
        plaintext: Mapping[str, str],
        version: int,
    ) -> EncryptedSecret: ...

    async def decrypt(
        self,
        *,
        tenant_id: str,
        channel_id: str,
        encrypted: EncryptedSecret,
    ) -> Mapping[str, str]: ...


class UnavailableSecretStore:
    """Fail-closed default until an approved cipher adapter is injected."""

    async def encrypt(
        self,
        *,
        tenant_id: str,
        channel_id: str,
        plaintext: Mapping[str, str],
        version: int,
    ) -> EncryptedSecret:
        del tenant_id, channel_id, plaintext, version
        raise SecretStoreUnavailable("channel credential encryption is unavailable")

    async def decrypt(
        self,
        *,
        tenant_id: str,
        channel_id: str,
        encrypted: EncryptedSecret,
    ) -> Mapping[str, str]:
        del tenant_id, channel_id, encrypted
        raise SecretStoreUnavailable("channel credential decryption is unavailable")


class AESGCMChannelSecretStore:
    """Adapter over MultiRAG's row-bound AES-256-GCM credential cipher."""

    def __init__(self, cipher: ChannelSecretCipher) -> None:
        self._cipher = cipher

    async def encrypt(
        self,
        *,
        tenant_id: str,
        channel_id: str,
        plaintext: Mapping[str, str],
        version: int,
    ) -> EncryptedSecret:
        if version < 1:
            raise SecretStoreUnavailable("channel credential encryption is unavailable")
        try:
            encrypted = self._cipher.encrypt(
                tenant_id=tenant_id,
                channel_id=channel_id,
                values=plaintext,
            )
        except ChannelSecretCipherError as exc:
            raise SecretStoreUnavailable("channel credential encryption is unavailable") from exc
        return EncryptedSecret(
            ciphertext=encrypted.ciphertext,
            key_id=encrypted.key_id,
            version=version,
        )

    async def decrypt(
        self,
        *,
        tenant_id: str,
        channel_id: str,
        encrypted: EncryptedSecret,
    ) -> Mapping[str, str]:
        if encrypted.version < 1 or encrypted.key_id != self._cipher.key_id:
            raise SecretStoreUnavailable("channel credential decryption is unavailable")
        try:
            return self._cipher.decrypt(
                tenant_id=tenant_id,
                channel_id=channel_id,
                ciphertext=encrypted.ciphertext,
            )
        except ChannelSecretCipherError as exc:
            raise SecretStoreUnavailable("channel credential decryption is unavailable") from exc


_UNAVAILABLE_SECRET_STORE = UnavailableSecretStore()


def get_channel_secret_store() -> SecretStore:
    """Build the configured store, failing closed when no master key exists."""

    encoded_key = get_app_config().channels.control.secret_encryption_key.get_secret_value()
    if not encoded_key:
        return _UNAVAILABLE_SECRET_STORE
    try:
        return AESGCMChannelSecretStore(ChannelSecretCipher.from_base64_key(encoded_key))
    except ChannelSecretCipherError:
        # AppConfig validates the same key shape. Keep this defensive boundary
        # non-sensitive in case a custom configuration source bypasses it.
        return _UNAVAILABLE_SECRET_STORE
