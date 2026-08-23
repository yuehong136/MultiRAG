"""Fail-closed tests for the temporary Channel-backed I4 credential seam."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from types import TracebackType
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.channel_control.secret_store import EncryptedSecret, SecretStoreUnavailable
from api.identity.contracts import ProviderContext
from api.identity.providers.contracts import (
    FeishuDomain,
    FeishuProviderCredential,
    ProviderCredentialError,
    ProviderCredentialResolver,
    ProviderErrorCode,
)
from api.identity_adapters.channel_credentials import (
    ChannelProviderCredentialResolver,
    SessionFactoryChannelProviderCredentialResolver,
    SessionFactoryReconciliationProviderCredentialResolver,
)

_NOW = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
_SENSITIVE = "must-never-appear"


def _context(**changes: object) -> ProviderContext:
    return replace(
        ProviderContext(
            tenant_id="tenant-unit",
            provider="feishu",
            provider_tenant_key="tenant-key-sensitive",
            provider_account_id="account-unit",
            provider_account_key="cli_unit",
            provider_account_revision=7,
            provider_account_last_scope_change_at=_NOW,
        ),
        **changes,
    )


def _row(**changes: object) -> tuple[object, ...]:
    values: dict[str, object] = {
        "account_id": "account-unit",
        "account_key": "cli_unit",
        "health_state": "healthy",
        "link_tenant_id": "tenant-unit",
        "link_provider": "feishu",
        "channel_id": "channel-unit",
        "channel_tenant_id": "tenant-unit",
        "channel_provider": "feishu",
        "channel_config": {
            "credential": {"app_id": "cli_unit"},
            "domain": "feishu",
        },
        "ciphertext": "ciphertext-unit",
        "key_id": "key-unit",
        "version": 3,
    }
    values.update(changes)
    return tuple(values.values())


class _Rows:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self._rows = rows

    def all(self) -> list[tuple[object, ...]]:
        return self._rows


class _SecretStore:
    def __init__(
        self,
        plaintext: Mapping[str, str] | None = None,
        *,
        error: BaseException | None = None,
    ) -> None:
        self.plaintext = plaintext or {"app_secret": _SENSITIVE}
        self.error = error
        self.calls: list[tuple[str, str, EncryptedSecret]] = []

    async def encrypt(
        self,
        *,
        tenant_id: str,
        channel_id: str,
        plaintext: Mapping[str, str],
        version: int,
    ) -> EncryptedSecret:
        del tenant_id, channel_id, plaintext, version
        raise AssertionError("credential resolution must never encrypt")

    async def decrypt(
        self,
        *,
        tenant_id: str,
        channel_id: str,
        encrypted: EncryptedSecret,
    ) -> Mapping[str, str]:
        self.calls.append((tenant_id, channel_id, encrypted))
        if self.error is not None:
            raise self.error
        return self.plaintext


class _TrackedSessionContext:
    def __init__(
        self,
        session: AsyncSession,
        state: dict[str, bool],
    ) -> None:
        self._session = session
        self._state = state

    async def __aenter__(self) -> AsyncSession:
        self._state["open"] = True
        return self._session

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        self._state["open"] = False


class _TrackedSessionFactory(async_sessionmaker[AsyncSession]):
    def __init__(
        self,
        session: AsyncSession,
        state: dict[str, bool],
    ) -> None:
        super().__init__()
        self._session = session
        self._state = state

    def __call__(self) -> _TrackedSessionContext:
        return _TrackedSessionContext(self._session, self._state)


class _ExitAwareSecretStore(_SecretStore):
    def __init__(
        self,
        state: dict[str, bool],
        *,
        error: BaseException | None = None,
    ) -> None:
        super().__init__(error=error)
        self._state = state

    async def decrypt(
        self,
        *,
        tenant_id: str,
        channel_id: str,
        encrypted: EncryptedSecret,
    ) -> Mapping[str, str]:
        assert self._state["open"] is False
        return await super().decrypt(
            tenant_id=tenant_id,
            channel_id=channel_id,
            encrypted=encrypted,
        )


def _resolver(
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    rows: list[tuple[object, ...]],
    *,
    store: _SecretStore | None = None,
) -> tuple[ChannelProviderCredentialResolver, AsyncMock, _SecretStore]:
    execute = AsyncMock(return_value=_Rows(rows))
    monkeypatch.setattr(db, "execute", execute)
    secret_store = store or _SecretStore()
    return ChannelProviderCredentialResolver(db, secret_store), execute, secret_store


async def test_resolver_reads_one_exact_link_and_returns_a_redacted_short_lived_value(
    async_db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver, execute, store = _resolver(async_db, monkeypatch, [_row()])

    credential = await resolver.resolve(_context())

    assert isinstance(resolver, ProviderCredentialResolver)
    assert credential == FeishuProviderCredential(
        provider_account_id="account-unit",
        app_id="cli_unit",
        app_secret=_SENSITIVE,
        domain=FeishuDomain.FEISHU,
        credential_generation=3,
    )
    assert store.calls == [
        (
            "tenant-unit",
            "channel-unit",
            EncryptedSecret(
                ciphertext="ciphertext-unit",
                key_id="key-unit",
                version=3,
            ),
        )
    ]
    assert execute.await_count == 1
    statement = execute.await_args.args[0]
    assert statement._limit_clause.value == 2
    rendered = str(statement)
    assert "t_ai_identity_provider_accounts" in rendered
    assert "t_ai_identity_provider_channel_links" in rendered
    assert "t_ai_chat_channels" in rendered
    assert "t_ai_channel_secrets" in rendered
    assert _SENSITIVE not in repr(credential)
    assert "account-unit" not in repr(credential)
    assert "cli_unit" not in repr(credential)


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [_row(), _row(channel_id="channel-other")],
        [_row(health_state="disabled")],
        [_row(account_id="account-other")],
        [_row(account_key="cli_other")],
        [_row(link_tenant_id="tenant-other")],
        [_row(link_provider="dingtalk")],
        [_row(channel_tenant_id="tenant-other")],
        [_row(channel_provider="dingtalk")],
        [_row(ciphertext=None)],
        [_row(key_id="")],
        [_row(version=0)],
        [_row(channel_config={"credential": {"app_id": "cli_other"}, "domain": "feishu"})],
        [_row(channel_config={"credential": {"app_id": "cli_unit"}})],
        [_row(channel_config={"credential": {"app_id": "cli_unit"}, "domain": "unknown"})],
        [_row(channel_config={"credential": {"app_id": "cli_unit", "app_secret": _SENSITIVE}, "domain": "feishu"})],
    ],
)
async def test_missing_ambiguous_disabled_or_scope_mismatched_rows_fail_closed_without_decryption(
    async_db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    rows: list[tuple[object, ...]],
) -> None:
    resolver, _execute, store = _resolver(async_db, monkeypatch, rows)

    with pytest.raises(ProviderCredentialError) as caught:
        await resolver.resolve(_context())

    assert caught.value.code is ProviderErrorCode.CREDENTIAL_UNAVAILABLE
    assert str(caught.value) == "IDENTITY_PROVIDER_CREDENTIAL_UNAVAILABLE"
    assert store.calls == []
    assert _SENSITIVE not in repr(caught.value)
    assert _SENSITIVE not in str(caught.value)


async def test_task_cancellation_is_not_mapped_to_a_credential_error(
    async_db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _SecretStore(error=asyncio.CancelledError())
    resolver, _execute, _store = _resolver(async_db, monkeypatch, [_row()], store=store)

    with pytest.raises(asyncio.CancelledError):
        await resolver.resolve(_context())


@pytest.mark.parametrize(
    "context",
    [
        _context(provider="dingtalk"),
        _context(tenant_id=""),
        _context(provider_account_revision=0),
        _context(provider_account_last_scope_change_at=_NOW.replace(tzinfo=None)),
    ],
)
async def test_invalid_or_non_feishu_context_fails_before_database_access(
    async_db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    context: ProviderContext,
) -> None:
    resolver, execute, store = _resolver(async_db, monkeypatch, [_row()])

    with pytest.raises(ProviderCredentialError) as caught:
        await resolver.resolve(context)

    assert caught.value.code is ProviderErrorCode.CREDENTIAL_UNAVAILABLE
    execute.assert_not_awaited()
    assert store.calls == []


@pytest.mark.parametrize(
    "store",
    [
        _SecretStore({"unrelated": _SENSITIVE}),
        _SecretStore({"app_secret": ""}),
        _SecretStore({"app_secret": "x" * 4_097}),
        _SecretStore(error=SecretStoreUnavailable(_SENSITIVE)),
        _SecretStore(error=RuntimeError(_SENSITIVE)),
    ],
)
async def test_missing_or_undecryptable_app_secret_is_a_stable_redacted_failure(
    async_db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    store: _SecretStore,
) -> None:
    resolver, _execute, _store = _resolver(async_db, monkeypatch, [_row()], store=store)

    with pytest.raises(ProviderCredentialError) as caught:
        await resolver.resolve(_context())

    assert caught.value.code is ProviderErrorCode.CREDENTIAL_UNAVAILABLE
    assert _SENSITIVE not in str(caught.value)
    assert _SENSITIVE not in repr(caught.value)


@pytest.mark.parametrize("health_state", ["pending", "healthy", "degraded", "error"])
async def test_non_disabled_health_states_can_use_the_credential_to_probe_or_recover(
    async_db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    health_state: str,
) -> None:
    resolver, _execute, store = _resolver(async_db, monkeypatch, [_row(health_state=health_state)])

    credential = await resolver.resolve(_context())

    assert credential.app_id == "cli_unit"
    assert len(store.calls) == 1


async def test_database_errors_are_mapped_without_sql_or_bound_identifiers(
    async_db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execute = AsyncMock(
        side_effect=OperationalError(
            "SELECT secret",
            {"app_secret": _SENSITIVE},
            RuntimeError(_SENSITIVE),
        )
    )
    monkeypatch.setattr(async_db, "execute", execute)
    resolver = ChannelProviderCredentialResolver(async_db, _SecretStore())

    with pytest.raises(ProviderCredentialError) as caught:
        await resolver.resolve(_context())

    assert caught.value.code is ProviderErrorCode.CREDENTIAL_UNAVAILABLE
    assert _SENSITIVE not in str(caught.value)
    assert _SENSITIVE not in repr(caught.value)


async def test_legacy_nested_domain_is_accepted_without_becoming_a_secret_source(
    async_db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _row(
        channel_config={
            "credential": {
                "app_id": "cli_unit",
                "domain": "lark",
            }
        }
    )
    resolver, _execute, _store = _resolver(async_db, monkeypatch, [row])

    credential = await resolver.resolve(_context())

    assert credential.domain is FeishuDomain.LARK
    assert _SENSITIVE not in repr(credential)


def test_credential_contract_redacts_all_account_and_secret_material() -> None:
    credential = FeishuProviderCredential(
        provider_account_id=_SENSITIVE,
        app_id=_SENSITIVE,
        app_secret=_SENSITIVE,
        domain=FeishuDomain.FEISHU,
        credential_generation=9,
    )

    assert repr(credential) == "FeishuProviderCredential(credential_generation=9, domain=<FeishuDomain.FEISHU: 'feishu'>)"
    assert _SENSITIVE not in repr(credential)
    assert not hasattr(credential, "__dict__")


async def test_secret_rotation_advances_the_credential_generation(
    async_db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver, execute, _store = _resolver(async_db, monkeypatch, [_row(version=3)])

    before = await resolver.resolve(_context())
    execute.return_value = _Rows([_row(version=4, ciphertext="rotated-ciphertext")])
    after = await resolver.resolve(_context())

    assert before.credential_generation == 3
    assert after.credential_generation == 4
    assert after.app_secret == before.app_secret


def test_test_double_has_the_same_secret_store_shape() -> None:
    """Keep the unit double honest without importing a real cipher."""

    store: Any = _SecretStore()
    assert callable(store.encrypt)
    assert callable(store.decrypt)


async def test_factory_resolver_closes_db_session_before_decrypt(
    async_db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = {"open": False}
    execute = AsyncMock(return_value=_Rows([_row()]))
    monkeypatch.setattr(async_db, "execute", execute)
    store = _ExitAwareSecretStore(state)
    resolver = SessionFactoryChannelProviderCredentialResolver(
        _TrackedSessionFactory(async_db, state),
        store,
    )

    credential = await resolver.resolve(_context())

    assert state["open"] is False
    assert credential.credential_generation == 3
    assert _SENSITIVE not in repr(credential)


@pytest.mark.parametrize("health_state", ["pending", "degraded", "error", "disabled"])
async def test_factory_resolver_requires_exact_healthy_before_decrypt(
    async_db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    health_state: str,
) -> None:
    state = {"open": False}
    execute = AsyncMock(return_value=_Rows([_row(health_state=health_state)]))
    monkeypatch.setattr(async_db, "execute", execute)
    store = _ExitAwareSecretStore(state)
    resolver = SessionFactoryChannelProviderCredentialResolver(
        _TrackedSessionFactory(async_db, state),
        store,
    )

    with pytest.raises(ProviderCredentialError) as caught:
        await resolver.resolve(_context())

    assert caught.value.code is ProviderErrorCode.CREDENTIAL_UNAVAILABLE
    assert state["open"] is False
    assert store.calls == []


@pytest.mark.parametrize("health_state", ["healthy", "degraded"])
async def test_reconciliation_factory_resolver_allows_recovery_health_states(
    async_db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    health_state: str,
) -> None:
    state = {"open": False}
    execute = AsyncMock(return_value=_Rows([_row(health_state=health_state)]))
    monkeypatch.setattr(async_db, "execute", execute)
    store = _ExitAwareSecretStore(state)
    resolver = SessionFactoryReconciliationProviderCredentialResolver(
        _TrackedSessionFactory(async_db, state),
        store,
    )

    credential = await resolver.resolve(_context())

    assert state["open"] is False
    assert credential.credential_generation == 3
    assert len(store.calls) == 1


@pytest.mark.parametrize("health_state", ["pending", "error", "disabled"])
async def test_reconciliation_factory_resolver_rejects_non_recovery_health_states(
    async_db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    health_state: str,
) -> None:
    state = {"open": False}
    execute = AsyncMock(return_value=_Rows([_row(health_state=health_state)]))
    monkeypatch.setattr(async_db, "execute", execute)
    store = _ExitAwareSecretStore(state)
    resolver = SessionFactoryReconciliationProviderCredentialResolver(
        _TrackedSessionFactory(async_db, state),
        store,
    )

    with pytest.raises(ProviderCredentialError) as caught:
        await resolver.resolve(_context())

    assert caught.value.code is ProviderErrorCode.CREDENTIAL_UNAVAILABLE
    assert state["open"] is False
    assert store.calls == []


async def test_factory_resolver_propagates_decrypt_cancellation_after_session_exit(
    async_db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = {"open": False}
    execute = AsyncMock(return_value=_Rows([_row()]))
    monkeypatch.setattr(async_db, "execute", execute)
    store = _ExitAwareSecretStore(state, error=asyncio.CancelledError())
    resolver = SessionFactoryChannelProviderCredentialResolver(
        _TrackedSessionFactory(async_db, state),
        store,
    )

    with pytest.raises(asyncio.CancelledError):
        await resolver.resolve(_context())

    assert state["open"] is False
    assert len(store.calls) == 1
