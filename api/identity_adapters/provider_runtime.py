"""Process-retained enterprise identity provider composition.

The provider instances own bounded credential, token, and directory caches.
All in-process consumers therefore share this one registry instead of building
parallel cache islands for Channel ingress and background identity work.
"""

from __future__ import annotations

from collections.abc import Callable
from threading import Lock
from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.channel_control.secret_store import get_channel_secret_store
from api.identity.providers.feishu import FeishuEnterpriseIdentityProvider
from api.identity_adapters.channel_credentials import (
    SessionFactoryChannelProviderCredentialResolver,
    SessionFactoryReconciliationProviderCredentialResolver,
)
from api.identity_adapters.channel_runtime import IdentityProviderRegistry

IdentityProviderRegistryBuilder = Callable[[async_sessionmaker[AsyncSession]], IdentityProviderRegistry]


class IdentityProviderRuntimeUnavailableError(RuntimeError):
    """Raised when provider composition has no active async DB lifecycle."""


_identity_provider_registry_lock = Lock()
_identity_provider_registry: IdentityProviderRegistry | None = None
_identity_provider_registry_session_factory: object | None = None


def build_identity_provider_registry(
    session_factory: async_sessionmaker[AsyncSession],
) -> IdentityProviderRegistry:
    """Compose providers over the active DB lifecycle and Channel secrets."""

    secret_store = get_channel_secret_store()
    credential_resolver = SessionFactoryChannelProviderCredentialResolver(
        session_factory,
        secret_store,
    )
    reconciliation_credential_resolver = SessionFactoryReconciliationProviderCredentialResolver(
        session_factory,
        secret_store,
    )
    return IdentityProviderRegistry(
        {
            "feishu": lambda: FeishuEnterpriseIdentityProvider(
                credential_resolver,
                reconciliation_credential_resolver=reconciliation_credential_resolver,
            ),
        },
    )


def _current_async_session_factory() -> async_sessionmaker[AsyncSession]:
    from api.db import db_models

    session_factory = db_models.async_session_factory
    if session_factory is None:
        raise IdentityProviderRuntimeUnavailableError("identity provider runtime is unavailable")
    return session_factory


def get_identity_provider_registry(
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    *,
    builder: IdentityProviderRegistryBuilder | None = None,
) -> IdentityProviderRegistry:
    """Return one registry for the active DB lifecycle.

    FastAPI may execute synchronous dependencies concurrently in its thread
    pool. ``functools.lru_cache`` permits duplicate cold-miss executions, so a
    lock protects construction of both the registry and its lazy providers.
    A failed construction is never retained and remains retryable.
    """

    active_session_factory = session_factory or _current_async_session_factory()
    selected_builder = builder or build_identity_provider_registry
    return get_identity_provider_registry_for_session_factory(
        active_session_factory,
        builder=cast("Callable[[object], IdentityProviderRegistry]", selected_builder),
    )


def get_identity_provider_registry_for_session_factory(
    session_factory: object,
    *,
    builder: Callable[[object], IdentityProviderRegistry],
) -> IdentityProviderRegistry:
    """Share the singleton with an already-validated lifecycle.

    This compatibility boundary preserves injectable Channel composition.
    New identity callers should use :func:`get_identity_provider_registry`,
    whose public session-factory input remains strictly typed.
    """

    global _identity_provider_registry
    global _identity_provider_registry_session_factory

    registry = _identity_provider_registry
    if registry is not None and _identity_provider_registry_session_factory is session_factory:
        return registry
    with _identity_provider_registry_lock:
        registry = _identity_provider_registry
        if registry is None or _identity_provider_registry_session_factory is not session_factory:
            registry = builder(session_factory)
            _identity_provider_registry = registry
            _identity_provider_registry_session_factory = session_factory
        return registry


def reset_identity_provider_registry_for_testing() -> None:
    """Drop retained process state at an explicit application/test boundary."""

    global _identity_provider_registry
    global _identity_provider_registry_session_factory

    with _identity_provider_registry_lock:
        _identity_provider_registry = None
        _identity_provider_registry_session_factory = None


__all__ = [
    "IdentityProviderRuntimeUnavailableError",
    "build_identity_provider_registry",
    "get_identity_provider_registry",
    "get_identity_provider_registry_for_session_factory",
    "reset_identity_provider_registry_for_testing",
]
