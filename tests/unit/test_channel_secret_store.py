"""Control-plane SecretStore adapter tests."""

import base64
import os

import pytest
from pydantic import SecretStr

from api.channel_control.secret_store import (
    AESGCMChannelSecretStore,
    EncryptedSecret,
    SecretStoreUnavailable,
    UnavailableSecretStore,
    get_channel_secret_store,
)
from common.app_config import AppConfig
from common.channel_secret_crypto import ChannelSecretCipher


def _encoded_key() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")


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
    encoded_key = _encoded_key()
    config = AppConfig.model_validate({"channels": {"control": {"secret_encryption_key": encoded_key}}})
    monkeypatch.setattr(
        "api.channel_control.secret_store.get_app_config",
        lambda: config,
    )

    assert isinstance(get_channel_secret_store(), AESGCMChannelSecretStore)


# --- key ring / master key rotation (CHN-O7) --------------------------------


@pytest.mark.asyncio
async def test_retired_key_left_on_the_ring_still_decrypts_the_rows_it_wrote() -> None:
    """Rotation must not orphan stored credentials.

    This is the whole point of the ring: yesterday's key stays readable after a
    new key takes over encryption, so a leaked key can actually be replaced.
    """

    retired = ChannelSecretCipher(os.urandom(32))
    written_before_rotation = await AESGCMChannelSecretStore(retired).encrypt(
        tenant_id="tenant-a",
        channel_id="channel-a",
        plaintext={"app_secret": "sensitive-value"},
        version=3,
    )

    rotated = AESGCMChannelSecretStore(ChannelSecretCipher(os.urandom(32)), retired)

    assert await rotated.decrypt(
        tenant_id="tenant-a",
        channel_id="channel-a",
        encrypted=written_before_rotation,
    ) == {"app_secret": "sensitive-value"}


@pytest.mark.asyncio
async def test_first_key_on_the_ring_is_the_one_that_encrypts() -> None:
    active = ChannelSecretCipher(os.urandom(32))
    retired = ChannelSecretCipher(os.urandom(32))
    store = AESGCMChannelSecretStore(active, retired)

    encrypted = await store.encrypt(
        tenant_id="tenant-a",
        channel_id="channel-a",
        plaintext={"app_secret": "sensitive-value"},
        version=1,
    )

    assert encrypted.key_id == active.key_id
    assert encrypted.key_id != retired.key_id


@pytest.mark.asyncio
async def test_dropping_a_key_from_the_ring_fails_closed_instead_of_returning_empty() -> None:
    """Retiring a key for real must be loud, not a silently empty credential."""

    retired = ChannelSecretCipher(os.urandom(32))
    orphaned = await AESGCMChannelSecretStore(retired).encrypt(
        tenant_id="tenant-a",
        channel_id="channel-a",
        plaintext={"app_secret": "sensitive-value"},
        version=1,
    )

    without_retired_key = AESGCMChannelSecretStore(ChannelSecretCipher(os.urandom(32)))

    with pytest.raises(SecretStoreUnavailable, match="decryption is unavailable"):
        await without_retired_key.decrypt(
            tenant_id="tenant-a",
            channel_id="channel-a",
            encrypted=orphaned,
        )


def test_empty_key_ring_is_rejected_at_construction() -> None:
    with pytest.raises(SecretStoreUnavailable, match="encryption is unavailable"):
        AESGCMChannelSecretStore()


@pytest.mark.asyncio
async def test_dependency_builds_the_ring_in_configured_order(monkeypatch: pytest.MonkeyPatch) -> None:
    active_key, retired_key = _encoded_key(), _encoded_key()
    config = AppConfig.model_validate({"channels": {"control": {"secret_encryption_key": [active_key, retired_key]}}})
    monkeypatch.setattr("api.channel_control.secret_store.get_app_config", lambda: config)

    store = get_channel_secret_store()
    assert isinstance(store, AESGCMChannelSecretStore)

    written_under_retired_key = await AESGCMChannelSecretStore(ChannelSecretCipher.from_base64_key(retired_key)).encrypt(
        tenant_id="tenant-a",
        channel_id="channel-a",
        plaintext={"app_secret": "sensitive-value"},
        version=1,
    )
    encrypted = await store.encrypt(
        tenant_id="tenant-a",
        channel_id="channel-a",
        plaintext={"app_secret": "sensitive-value"},
        version=1,
    )

    assert encrypted.key_id == ChannelSecretCipher.from_base64_key(active_key).key_id
    assert await store.decrypt(
        tenant_id="tenant-a",
        channel_id="channel-a",
        encrypted=written_under_retired_key,
    ) == {"app_secret": "sensitive-value"}


def test_dependency_fails_closed_when_any_key_on_the_ring_is_malformed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bad retired key is an operator error, not something to skip past.

    AppConfig rejects this shape, so reaching it means a configuration source
    bypassed validation -- refuse the whole ring rather than half-applying it.
    """

    config = AppConfig.model_validate({"channels": {"control": {"secret_encryption_key": _encoded_key()}}})
    config.channels.control.secret_encryption_key.append(SecretStr("not-base64!"))
    monkeypatch.setattr("api.channel_control.secret_store.get_app_config", lambda: config)

    assert isinstance(get_channel_secret_store(), UnavailableSecretStore)
