"""Binding-scoped workload tokens for managed Channel child processes."""

from __future__ import annotations

import base64
import hashlib
import hmac

_TOKEN_CONTEXT = b"multirag-channel-binding-v1\x00"
_MAX_GENERATION = 2**31 - 1


def derive_binding_workload_token(
    master_token: str,
    *,
    binding_id: str,
    generation: int,
) -> str:
    """Derive one opaque token fenced to a binding and desired generation."""

    if not master_token:
        raise ValueError("channel workload master token must not be empty")
    if not binding_id:
        raise ValueError("channel binding ID must not be empty")
    if not 1 <= generation <= _MAX_GENERATION:
        raise ValueError("channel binding generation is out of range")
    payload = _TOKEN_CONTEXT + len(binding_id.encode("utf-8")).to_bytes(8, byteorder="big") + binding_id.encode("utf-8") + generation.to_bytes(8, byteorder="big")
    digest = hmac.new(master_token.encode("utf-8"), payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
