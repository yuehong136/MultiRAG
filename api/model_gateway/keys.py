"""Caller key format for the model gateway: ``sk-mrag-<key_id>-<secret>``.

``key_id`` is a public 32-character hexadecimal identifier used to find the
stored row. ``secret`` is 43 URL-safe characters carrying 256 random bits.
Only the SHA-256 digest of the whole key is persisted. Parsing is positional
because the secret alphabet itself contains ``-``.
"""

import hashlib
import hmac
import re
import secrets
import uuid
from dataclasses import dataclass, field

KEY_PREFIX = "sk-mrag-"
_KEY_ID = re.compile(r"[0-9a-f]{32}")
_SECRET = re.compile(r"[A-Za-z0-9_-]{43}")
_ID_START = len(KEY_PREFIX)
_ID_END = _ID_START + 32
_KEY_LENGTH = _ID_END + 1 + 43


@dataclass(frozen=True, slots=True)
class IssuedKey:
    """A freshly minted key. ``plaintext`` exists only here and is shown once."""

    key_id: str
    secret_hash: str
    plaintext: str = field(repr=False)


def hash_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def issue_key() -> IssuedKey:
    key_id = uuid.uuid4().hex
    plaintext = f"{KEY_PREFIX}{key_id}-{secrets.token_urlsafe(32)}"
    return IssuedKey(key_id=key_id, secret_hash=hash_key(plaintext), plaintext=plaintext)


def reissue_key(key_id: str) -> IssuedKey:
    """A new secret for an existing key id: what a rotation hands out."""
    plaintext = f"{KEY_PREFIX}{key_id}-{secrets.token_urlsafe(32)}"
    return IssuedKey(key_id=key_id, secret_hash=hash_key(plaintext), plaintext=plaintext)


def parse_key_id(presented: str) -> str | None:
    """Return the key id of a well-formed key, otherwise None. Never raises."""
    if len(presented) != _KEY_LENGTH or not presented.startswith(KEY_PREFIX) or presented[_ID_END] != "-":
        return None
    key_id, secret = presented[_ID_START:_ID_END], presented[_ID_END + 1 :]
    if not _KEY_ID.fullmatch(key_id) or not _SECRET.fullmatch(secret):
        return None
    return key_id


def verify_key(presented: str, secret_hash: str) -> bool:
    """Constant-time check of a well-formed presented key against its stored digest."""
    return hmac.compare_digest(hash_key(presented), secret_hash)


def redact_key(key_id: str) -> str:
    """Display form used after creation: the prefix and the start of the key id."""
    return f"{KEY_PREFIX}{key_id[:8]}..."
