"""Control-plane SecretStore adapter tests."""

import base64
import os

import pytest

from api.channel_control.secret_store import (
    AESGCMChannelSecretStore,
    EncryptedSecret,
    SecretStoreUnavailable,
    UnavailableSecretStore,
    get_channel_secret_store,
)
from common.app_config import AppConfig
from common.channel_secret_crypto import ChannelSecretCipher


@pytest.mark.asyncio
async def test_aesgcm_store_round_trip_preserves_rotation_version() -> None:
    cipher = ChannelSecretCipher(os.urandom(32))
    store = AESGCMChannelSecretStore(cipher)

    encrypted = await store.encrypt(
        tenant_id="tenant-a",
        channel_id="channel-a",
        plaintext={"app_secret": "sensitive-value"},
        version=7,
    )

    assert encrypted.version == 7
    assert encrypted.key_id == cipher.key_id
    assert "sensitive-value" not in encrypted.ciphertext
    assert await store.decrypt(
        tenant_id="tenant-a",
        channel_id="channel-a",
        encrypted=encrypted,
    ) == {"app_secret": "sensitive-value"}


@pytest.mark.asyncio
async def test_store_rejects_unknown_key_id_without_sensitive_error() -> None:
    store = AESGCMChannelSecretStore(ChannelSecretCipher(os.urandom(32)))
    encrypted = EncryptedSecret(
        ciphertext="v1.not-a-real-secret",
        key_id="unknown-key",
        version=1,
    )

    with pytest.raises(SecretStoreUnavailable, match="decryption is unavailable") as captured:
        await store.decrypt(
            tenant_id="tenant-a",
            channel_id="channel-a",
            encrypted=encrypted,
        )

    assert "not-a-real-secret" not in str(captured.value)


def test_dependency_fails_closed_without_configured_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "api.channel_control.secret_store.get_app_config",
        lambda: AppConfig(),
    )

    assert isinstance(get_channel_secret_store(), UnavailableSecretStore)


def test_dependency_builds_aesgcm_store_from_typed_config(monkeypatch: pytest.MonkeyPatch) -> None:
    encoded_key = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")
    config = AppConfig.model_validate({"channels": {"control": {"secret_encryption_key": encoded_key}}})
    monkeypatch.setattr(
        "api.channel_control.secret_store.get_app_config",
        lambda: config,
    )

    assert isinstance(get_channel_secret_store(), AESGCMChannelSecretStore)
