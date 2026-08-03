"""Security properties of Channel credential encryption."""

import base64
import os

import pytest

from common.channel_secret_crypto import ChannelSecretCipher, ChannelSecretCipherError


def _encoded_key() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")


def test_channel_secret_round_trip_and_key_fingerprint() -> None:
    cipher = ChannelSecretCipher.from_base64_key(_encoded_key())

    encrypted = cipher.encrypt(
        tenant_id="tenant-a",
        channel_id="channel-a",
        values={"app_id": "cli_demo", "app_secret": "sensitive-value"},
    )

    assert encrypted.ciphertext.startswith("v1.")
    assert encrypted.key_id == cipher.key_id
    assert "sensitive-value" not in encrypted.ciphertext
    assert cipher.decrypt(
        tenant_id="tenant-a",
        channel_id="channel-a",
        ciphertext=encrypted.ciphertext,
    ) == {"app_id": "cli_demo", "app_secret": "sensitive-value"}


@pytest.mark.parametrize(
    "tenant_id,channel_id",
    [("tenant-b", "channel-a"), ("tenant-a", "channel-b")],
)
def test_ciphertext_is_bound_to_tenant_and_channel(tenant_id: str, channel_id: str) -> None:
    cipher = ChannelSecretCipher.from_base64_key(_encoded_key())
    encrypted = cipher.encrypt(
        tenant_id="tenant-a",
        channel_id="channel-a",
        values={"app_secret": "must-not-leak"},
    )

    with pytest.raises(ChannelSecretCipherError, match="could not be decrypted") as captured:
        cipher.decrypt(tenant_id=tenant_id, channel_id=channel_id, ciphertext=encrypted.ciphertext)

    assert "must-not-leak" not in str(captured.value)


def test_ciphertext_tampering_fails_closed_without_secret_in_error() -> None:
    cipher = ChannelSecretCipher.from_base64_key(_encoded_key())
    encrypted = cipher.encrypt(
        tenant_id="tenant-a",
        channel_id="channel-a",
        values={"app_secret": "must-not-leak"},
    )
    version, encoded_payload = encrypted.ciphertext.split(".", maxsplit=1)
    payload = bytearray(base64.urlsafe_b64decode(encoded_payload + "=" * (-len(encoded_payload) % 4)))
    payload[-1] ^= 1
    tampered = f"{version}.{base64.urlsafe_b64encode(payload).decode('ascii').rstrip('=')}"

    with pytest.raises(ChannelSecretCipherError, match="could not be decrypted") as captured:
        cipher.decrypt(
            tenant_id="tenant-a",
            channel_id="channel-a",
            ciphertext=tampered,
        )

    assert "must-not-leak" not in str(captured.value)


@pytest.mark.parametrize(
    "encoded",
    ["", "not-base64!", base64.urlsafe_b64encode(b"too-short").decode("ascii")],
)
def test_invalid_encryption_key_is_rejected(encoded: str) -> None:
    with pytest.raises(ChannelSecretCipherError, match="encryption key"):
        ChannelSecretCipher.from_base64_key(encoded)


def test_payload_requires_string_keys_and_values() -> None:
    cipher = ChannelSecretCipher.from_base64_key(_encoded_key())

    with pytest.raises(ChannelSecretCipherError, match="payload is invalid"):
        cipher.encrypt(tenant_id="tenant-a", channel_id="channel-a", values={"secret": 123})  # type: ignore[dict-item]


def test_empty_security_boundary_is_rejected() -> None:
    cipher = ChannelSecretCipher.from_base64_key(_encoded_key())

    with pytest.raises(ChannelSecretCipherError, match="boundary is invalid"):
        cipher.encrypt(tenant_id="", channel_id="channel-a", values={"secret": "value"})
