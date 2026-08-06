"""Credential self-check seam: ask a provider "is this credential good?".

Deliberately separate from ``api.channels.provider``. That module is the
*worker* seam and resolving it imports a transport SDK; this one is resolved by
the **API process**, which must be able to check a credential without paying
for lark-oapi's process-global event loop. So a verifier is a plain HTTP probe
living in ``api/channels/<name>/verify.py``, imported by name and never
importing its provider's SDK.

Verification is optional. A provider with no verifier is not an error -- it
reports "cannot check" and the admin falls back to enabling the channel and
watching the runtime panel, which is exactly today's behaviour.
"""

from __future__ import annotations

import importlib
from collections.abc import Mapping
from typing import Protocol, runtime_checkable

from api.channel_providers import UnknownChannelProvider, verify_module


class ChannelVerificationError(RuntimeError):
    """Base for self-check failures, carrying a non-sensitive error code."""

    def __init__(self, error_code: str) -> None:
        self.error_code = error_code
        super().__init__(error_code)


class ChannelCredentialRejected(ChannelVerificationError):
    """The provider answered, and the answer was "these credentials are wrong".

    A definite no. This is the whole point of the endpoint: turning a
    tens-of-seconds guess into a two-second fact.
    """

    def __init__(self, error_code: str = "CHANNEL_CREDENTIAL_REJECTED") -> None:
        super().__init__(error_code)


class ChannelVerificationUnavailable(ChannelVerificationError):
    """The check could not be completed, so it says nothing about the credential.

    Kept distinct from rejection on purpose. Reporting a timeout as "your
    App Secret is wrong" would send an admin to re-enter a credential that was
    never the problem.
    """

    def __init__(self, error_code: str = "CHANNEL_VERIFICATION_UNAVAILABLE") -> None:
        super().__init__(error_code)


@runtime_checkable
class CredentialVerifier(Protocol):
    """One provider's credential probe. Returns on success, raises otherwise."""

    async def verify_credential(
        self,
        *,
        credential: Mapping[str, str],
        public_config: Mapping[str, object],
    ) -> None: ...


def credential_verifier(name: str) -> CredentialVerifier | None:
    """Resolve one provider's verifier, or ``None`` when it declares none."""

    try:
        module_path = verify_module(name)
    except UnknownChannelProvider:
        return None
    if module_path is None:
        return None
    verifier = getattr(importlib.import_module(module_path), "CREDENTIAL_VERIFIER", None)
    if not isinstance(verifier, CredentialVerifier):
        return None
    return verifier
