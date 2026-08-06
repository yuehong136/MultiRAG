"""Credential encryption boundary for the channel control plane."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from common.app_config import get_app_config
from common.channel_secret_crypto import ChannelSecretCipher, ChannelSecretCipherError


class SecretStoreUnavailable(RuntimeError):
    """Raised when no approved channel credential cipher is configured."""


@dataclass(frozen=True, slots=True)
class EncryptedSecret:
    ciphertext: str
    key_id: str
    version: int


@runtime_checkable
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
    """Adapter over MultiRAG's row-bound AES-256-GCM credential cipher.

    Holds an ordered key ring rather than one cipher. The first key is active
    and encrypts every new row; every key still on the ring can decrypt the
    rows it wrote. That is what makes rotating the master key survivable:
    put the new key in front, leave the retired one behind it, and credentials
    stored under the retired key keep opening instead of becoming unreadable.

    Rows carry the fingerprint of the key that wrote them, so this is a lookup
    and not a trial-decryption loop -- a wrong key never even gets attempted.
    """

    def __init__(self, *ciphers: ChannelSecretCipher) -> None:
        if not ciphers:
            raise SecretStoreUnavailable("channel credential encryption is unavailable")
        self._cipher = ciphers[0]
        by_key_id: dict[str, ChannelSecretCipher] = {}
        for cipher in ciphers:
            # First occurrence wins so a duplicated key cannot displace the
            # active one as the answer for its own fingerprint.
            by_key_id.setdefault(cipher.key_id, cipher)
        self._by_key_id = by_key_id

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
        cipher = self._by_key_id.get(encrypted.key_id)
        if encrypted.version < 1 or cipher is None:
            raise SecretStoreUnavailable("channel credential decryption is unavailable")
        try:
            return cipher.decrypt(
                tenant_id=tenant_id,
                channel_id=channel_id,
                ciphertext=encrypted.ciphertext,
            )
        except ChannelSecretCipherError as exc:
            raise SecretStoreUnavailable("channel credential decryption is unavailable") from exc


_UNAVAILABLE_SECRET_STORE = UnavailableSecretStore()


def get_channel_secret_store() -> SecretStore:
    """Build the configured store, failing closed when no master key exists."""

    encoded_keys = [key.get_secret_value() for key in get_app_config().channels.control.secret_encryption_key]
    if not encoded_keys:
        return _UNAVAILABLE_SECRET_STORE
    try:
        ciphers = [ChannelSecretCipher.from_base64_key(encoded_key) for encoded_key in encoded_keys]
    except ChannelSecretCipherError:
        # AppConfig validates the same key shape. Keep this defensive boundary
        # non-sensitive in case a custom configuration source bypasses it, and
        # reject the whole ring rather than half of it: a malformed retired key
        # is an operator error that must surface, not be silently skipped.
        return _UNAVAILABLE_SECRET_STORE
    return AESGCMChannelSecretStore(*ciphers)
