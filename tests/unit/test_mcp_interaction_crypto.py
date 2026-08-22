"""Authenticated persistence for sensitive U14 payloads."""

from __future__ import annotations

import base64

import pytest

from api.identity.mcp_interactions.crypto import (
    InteractionPayloadCipher,
    InteractionPayloadCipherError,
)


def _key(byte: int) -> str:
    return base64.urlsafe_b64encode(bytes([byte]) * 32).decode().rstrip("=")


def test_payload_cipher_round_trip_rotation_and_active_key_selection() -> None:
    old = InteractionPayloadCipher.from_base64_keyring([_key(1)])
    encrypted_old = old.encrypt(
        tenant_id="tenant-a",
        interaction_id="interaction-a",
        revision=1,
        purpose="request_state",
        value={"state": "opaque"},
    )
    rotated = InteractionPayloadCipher.from_base64_keyring([_key(2), _key(1)])

    assert rotated.decrypt(
        tenant_id="tenant-a",
        interaction_id="interaction-a",
        revision=1,
        purpose="request_state",
        encrypted=encrypted_old,
    ) == {"state": "opaque"}
    assert (
        rotated.encrypt(
            tenant_id="tenant-a",
            interaction_id="interaction-a",
            revision=1,
            purpose="request_state",
            value={"state": "new"},
        ).key_id
        != encrypted_old.key_id
    )


@pytest.mark.parametrize(
    ("tenant_id", "interaction_id", "revision", "purpose"),
    [
        ("tenant-b", "interaction-a", 1, "request_state"),
        ("tenant-a", "interaction-b", 1, "request_state"),
        ("tenant-a", "interaction-a", 2, "request_state"),
        ("tenant-a", "interaction-a", 1, "input_response"),
    ],
)
def test_payload_cipher_rejects_cross_boundary_replay(
    tenant_id: str,
    interaction_id: str,
    revision: int,
    purpose: str,
) -> None:
    cipher = InteractionPayloadCipher.from_base64_keyring([_key(1)])
    encrypted = cipher.encrypt(
        tenant_id="tenant-a",
        interaction_id="interaction-a",
        revision=1,
        purpose="request_state",
        value={"state": "opaque"},
    )

    with pytest.raises(InteractionPayloadCipherError, match="could not be decrypted"):
        cipher.decrypt(
            tenant_id=tenant_id,
            interaction_id=interaction_id,
            revision=revision,
            purpose=purpose,
            encrypted=encrypted,
        )


def test_payload_cipher_rejects_unknown_key_without_trying_unrelated_keys() -> None:
    encrypted = InteractionPayloadCipher.from_base64_keyring([_key(1)]).encrypt(
        tenant_id="tenant-a",
        interaction_id="interaction-a",
        revision=1,
        purpose="result",
        value={"ok": True},
    )

    with pytest.raises(InteractionPayloadCipherError, match="key is unavailable"):
        InteractionPayloadCipher.from_base64_keyring([_key(2)]).decrypt(
            tenant_id="tenant-a",
            interaction_id="interaction-a",
            revision=1,
            purpose="result",
            encrypted=encrypted,
        )
