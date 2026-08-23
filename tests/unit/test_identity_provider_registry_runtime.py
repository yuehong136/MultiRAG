"""Focused tests for the shared enterprise identity provider runtime."""

from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.channel_control.secret_store import UnavailableSecretStore
from api.identity.contracts import ProviderContext
from api.identity.providers.contracts import (
    ExternalIdentityAssertion,
    ProviderIdentityResult,
)
from api.identity_adapters import provider_runtime
from api.identity_adapters.channel_credentials import (
    SessionFactoryChannelProviderCredentialResolver,
    SessionFactoryReconciliationProviderCredentialResolver,
)
from api.identity_adapters.channel_runtime import IdentityProviderRegistry
from api.identity_adapters.provider_runtime import (
    IdentityProviderRuntimeUnavailableError,
    get_identity_provider_registry,
    reset_identity_provider_registry_for_testing,
)


@pytest.fixture(autouse=True)
def _reset_provider_runtime() -> Iterator[None]:
    reset_identity_provider_registry_for_testing()
    yield
    reset_identity_provider_registry_for_testing()


def _registry_builder(
    builds: list[async_sessionmaker[AsyncSession]],
) -> Callable[[async_sessionmaker[AsyncSession]], IdentityProviderRegistry]:
    def _build(session_factory: async_sessionmaker[AsyncSession]) -> IdentityProviderRegistry:
        builds.append(session_factory)
        return IdentityProviderRegistry({})

    return _build


class _Provider:
    async def resolve(
        self,
        context: ProviderContext,
        assertion: ExternalIdentityAssertion,
    ) -> ProviderIdentityResult:
        del context, assertion
        raise AssertionError("composition test must not call the provider")

    async def refresh(
        self,
        context: ProviderContext,
        provider_user_id: str,
    ) -> ProviderIdentityResult:
        del context, provider_user_id
        raise AssertionError("composition test must not call the provider")


def test_registry_is_retained_only_for_same_session_factory() -> None:
    first_factory = async_sessionmaker()
    second_factory = async_sessionmaker()
    builds: list[async_sessionmaker[AsyncSession]] = []
    builder = _registry_builder(builds)

    first = get_identity_provider_registry(first_factory, builder=builder)
    repeated = get_identity_provider_registry(first_factory, builder=builder)
    second = get_identity_provider_registry(second_factory, builder=builder)

    assert repeated is first
    assert second is not first
    assert builds == [first_factory, second_factory]


def test_reset_drops_retained_registry() -> None:
    session_factory = async_sessionmaker()
    builds: list[async_sessionmaker[AsyncSession]] = []
    builder = _registry_builder(builds)

    first = get_identity_provider_registry(session_factory, builder=builder)
    reset_identity_provider_registry_for_testing()
    second = get_identity_provider_registry(session_factory, builder=builder)

    assert second is not first
    assert builds == [session_factory, session_factory]


def test_failed_construction_is_not_retained() -> None:
    session_factory = async_sessionmaker()
    attempts = 0

    def _flaky_builder(
        active_session_factory: async_sessionmaker[AsyncSession],
    ) -> IdentityProviderRegistry:
        nonlocal attempts
        assert active_session_factory is session_factory
        attempts += 1
        if attempts == 1:
            raise RuntimeError("safe construction failure")
        return IdentityProviderRegistry({})

    with pytest.raises(RuntimeError, match="safe construction failure"):
        get_identity_provider_registry(session_factory, builder=_flaky_builder)

    registry = get_identity_provider_registry(session_factory, builder=_flaky_builder)

    assert isinstance(registry, IdentityProviderRegistry)
    assert attempts == 2


def test_default_composition_reads_current_async_session_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from api.db import db_models

    session_factory = async_sessionmaker()
    builds: list[async_sessionmaker[AsyncSession]] = []
    monkeypatch.setattr(db_models, "async_session_factory", session_factory)

    registry = get_identity_provider_registry(builder=_registry_builder(builds))

    assert isinstance(registry, IdentityProviderRegistry)
    assert builds == [session_factory]


def test_default_composition_injects_separate_foreground_and_reconciliation_resolvers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[tuple[object, object]] = []

    def _provider_factory(
        credential_resolver: object,
        *,
        reconciliation_credential_resolver: object,
    ) -> _Provider:
        captured.append(
            (
                credential_resolver,
                reconciliation_credential_resolver,
            )
        )
        return _Provider()

    monkeypatch.setattr(
        provider_runtime,
        "get_channel_secret_store",
        lambda: UnavailableSecretStore(),
    )
    monkeypatch.setattr(
        provider_runtime,
        "FeishuEnterpriseIdentityProvider",
        _provider_factory,
    )

    registry = provider_runtime.build_identity_provider_registry(async_sessionmaker())
    provider = registry.get("feishu")

    assert isinstance(provider, _Provider)
    assert len(captured) == 1
    foreground, reconciliation = captured[0]
    assert isinstance(foreground, SessionFactoryChannelProviderCredentialResolver)
    assert isinstance(
        reconciliation,
        SessionFactoryReconciliationProviderCredentialResolver,
    )
    assert foreground is not reconciliation


def test_default_composition_fails_closed_without_async_db(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from api.db import db_models

    monkeypatch.setattr(db_models, "async_session_factory", None)

    with pytest.raises(
        IdentityProviderRuntimeUnavailableError,
        match="identity provider runtime is unavailable",
    ):
        get_identity_provider_registry()
