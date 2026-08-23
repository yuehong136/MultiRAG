"""PostgreSQL contract tests for canonical enterprise identity persistence."""

from __future__ import annotations

import asyncio
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from api.db import UserAccountKind
from api.db.db_models import (
    ChatChannel,
    EnterpriseSubjectLink,
    ExternalIdentity,
    ExternalIdentityAlias,
    IdentityBindingEvent,
    IdentityEventReceipt,
    IdentityLinkCode,
    IdentityProviderAccount,
    IdentityProviderChannelLink,
    IdentityProviderTenant,
    IdentityTenantPolicy,
    Tenant,
    User,
)

_I6_REVISION = "b4c6d8e0f2a4"
_CURRENT_HEAD_REVISION = "e1f3a5c7b9d0"
_I21_REVISION = "9a3b5c7d8e0f"
_I2_REVISION = "8f2c4d6e7a9b"
_PRE_I2_REVISION = "7c8d9e0f1a2b"
_SCHEMA = "usr_ai"
_CHAT_TENANT_SCOPE_UNIQUE = "uq_chat_channels_tenant_scope"
_CHAT_IDENTITY_SCOPE_UNIQUE = "uq_chat_channels_identity_scope"
_IDENTITY_TABLES = (
    IdentityProviderTenant.__table__,
    IdentityProviderAccount.__table__,
    IdentityProviderChannelLink.__table__,
    ExternalIdentity.__table__,
    ExternalIdentityAlias.__table__,
    EnterpriseSubjectLink.__table__,
    IdentityEventReceipt.__table__,
    IdentityTenantPolicy.__table__,
    IdentityLinkCode.__table__,
    IdentityBindingEvent.__table__,
)
_AUDIT_COLUMNS = {
    "create_date",
    "update_date",
    "create_time",
    "update_time",
}
_EXPECTED_COLUMNS = {
    "t_ai_identity_provider_tenants": {
        "id",
        "tenant_id",
        "provider",
        "provider_tenant_key",
        "verified_at",
    },
    "t_ai_identity_provider_accounts": {
        "id",
        "tenant_id",
        "provider",
        "provider_tenant_key",
        "provider_account_key",
        "identity_revision",
        "last_scope_change_at",
        "last_directory_event_at",
        "identity_health_state",
        "identity_health_error_code",
    },
    "t_ai_identity_provider_channel_links": {
        "id",
        "tenant_id",
        "provider",
        "provider_account_id",
        "channel_id",
        "linked_at",
    },
    "t_ai_external_identities": {
        "id",
        "tenant_id",
        "user_id",
        "provider",
        "provider_tenant_key",
        "subject_type",
        "subject_value",
        "state",
        "verified_at",
        "last_seen_at",
        "identity_revision",
        "attributes",
    },
    "t_ai_external_identity_aliases": {
        "id",
        "tenant_id",
        "external_identity_id",
        "provider",
        "provider_tenant_key",
        "provider_account_key",
        "alias_type",
        "alias_value",
        "verified_at",
    },
    "t_ai_enterprise_subject_links": {
        "id",
        "tenant_id",
        "user_id",
        "subject_type",
        "subject_value",
        "issuer",
        "issuer_tenant",
        "state",
        "verified_at",
        "source_revision",
    },
    "t_ai_identity_event_receipts": {
        "id",
        "tenant_id",
        "provider",
        "provider_tenant_key",
        "provider_account_key",
        "event_type",
        "event_id",
        "event_hash",
        "processing_state",
        "event_at",
        "processed_at",
        "error_code",
        "external_identity_id",
    },
}


def _ids() -> dict[str, str]:
    prefix = uuid.uuid4().hex[:12]
    return {
        "tenant": f"t-{prefix}",
        "tenant_2": f"u-{prefix}",
        "user": f"a-{prefix}",
        "user_2": f"b-{prefix}",
        "channel": f"c-{prefix}",
        "channel_2": f"d-{prefix}",
        "channel_3": f"e-{prefix}",
        "provider_tenant": f"p-{prefix}",
        "provider_tenant_2": f"q-{prefix}",
        "provider_account": f"r-{prefix}",
        "provider_account_2": f"w-{prefix}",
        "channel_link": f"l-{prefix}",
        "identity": f"i-{prefix}",
        "subject": f"s-{prefix}",
        "receipt": f"v-{prefix}",
        "provider_tenant_key": f"tk-{prefix}",
        "provider_tenant_key_2": f"tk2-{prefix}",
        "provider_account_key": f"app-{prefix}",
        "subject_value": f"uid-{prefix}",
        "alias_value": f"oid-{prefix}",
        "event_id": f"evt-{prefix}",
    }


def _tenant_values(tenant_id: str) -> dict[str, Any]:
    return {
        "id": tenant_id,
        "name": "Identity test tenant",
        "llm_id": "test-llm",
        "embd_id": "test-embedding",
        "asr_id": "test-asr",
        "img2txt_id": "test-image",
        "parser_ids": "naive",
    }


def _user_values(user_id: str) -> dict[str, Any]:
    return {
        "id": user_id,
        "nickname": "Identity test user",
        "email": None,
        "password": None,
        "account_kind": UserAccountKind.EXTERNAL.value,
        "is_authenticated": True,
        "is_active": True,
        "is_anonymous": False,
    }


def _channel_values(channel_id: str, tenant_id: str) -> dict[str, Any]:
    return {
        "id": channel_id,
        "tenant_id": tenant_id,
        "name": "Identity test channel",
        "channel": "feishu",
        "config": {},
        "status": 0,
        "generation": 1,
    }


def _migration_config(alembic_cfg: Config, connection: sa.Connection) -> Config:
    cfg = Config(alembic_cfg.config_file_name)
    cfg.set_main_option(
        "script_location",
        alembic_cfg.get_main_option("script_location"),
    )
    cfg.attributes["connection"] = connection
    return cfg


def _drop_identity_sidecar(connection: sa.Connection) -> None:
    inspector = sa.inspect(connection)
    for table_name in (
        "t_ai_identity_reconciliation_targets",
        "t_ai_identity_reconciliation_checkpoints",
        "t_ai_mcp_interaction_callback_receipts",
        "t_ai_mcp_interaction_presentations",
    ):
        if inspector.has_table(table_name, schema=_SCHEMA):
            connection.execute(sa.text(f'DROP TABLE {_SCHEMA}."{table_name}"'))
            inspector = sa.inspect(connection)
    for table in reversed(_IDENTITY_TABLES):
        if inspector.has_table(table.name, schema=_SCHEMA):
            connection.execute(sa.text(f'DROP TABLE {_SCHEMA}."{table.name}"'))
            inspector = sa.inspect(connection)
    membership_indexes = {index["name"] for index in inspector.get_indexes("t_ai_user_tenants", schema=_SCHEMA)}
    if "uq_user_tenants_active_tenant_user" in membership_indexes:
        connection.execute(sa.text("DROP INDEX usr_ai.uq_user_tenants_active_tenant_user"))
    chat_unique = {
        constraint["name"]
        for constraint in inspector.get_unique_constraints(
            ChatChannel.__tablename__,
            schema=_SCHEMA,
        )
    }
    for constraint_name in (
        _CHAT_IDENTITY_SCOPE_UNIQUE,
        _CHAT_TENANT_SCOPE_UNIQUE,
    ):
        if constraint_name in chat_unique:
            connection.execute(sa.text(f"ALTER TABLE {_SCHEMA}.{ChatChannel.__tablename__} DROP CONSTRAINT {constraint_name}"))


def _seed_scope(
    connection: sa.Connection,
    values: dict[str, str],
    *,
    link_account: bool = True,
) -> None:
    connection.execute(
        sa.insert(Tenant.__table__),
        [
            _tenant_values(values["tenant"]),
            _tenant_values(values["tenant_2"]),
        ],
    )
    connection.execute(
        sa.insert(User.__table__),
        [
            _user_values(values["user"]),
            _user_values(values["user_2"]),
        ],
    )
    connection.execute(
        sa.insert(ChatChannel.__table__),
        [
            _channel_values(values["channel"], values["tenant"]),
            _channel_values(values["channel_2"], values["tenant_2"]),
            _channel_values(values["channel_3"], values["tenant"]),
        ],
    )
    now = datetime(2026, 8, 12, tzinfo=UTC)
    connection.execute(
        sa.insert(IdentityProviderTenant.__table__).values(
            id=values["provider_tenant"],
            tenant_id=values["tenant"],
            provider="feishu",
            provider_tenant_key=values["provider_tenant_key"],
            verified_at=now,
        )
    )
    connection.execute(
        sa.insert(IdentityProviderTenant.__table__).values(
            id=values["provider_tenant_2"],
            tenant_id=values["tenant_2"],
            provider="feishu",
            provider_tenant_key=values["provider_tenant_key_2"],
            verified_at=now,
        )
    )
    connection.execute(
        sa.insert(IdentityProviderAccount.__table__).values(
            id=values["provider_account"],
            tenant_id=values["tenant"],
            provider="feishu",
            provider_tenant_key=values["provider_tenant_key"],
            provider_account_key=values["provider_account_key"],
        )
    )
    if link_account:
        connection.execute(
            sa.insert(IdentityProviderChannelLink.__table__).values(
                id=values["channel_link"],
                tenant_id=values["tenant"],
                provider="feishu",
                provider_account_id=values["provider_account"],
                channel_id=values["channel"],
                linked_at=now,
            )
        )
    connection.execute(
        sa.insert(ExternalIdentity.__table__).values(
            id=values["identity"],
            tenant_id=values["tenant"],
            user_id=values["user"],
            provider="feishu",
            provider_tenant_key=values["provider_tenant_key"],
            subject_type="user_id",
            subject_value=values["subject_value"],
        )
    )


def _seed_old_i2_binding(
    connection: sa.Connection,
    values: dict[str, str],
    *,
    account_provider: str,
    channel_provider: str,
    channel_config: dict[str, object],
) -> None:
    """Seed one pre-I2.1 account whose Channel relationship is inline."""

    connection.execute(sa.insert(Tenant.__table__).values(**_tenant_values(values["tenant"])))
    channel_values = _channel_values(values["channel"], values["tenant"])
    channel_values.update(
        {
            "channel": channel_provider,
            "config": channel_config,
        }
    )
    connection.execute(sa.insert(ChatChannel.__table__).values(**channel_values))
    connection.execute(
        sa.insert(IdentityProviderTenant.__table__).values(
            id=values["provider_tenant"],
            tenant_id=values["tenant"],
            provider=account_provider,
            provider_tenant_key=values["provider_tenant_key"],
            verified_at=datetime(2026, 8, 12, tzinfo=UTC),
        )
    )
    connection.execute(
        sa.text(
            """
            INSERT INTO usr_ai.t_ai_identity_provider_accounts (
                id,
                tenant_id,
                channel_id,
                provider,
                provider_tenant_key,
                provider_account_key
            ) VALUES (
                :id,
                :tenant_id,
                :channel_id,
                :provider,
                :provider_tenant_key,
                :provider_account_key
            )
            """
        ),
        {
            "id": values["provider_account"],
            "tenant_id": values["tenant"],
            "channel_id": values["channel"],
            "provider": account_provider,
            "provider_tenant_key": values["provider_tenant_key"],
            "provider_account_key": values["provider_account_key"],
        },
    )


def _seed_current_provider_account(
    connection: sa.Connection,
    values: dict[str, str],
    *,
    with_link: bool,
) -> None:
    connection.execute(sa.insert(Tenant.__table__).values(**_tenant_values(values["tenant"])))
    if with_link:
        connection.execute(sa.insert(ChatChannel.__table__).values(**_channel_values(values["channel"], values["tenant"])))
    connection.execute(
        sa.insert(IdentityProviderTenant.__table__).values(
            id=values["provider_tenant"],
            tenant_id=values["tenant"],
            provider="feishu",
            provider_tenant_key=values["provider_tenant_key"],
            verified_at=datetime(2026, 8, 12, tzinfo=UTC),
        )
    )
    connection.execute(
        sa.insert(IdentityProviderAccount.__table__).values(
            id=values["provider_account"],
            tenant_id=values["tenant"],
            provider="feishu",
            provider_tenant_key=values["provider_tenant_key"],
            provider_account_key=values["provider_account_key"],
        )
    )
    if with_link:
        connection.execute(
            sa.insert(IdentityProviderChannelLink.__table__).values(
                id=values["channel_link"],
                tenant_id=values["tenant"],
                provider="feishu",
                provider_account_id=values["provider_account"],
                channel_id=values["channel"],
                linked_at=datetime(2026, 8, 12, tzinfo=UTC),
            )
        )


def _assert_integrity_error(
    connection: sa.Connection,
    statement: sa.Executable,
) -> None:
    with pytest.raises(IntegrityError), connection.begin_nested():
        connection.execute(statement)


def test_fresh_schema_has_exact_identity_columns_defaults_and_no_event_body(
    bootstrapped_engine: sa.Engine,
) -> None:
    inspector = sa.inspect(bootstrapped_engine)

    for table_name, domain_columns in _EXPECTED_COLUMNS.items():
        columns = {str(column["name"]): column for column in inspector.get_columns(table_name, schema=_SCHEMA)}
        assert set(columns) == domain_columns | _AUDIT_COLUMNS
        assert columns["id"]["nullable"] is False

    account_columns = {
        str(column["name"]): column
        for column in inspector.get_columns(
            IdentityProviderAccount.__tablename__,
            schema=_SCHEMA,
        )
    }
    identity_columns = {
        str(column["name"]): column
        for column in inspector.get_columns(
            ExternalIdentity.__tablename__,
            schema=_SCHEMA,
        )
    }
    link_columns = {
        str(column["name"]): column
        for column in inspector.get_columns(
            IdentityProviderChannelLink.__tablename__,
            schema=_SCHEMA,
        )
    }
    subject_columns = {
        str(column["name"]): column
        for column in inspector.get_columns(
            EnterpriseSubjectLink.__tablename__,
            schema=_SCHEMA,
        )
    }
    receipt_columns = {
        str(column["name"]): column
        for column in inspector.get_columns(
            IdentityEventReceipt.__tablename__,
            schema=_SCHEMA,
        )
    }

    assert account_columns["identity_revision"]["default"] is not None
    assert account_columns["identity_health_state"]["default"] is not None
    assert link_columns["linked_at"]["default"] is None
    assert identity_columns["state"]["default"] is not None
    assert identity_columns["identity_revision"]["default"] is not None
    assert identity_columns["attributes"]["default"] is not None
    assert subject_columns["state"]["default"] is not None
    assert receipt_columns["processing_state"]["default"] is not None
    assert "1" in str(account_columns["identity_revision"]["default"])
    assert "pending" in str(account_columns["identity_health_state"]["default"])
    assert "pending_link" in str(identity_columns["state"]["default"])
    assert "1" in str(identity_columns["identity_revision"]["default"])
    assert "{}" in str(identity_columns["attributes"]["default"])
    assert "inactive" in str(subject_columns["state"]["default"])
    assert "processing" in str(receipt_columns["processing_state"]["default"])
    assert not set(receipt_columns).intersection({"body", "event_body", "payload", "raw_body", "raw_event"})


def test_stored_database_upgrade_creates_current_identity_sidecar(
    bootstrapped_engine: sa.Engine,
    alembic_cfg: Config,
) -> None:
    connection = bootstrapped_engine.connect()
    transaction = connection.begin()
    try:
        _drop_identity_sidecar(connection)
        connection.execute(
            sa.text("UPDATE usr_ai.alembic_version SET version_num = :revision"),
            {"revision": _PRE_I2_REVISION},
        )

        command.upgrade(_migration_config(alembic_cfg, connection), _I6_REVISION)

        inspector = sa.inspect(connection)
        assert all(inspector.has_table(table.name, schema=_SCHEMA) for table in _IDENTITY_TABLES)
        chat_unique = {
            constraint["name"]
            for constraint in inspector.get_unique_constraints(
                ChatChannel.__tablename__,
                schema=_SCHEMA,
            )
        }
        assert _CHAT_TENANT_SCOPE_UNIQUE in chat_unique
        assert _CHAT_IDENTITY_SCOPE_UNIQUE in chat_unique
        assert connection.execute(sa.text("SELECT version_num FROM usr_ai.alembic_version")).scalar_one() == _I6_REVISION
    finally:
        transaction.rollback()
        connection.close()


@pytest.mark.parametrize(
    ("provider", "credential_field"),
    [
        ("feishu", "app_id"),
        ("dingtalk", "client_id"),
    ],
)
def test_old_i2_binding_backfills_one_to_one_channel_link(
    bootstrapped_engine: sa.Engine,
    alembic_cfg: Config,
    provider: str,
    credential_field: str,
) -> None:
    values = _ids()
    connection = bootstrapped_engine.connect()
    transaction = connection.begin()
    try:
        cfg = _migration_config(alembic_cfg, connection)
        command.downgrade(cfg, _I2_REVISION)
        _seed_old_i2_binding(
            connection,
            values,
            account_provider=provider,
            channel_provider=provider,
            channel_config={
                "credential": {
                    credential_field: values["provider_account_key"],
                }
            },
        )

        command.upgrade(cfg, _I21_REVISION)

        account_columns = {
            str(column["name"])
            for column in sa.inspect(connection).get_columns(
                IdentityProviderAccount.__tablename__,
                schema=_SCHEMA,
            )
        }
        link = connection.execute(
            sa.select(
                IdentityProviderChannelLink.id,
                IdentityProviderChannelLink.tenant_id,
                IdentityProviderChannelLink.provider,
                IdentityProviderChannelLink.provider_account_id,
                IdentityProviderChannelLink.channel_id,
                IdentityProviderChannelLink.linked_at,
            )
        ).one()

        assert "channel_id" not in account_columns
        assert link.id != values["provider_account"]
        assert re.fullmatch(r"[0-9a-f]{32}", link.id)
        assert link.tenant_id == values["tenant"]
        assert link.provider == provider
        assert link.provider_account_id == values["provider_account"]
        assert link.channel_id == values["channel"]
        assert link.linked_at is not None
        assert connection.execute(sa.text("SELECT version_num FROM usr_ai.alembic_version")).scalar_one() == _I21_REVISION
    finally:
        transaction.rollback()
        connection.close()


@pytest.mark.parametrize(
    ("account_provider", "channel_provider", "channel_config", "category"),
    [
        (
            "feishu",
            "feishu",
            {"credential": {}},
            "missing_account_identity",
        ),
        (
            "feishu",
            "feishu",
            {"credential": {"app_id": 42}},
            "invalid_account_identity_type",
        ),
        (
            "feishu",
            "feishu",
            {"credential": {"app_id": "different-installation"}},
            "account_identity_mismatch",
        ),
        (
            "feishu",
            "dingtalk",
            {"credential": {"app_id": "different-installation"}},
            "provider_mismatch",
        ),
        (
            "unsupported-provider",
            "unsupported-provider",
            {"credential": {}},
            "unsupported_provider",
        ),
    ],
)
def test_old_i2_binding_drift_fails_closed_without_leaking_account_key(
    bootstrapped_engine: sa.Engine,
    alembic_cfg: Config,
    account_provider: str,
    channel_provider: str,
    channel_config: dict[str, object],
    category: str,
) -> None:
    values = _ids()
    connection = bootstrapped_engine.connect()
    transaction = connection.begin()
    try:
        cfg = _migration_config(alembic_cfg, connection)
        command.downgrade(cfg, _I2_REVISION)
        _seed_old_i2_binding(
            connection,
            values,
            account_provider=account_provider,
            channel_provider=channel_provider,
            channel_config=channel_config,
        )

        with pytest.raises(
            RuntimeError,
            match="identity binding validation failed",
        ) as exc_info:
            command.upgrade(cfg, _I21_REVISION)

        message = str(exc_info.value)
        assert f"{category}=1" in message
        assert values["provider_account_key"] not in message
        assert "different-installation" not in message
        assert connection.execute(sa.text("SELECT version_num FROM usr_ai.alembic_version")).scalar_one() == _I2_REVISION
        assert not sa.inspect(connection).has_table(
            IdentityProviderChannelLink.__tablename__,
            schema=_SCHEMA,
        )
    finally:
        transaction.rollback()
        connection.close()


def test_model_first_i21_sidecar_is_validated_instead_of_recreated(
    bootstrapped_engine: sa.Engine,
    alembic_cfg: Config,
) -> None:
    connection = bootstrapped_engine.connect()
    transaction = connection.begin()
    try:
        connection.execute(
            sa.text("UPDATE usr_ai.alembic_version SET version_num = :revision"),
            {"revision": _I2_REVISION},
        )

        command.upgrade(_migration_config(alembic_cfg, connection), _I21_REVISION)

        assert connection.execute(sa.text("SELECT version_num FROM usr_ai.alembic_version")).scalar_one() == _I21_REVISION
    finally:
        transaction.rollback()
        connection.close()


def test_model_first_rejects_same_name_weakened_link_check_without_advancing_version(
    bootstrapped_engine: sa.Engine,
    alembic_cfg: Config,
) -> None:
    connection = bootstrapped_engine.connect()
    transaction = connection.begin()
    try:
        connection.execute(sa.text("ALTER TABLE usr_ai.t_ai_identity_provider_channel_links DROP CONSTRAINT ck_identity_provider_channel_links_provider_nonempty"))
        connection.execute(sa.text("ALTER TABLE usr_ai.t_ai_identity_provider_channel_links ADD CONSTRAINT ck_identity_provider_channel_links_provider_nonempty CHECK (true)"))
        connection.execute(
            sa.text("UPDATE usr_ai.alembic_version SET version_num = :revision"),
            {"revision": _I2_REVISION},
        )

        with pytest.raises(RuntimeError, match="check constraints"):
            command.upgrade(
                _migration_config(alembic_cfg, connection),
                _I21_REVISION,
            )

        assert connection.execute(sa.text("SELECT version_num FROM usr_ai.alembic_version")).scalar_one() == _I2_REVISION
    finally:
        transaction.rollback()
        connection.close()


def test_model_first_rejects_wrong_link_server_default_without_advancing_version(
    bootstrapped_engine: sa.Engine,
    alembic_cfg: Config,
) -> None:
    connection = bootstrapped_engine.connect()
    transaction = connection.begin()
    try:
        connection.execute(sa.text("ALTER TABLE usr_ai.t_ai_identity_provider_channel_links ALTER COLUMN linked_at SET DEFAULT now()"))
        connection.execute(
            sa.text("UPDATE usr_ai.alembic_version SET version_num = :revision"),
            {"revision": _I2_REVISION},
        )

        with pytest.raises(RuntimeError, match="server defaults"):
            command.upgrade(
                _migration_config(alembic_cfg, connection),
                _I21_REVISION,
            )

        assert connection.execute(sa.text("SELECT version_num FROM usr_ai.alembic_version")).scalar_one() == _I2_REVISION
    finally:
        transaction.rollback()
        connection.close()


def test_model_first_rejects_not_valid_link_constraint_without_advancing_version(
    bootstrapped_engine: sa.Engine,
    alembic_cfg: Config,
) -> None:
    connection = bootstrapped_engine.connect()
    transaction = connection.begin()
    try:
        connection.execute(sa.text("ALTER TABLE usr_ai.t_ai_identity_provider_channel_links DROP CONSTRAINT ck_identity_provider_channel_links_provider_nonempty"))
        connection.execute(
            sa.text("ALTER TABLE usr_ai.t_ai_identity_provider_channel_links ADD CONSTRAINT ck_identity_provider_channel_links_provider_nonempty CHECK (btrim(provider) <> '') NOT VALID")
        )
        connection.execute(
            sa.text("UPDATE usr_ai.alembic_version SET version_num = :revision"),
            {"revision": _I2_REVISION},
        )

        with pytest.raises(RuntimeError, match="constraint enforcement"):
            command.upgrade(
                _migration_config(alembic_cfg, connection),
                _I21_REVISION,
            )

        assert connection.execute(sa.text("SELECT version_num FROM usr_ai.alembic_version")).scalar_one() == _I2_REVISION
    finally:
        transaction.rollback()
        connection.close()


def test_upgrade_rejects_partial_i21_schema_without_advancing_version(
    bootstrapped_engine: sa.Engine,
    alembic_cfg: Config,
) -> None:
    connection = bootstrapped_engine.connect()
    transaction = connection.begin()
    try:
        connection.execute(sa.text("DROP TABLE usr_ai.t_ai_identity_provider_channel_links"))
        connection.execute(
            sa.text("UPDATE usr_ai.alembic_version SET version_num = :revision"),
            {"revision": _I2_REVISION},
        )

        with pytest.raises(RuntimeError, match="identity schema is incomplete"):
            command.upgrade(
                _migration_config(alembic_cfg, connection),
                _I21_REVISION,
            )

        assert connection.execute(sa.text("SELECT version_num FROM usr_ai.alembic_version")).scalar_one() == _I2_REVISION
    finally:
        transaction.rollback()
        connection.close()


def test_model_first_rejects_wrong_link_column_shape_without_advancing_version(
    bootstrapped_engine: sa.Engine,
    alembic_cfg: Config,
) -> None:
    connection = bootstrapped_engine.connect()
    transaction = connection.begin()
    try:
        connection.execute(sa.text("ALTER TABLE usr_ai.t_ai_identity_provider_channel_links ALTER COLUMN linked_at TYPE timestamp without time zone USING linked_at AT TIME ZONE 'UTC'"))
        connection.execute(
            sa.text("UPDATE usr_ai.alembic_version SET version_num = :revision"),
            {"revision": _I2_REVISION},
        )

        with pytest.raises(RuntimeError, match="columns"):
            command.upgrade(
                _migration_config(alembic_cfg, connection),
                _I21_REVISION,
            )

        assert connection.execute(sa.text("SELECT version_num FROM usr_ai.alembic_version")).scalar_one() == _I2_REVISION
    finally:
        transaction.rollback()
        connection.close()


def test_empty_identity_sidecar_downgrades_and_upgrades_round_trip(
    bootstrapped_engine: sa.Engine,
    alembic_cfg: Config,
) -> None:
    connection = bootstrapped_engine.connect()
    transaction = connection.begin()
    try:
        for table in _IDENTITY_TABLES:
            assert connection.execute(sa.select(sa.func.count()).select_from(table)).scalar_one() == 0

        cfg = _migration_config(alembic_cfg, connection)
        command.downgrade(cfg, _PRE_I2_REVISION)

        inspector = sa.inspect(connection)
        assert all(not inspector.has_table(table.name, schema=_SCHEMA) for table in _IDENTITY_TABLES)
        chat_unique = {
            constraint["name"]
            for constraint in inspector.get_unique_constraints(
                ChatChannel.__tablename__,
                schema=_SCHEMA,
            )
        }
        assert _CHAT_TENANT_SCOPE_UNIQUE not in chat_unique
        assert _CHAT_IDENTITY_SCOPE_UNIQUE not in chat_unique
        assert connection.execute(sa.text("SELECT version_num FROM usr_ai.alembic_version")).scalar_one() == _PRE_I2_REVISION

        command.upgrade(cfg, _I6_REVISION)

        inspector = sa.inspect(connection)
        assert all(inspector.has_table(table.name, schema=_SCHEMA) for table in _IDENTITY_TABLES)
        chat_unique = {
            constraint["name"]
            for constraint in inspector.get_unique_constraints(
                ChatChannel.__tablename__,
                schema=_SCHEMA,
            )
        }
        assert _CHAT_TENANT_SCOPE_UNIQUE in chat_unique
        assert _CHAT_IDENTITY_SCOPE_UNIQUE in chat_unique
        assert connection.execute(sa.text("SELECT version_num FROM usr_ai.alembic_version")).scalar_one() == _I6_REVISION
    finally:
        transaction.rollback()
        connection.close()


@pytest.mark.parametrize("with_link", [False, True])
def test_i21_downgrade_refuses_provider_account_ownership(
    bootstrapped_engine: sa.Engine,
    alembic_cfg: Config,
    *,
    with_link: bool,
) -> None:
    values = _ids()
    connection = bootstrapped_engine.connect()
    transaction = connection.begin()
    try:
        _seed_current_provider_account(
            connection,
            values,
            with_link=with_link,
        )

        with pytest.raises(
            RuntimeError,
            match="provider account/channel decoupling while identity ownership exists",
        ) as exc_info:
            command.downgrade(
                _migration_config(alembic_cfg, connection),
                _I2_REVISION,
            )

        message = str(exc_info.value)
        assert "t_ai_identity_provider_accounts=1" in message
        if with_link:
            assert "t_ai_identity_provider_channel_links=1" in message
        else:
            assert "t_ai_identity_provider_channel_links" not in message
        assert values["provider_account_key"] not in message
        assert values["channel"] not in message
        assert connection.execute(sa.text("SELECT version_num FROM usr_ai.alembic_version")).scalar_one() == _I21_REVISION
    finally:
        transaction.rollback()
        connection.close()


def test_i21_downgrade_lock_blocks_concurrent_account_insert_and_preserves_link(
    bootstrapped_engine: sa.Engine,
    alembic_cfg: Config,
) -> None:
    values = _ids()
    attempted_id = uuid.uuid4().hex
    attempted_account_key = f"concurrent-{values['provider_account_key']}"
    with bootstrapped_engine.begin() as seed_connection:
        _seed_current_provider_account(
            seed_connection,
            values,
            with_link=True,
        )

    lock_acquired = threading.Event()
    contender_finished = threading.Event()
    downgrade_connection = bootstrapped_engine.connect()
    downgrade_transaction = downgrade_connection.begin()

    def _after_cursor_execute(
        _connection: sa.Connection,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        normalized = " ".join(statement.lower().split())
        expected_tables = (
            IdentityProviderAccount.__tablename__,
            IdentityProviderChannelLink.__tablename__,
            ChatChannel.__tablename__,
        )
        if normalized.startswith("lock table") and all(table_name in normalized for table_name in expected_tables):
            lock_acquired.set()
            if not contender_finished.wait(timeout=5):
                raise AssertionError("concurrent account insert did not finish within lock_timeout")

    def _attempt_concurrent_insert() -> str | None:
        if not lock_acquired.wait(timeout=5):
            raise AssertionError("I2.1 downgrade did not acquire transition locks")
        with bootstrapped_engine.connect() as contender:
            contender_transaction = contender.begin()
            try:
                contender.execute(sa.text("SET LOCAL lock_timeout = '1s'"))
                contender.execute(
                    sa.insert(IdentityProviderAccount.__table__).values(
                        id=attempted_id,
                        tenant_id=values["tenant"],
                        provider="feishu",
                        provider_tenant_key=values["provider_tenant_key"],
                        provider_account_key=attempted_account_key,
                    )
                )
            except OperationalError as exc:
                contender_transaction.rollback()
                return getattr(exc.orig, "sqlstate", None)
            else:
                contender_transaction.commit()
                return "committed"
            finally:
                contender_finished.set()

    sa.event.listen(
        downgrade_connection,
        "after_cursor_execute",
        _after_cursor_execute,
    )
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            contender = executor.submit(_attempt_concurrent_insert)
            with pytest.raises(
                RuntimeError,
                match="provider account/channel decoupling while identity ownership exists",
            ):
                command.downgrade(
                    _migration_config(alembic_cfg, downgrade_connection),
                    _I2_REVISION,
                )
            assert contender.result(timeout=5) == "55P03"
    finally:
        sa.event.remove(
            downgrade_connection,
            "after_cursor_execute",
            _after_cursor_execute,
        )
        downgrade_transaction.rollback()
        downgrade_connection.close()

    try:
        with bootstrapped_engine.connect() as verification_connection:
            account_ids = set(verification_connection.execute(sa.select(IdentityProviderAccount.id).where(IdentityProviderAccount.id.in_([values["provider_account"], attempted_id]))).scalars())
            link_ids = set(verification_connection.execute(sa.select(IdentityProviderChannelLink.id).where(IdentityProviderChannelLink.id == values["channel_link"])).scalars())
        assert account_ids == {values["provider_account"]}
        assert link_ids == {values["channel_link"]}
    finally:
        with bootstrapped_engine.begin() as cleanup_connection:
            cleanup_connection.execute(sa.delete(IdentityProviderChannelLink).where(IdentityProviderChannelLink.tenant_id == values["tenant"]))
            cleanup_connection.execute(sa.delete(IdentityProviderAccount).where(IdentityProviderAccount.tenant_id == values["tenant"]))
            cleanup_connection.execute(sa.delete(IdentityProviderTenant).where(IdentityProviderTenant.tenant_id == values["tenant"]))
            cleanup_connection.execute(sa.delete(ChatChannel).where(ChatChannel.id == values["channel"]))
            cleanup_connection.execute(sa.delete(Tenant).where(Tenant.id == values["tenant"]))


def test_i2_downgrade_refuses_to_destroy_identity_history(
    bootstrapped_engine: sa.Engine,
    alembic_cfg: Config,
) -> None:
    values = _ids()
    connection = bootstrapped_engine.connect()
    transaction = connection.begin()
    try:
        connection.execute(sa.insert(Tenant.__table__).values(**_tenant_values(values["tenant"])))
        connection.execute(
            sa.insert(IdentityProviderTenant.__table__).values(
                id=values["provider_tenant"],
                tenant_id=values["tenant"],
                provider="feishu",
                provider_tenant_key=values["provider_tenant_key"],
                verified_at=datetime(2026, 8, 12, tzinfo=UTC),
            )
        )

        with pytest.raises(RuntimeError, match="identity history exists"):
            command.downgrade(
                _migration_config(alembic_cfg, connection),
                _PRE_I2_REVISION,
            )

        assert sa.inspect(connection).has_table(
            IdentityProviderTenant.__tablename__,
            schema=_SCHEMA,
        )
        head = ScriptDirectory.from_config(alembic_cfg).get_current_head()
        assert head == _CURRENT_HEAD_REVISION
        # The I8, U15/U14, and I2.1 steps are safely reversible because their
        # protected rows do not exist; the following I2 downgrade then refuses
        # to erase the provider tenant history. Alembic therefore remains at
        # the last completed revision instead of pretending the whole
        # multi-step downgrade was atomic.
        assert connection.execute(sa.text("SELECT version_num FROM usr_ai.alembic_version")).scalar_one() == _I2_REVISION
    finally:
        transaction.rollback()
        connection.close()


def test_downgrade_lock_blocks_concurrent_identity_insert_and_preserves_history(
    bootstrapped_engine: sa.Engine,
    alembic_cfg: Config,
) -> None:
    values = _ids()
    attempted_id = uuid.uuid4().hex
    attempted_provider_tenant_key = f"concurrent-{values['provider_tenant_key']}"
    with bootstrapped_engine.begin() as seed_connection:
        seed_connection.execute(sa.insert(Tenant.__table__).values(**_tenant_values(values["tenant"])))
        seed_connection.execute(
            sa.insert(IdentityProviderTenant.__table__).values(
                id=values["provider_tenant"],
                tenant_id=values["tenant"],
                provider="feishu",
                provider_tenant_key=values["provider_tenant_key"],
                verified_at=datetime(2026, 8, 12, tzinfo=UTC),
            )
        )

    lock_acquired = threading.Event()
    contender_finished = threading.Event()
    downgrade_connection = bootstrapped_engine.connect()
    downgrade_transaction = downgrade_connection.begin()

    def _after_cursor_execute(
        _connection: sa.Connection,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        normalized = " ".join(statement.lower().split())
        if normalized.startswith("lock table") and IdentityProviderTenant.__tablename__ in normalized:
            lock_acquired.set()
            if not contender_finished.wait(timeout=5):
                raise AssertionError("concurrent insert did not finish within its lock_timeout")

    def _attempt_concurrent_insert() -> str | None:
        if not lock_acquired.wait(timeout=5):
            raise AssertionError("downgrade did not acquire the identity table lock")
        with bootstrapped_engine.connect() as contender:
            contender_transaction = contender.begin()
            try:
                contender.execute(sa.text("SET LOCAL lock_timeout = '1s'"))
                contender.execute(
                    sa.insert(IdentityProviderTenant.__table__).values(
                        id=attempted_id,
                        tenant_id=values["tenant"],
                        provider="feishu",
                        provider_tenant_key=attempted_provider_tenant_key,
                        verified_at=datetime(2026, 8, 12, tzinfo=UTC),
                    )
                )
            except OperationalError as exc:
                contender_transaction.rollback()
                return getattr(exc.orig, "sqlstate", None)
            else:
                contender_transaction.commit()
                return "committed"
            finally:
                contender_finished.set()

    sa.event.listen(
        downgrade_connection,
        "after_cursor_execute",
        _after_cursor_execute,
    )
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            contender = executor.submit(_attempt_concurrent_insert)
            with pytest.raises(RuntimeError, match="identity history exists"):
                command.downgrade(
                    _migration_config(alembic_cfg, downgrade_connection),
                    _PRE_I2_REVISION,
                )
            assert contender.result(timeout=5) == "55P03"
    finally:
        sa.event.remove(
            downgrade_connection,
            "after_cursor_execute",
            _after_cursor_execute,
        )
        downgrade_transaction.rollback()
        downgrade_connection.close()

    try:
        with bootstrapped_engine.connect() as verification_connection:
            persisted_ids = set(verification_connection.execute(sa.select(IdentityProviderTenant.id).where(IdentityProviderTenant.id.in_([values["provider_tenant"], attempted_id]))).scalars())
        assert persisted_ids == {values["provider_tenant"]}
    finally:
        with bootstrapped_engine.begin() as cleanup_connection:
            cleanup_connection.execute(sa.delete(IdentityProviderTenant).where(IdentityProviderTenant.tenant_id == values["tenant"]))
            cleanup_connection.execute(sa.delete(Tenant).where(Tenant.id == values["tenant"]))


def test_database_enforces_tenant_account_identity_and_event_boundaries(
    bootstrapped_engine: sa.Engine,
) -> None:
    values = _ids()
    connection = bootstrapped_engine.connect()
    transaction = connection.begin()
    try:
        _seed_scope(connection, values)

        # A provider enterprise is globally owned by exactly one MultiRAG tenant.
        _assert_integrity_error(
            connection,
            sa.insert(IdentityProviderTenant.__table__).values(
                id=uuid.uuid4().hex,
                tenant_id=values["tenant_2"],
                provider="feishu",
                provider_tenant_key=values["provider_tenant_key"],
                verified_at=datetime(2026, 8, 12, tzinfo=UTC),
            ),
        )

        # A Provider Account is durable without a Channel link, while the same
        # installation identity still has exactly one database owner.
        standalone_account_id = values["provider_account_2"]
        connection.execute(
            sa.insert(IdentityProviderAccount.__table__).values(
                id=standalone_account_id,
                tenant_id=values["tenant"],
                provider="feishu",
                provider_tenant_key=values["provider_tenant_key"],
                provider_account_key=f"standalone-{values['provider_account_key']}",
            )
        )
        assert connection.execute(sa.select(IdentityProviderAccount.id).where(IdentityProviderAccount.id == standalone_account_id)).scalar_one() == standalone_account_id
        _assert_integrity_error(
            connection,
            sa.insert(IdentityProviderAccount.__table__).values(
                id=uuid.uuid4().hex,
                tenant_id=values["tenant"],
                provider="feishu",
                provider_tenant_key=values["provider_tenant_key"],
                provider_account_key=values["provider_account_key"],
            ),
        )
        _assert_integrity_error(
            connection,
            sa.insert(IdentityProviderAccount.__table__).values(
                id=uuid.uuid4().hex,
                tenant_id=values["tenant"],
                provider="feishu",
                provider_tenant_key=values["provider_tenant_key"],
                provider_account_key=f"unhealthy-{values['provider_account_key']}",
                identity_health_state="error",
                identity_health_error_code=None,
            ),
        )
        # Account tenant/provider scope must be owned by the same verified
        # external enterprise.
        _assert_integrity_error(
            connection,
            sa.insert(IdentityProviderAccount.__table__).values(
                id=uuid.uuid4().hex,
                tenant_id=values["tenant_2"],
                provider="feishu",
                provider_tenant_key=values["provider_tenant_key"],
                provider_account_key=f"wrong-tenant-{values['provider_account_key']}",
            ),
        )

        # The link is one-to-one in both directions.
        _assert_integrity_error(
            connection,
            sa.insert(IdentityProviderChannelLink.__table__).values(
                id=uuid.uuid4().hex,
                tenant_id=values["tenant"],
                provider="feishu",
                provider_account_id=values["provider_account"],
                channel_id=values["channel_3"],
                linked_at=datetime(2026, 8, 12, tzinfo=UTC),
            ),
        )
        _assert_integrity_error(
            connection,
            sa.insert(IdentityProviderChannelLink.__table__).values(
                id=uuid.uuid4().hex,
                tenant_id=values["tenant"],
                provider="feishu",
                provider_account_id=standalone_account_id,
                channel_id=values["channel"],
                linked_at=datetime(2026, 8, 12, tzinfo=UTC),
            ),
        )

        # Composite account and Channel foreign keys prevent tenant/provider
        # drift even when every referenced scalar ID exists independently.
        _assert_integrity_error(
            connection,
            sa.insert(IdentityProviderChannelLink.__table__).values(
                id=uuid.uuid4().hex,
                tenant_id=values["tenant_2"],
                provider="feishu",
                provider_account_id=standalone_account_id,
                channel_id=values["channel_2"],
                linked_at=datetime(2026, 8, 12, tzinfo=UTC),
            ),
        )
        _assert_integrity_error(
            connection,
            sa.insert(IdentityProviderChannelLink.__table__).values(
                id=uuid.uuid4().hex,
                tenant_id=values["tenant"],
                provider="dingtalk",
                provider_account_id=standalone_account_id,
                channel_id=values["channel_3"],
                linked_at=datetime(2026, 8, 12, tzinfo=UTC),
            ),
        )

        # Deleting either endpoint cannot silently erase or orphan ownership.
        _assert_integrity_error(
            connection,
            sa.delete(IdentityProviderAccount.__table__).where(IdentityProviderAccount.id == values["provider_account"]),
        )
        _assert_integrity_error(
            connection,
            sa.delete(ChatChannel.__table__).where(ChatChannel.id == values["channel"]),
        )

        # Core inserts bypass ORM validation; PostgreSQL still rejects unsafe
        # JSON attributes and unverified active identities.
        _assert_integrity_error(
            connection,
            sa.insert(ExternalIdentity.__table__).values(
                id=uuid.uuid4().hex,
                tenant_id=values["tenant"],
                user_id=values["user"],
                provider="feishu",
                provider_tenant_key=values["provider_tenant_key_2"],
                subject_type="user_id",
                subject_value=f"wrong-owner-{values['subject_value']}",
            ),
        )
        _assert_integrity_error(
            connection,
            sa.insert(ExternalIdentity.__table__).values(
                id=uuid.uuid4().hex,
                tenant_id=values["tenant"],
                user_id=values["user"],
                provider="feishu",
                provider_tenant_key=values["provider_tenant_key"],
                subject_type="user_id",
                subject_value=f"unsafe-{values['subject_value']}",
                attributes={"open_id": "must-not-be-stored"},
            ),
        )
        _assert_integrity_error(
            connection,
            sa.insert(ExternalIdentity.__table__).values(
                id=uuid.uuid4().hex,
                tenant_id=values["tenant"],
                user_id=values["user"],
                provider="feishu",
                provider_tenant_key=values["provider_tenant_key"],
                subject_type="user_id",
                subject_value=f"active-{values['subject_value']}",
                state="active",
                verified_at=None,
            ),
        )

        # Alias and event scopes must name an existing installation.
        _assert_integrity_error(
            connection,
            sa.insert(ExternalIdentityAlias.__table__).values(
                id=uuid.uuid4().hex,
                tenant_id=values["tenant"],
                external_identity_id=values["identity"],
                provider="feishu",
                provider_tenant_key=values["provider_tenant_key"],
                provider_account_key="unregistered-app",
                alias_type="open_id",
                alias_value=values["alias_value"],
                verified_at=datetime(2026, 8, 12, tzinfo=UTC),
            ),
        )
        _assert_integrity_error(
            connection,
            sa.insert(ExternalIdentityAlias.__table__).values(
                id=uuid.uuid4().hex,
                tenant_id=values["tenant"],
                external_identity_id=values["identity"],
                provider="feishu",
                provider_tenant_key=values["provider_tenant_key"],
                provider_account_key=values["provider_account_key"],
                alias_type="email",
                alias_value=values["alias_value"],
                verified_at=datetime(2026, 8, 12, tzinfo=UTC),
            ),
        )
        _assert_integrity_error(
            connection,
            sa.insert(ExternalIdentityAlias.__table__).values(
                id=uuid.uuid4().hex,
                tenant_id=values["tenant"],
                external_identity_id=uuid.uuid4().hex,
                provider="feishu",
                provider_tenant_key=values["provider_tenant_key"],
                provider_account_key=values["provider_account_key"],
                alias_type="open_id",
                alias_value=f"missing-parent-{values['alias_value']}",
                verified_at=datetime(2026, 8, 12, tzinfo=UTC),
            ),
        )
        _assert_integrity_error(
            connection,
            sa.insert(IdentityEventReceipt.__table__).values(
                id=values["receipt"],
                tenant_id=values["tenant"],
                provider="feishu",
                provider_tenant_key=values["provider_tenant_key"],
                provider_account_key=values["provider_account_key"],
                event_type="contact.user.updated",
                event_id=values["event_id"],
                event_hash="not-a-sha256",
            ),
        )
        _assert_integrity_error(
            connection,
            sa.insert(IdentityEventReceipt.__table__).values(
                id=uuid.uuid4().hex,
                tenant_id=values["tenant"],
                provider="feishu",
                provider_tenant_key=values["provider_tenant_key"],
                provider_account_key="unregistered-app",
                event_type="contact.user.updated",
                event_id=f"missing-account-{values['event_id']}",
                event_hash="a" * 64,
            ),
        )
        _assert_integrity_error(
            connection,
            sa.insert(IdentityEventReceipt.__table__).values(
                id=uuid.uuid4().hex,
                tenant_id=values["tenant"],
                provider="feishu",
                provider_tenant_key=values["provider_tenant_key"],
                provider_account_key=values["provider_account_key"],
                event_type="contact.user.updated",
                event_id=f"missing-identity-{values['event_id']}",
                event_hash="a" * 64,
                external_identity_id=uuid.uuid4().hex,
            ),
        )

        connection.execute(
            sa.insert(EnterpriseSubjectLink.__table__).values(
                id=values["subject"],
                tenant_id=values["tenant"],
                user_id=values["user"],
                subject_type="employee_no",
                subject_value=f"employee-{values['subject_value']}",
                issuer="feishu_contact",
                issuer_tenant=values["provider_tenant_key"],
            )
        )
        _assert_integrity_error(
            connection,
            sa.insert(EnterpriseSubjectLink.__table__).values(
                id=uuid.uuid4().hex,
                tenant_id=values["tenant"],
                user_id=values["user_2"],
                subject_type="employee_no",
                subject_value=f"employee-{values['subject_value']}",
                issuer="hr",
                issuer_tenant="hr-authoritative",
            ),
        )

        receipt_values = {
            "tenant_id": values["tenant"],
            "provider": "feishu",
            "provider_tenant_key": values["provider_tenant_key"],
            "provider_account_key": values["provider_account_key"],
            "event_type": "contact.user.updated",
            "event_id": values["event_id"],
            "event_hash": "a" * 64,
        }
        connection.execute(
            sa.insert(IdentityEventReceipt.__table__).values(
                id=values["receipt"],
                **receipt_values,
            )
        )
        _assert_integrity_error(
            connection,
            sa.insert(IdentityEventReceipt.__table__).values(
                id=uuid.uuid4().hex,
                **receipt_values,
            ),
        )
    finally:
        transaction.rollback()
        connection.close()


async def _insert_duplicate_alias(
    factory: async_sessionmaker[AsyncSession],
    *,
    values: dict[str, str],
) -> bool:
    async with factory() as session:
        session.add(
            ExternalIdentityAlias(
                id=uuid.uuid4().hex,
                tenant_id=values["tenant"],
                external_identity_id=values["identity"],
                provider="feishu",
                provider_tenant_key=values["provider_tenant_key"],
                provider_account_key=values["provider_account_key"],
                alias_type="open_id",
                alias_value=values["alias_value"],
                verified_at=datetime.now(UTC),
            )
        )
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            return False
        return True


async def _cleanup_async_scope(
    factory: async_sessionmaker[AsyncSession],
    values: dict[str, str],
) -> None:
    async with factory.begin() as session:
        for model in (
            IdentityEventReceipt,
            ExternalIdentityAlias,
            EnterpriseSubjectLink,
            ExternalIdentity,
            IdentityProviderChannelLink,
            IdentityProviderAccount,
            IdentityProviderTenant,
        ):
            await session.execute(sa.delete(model).where(model.tenant_id.in_([values["tenant"], values["tenant_2"]])))
        await session.execute(
            sa.delete(ChatChannel).where(
                ChatChannel.id.in_(
                    [
                        values["channel"],
                        values["channel_2"],
                        values["channel_3"],
                    ]
                )
            )
        )
        await session.execute(sa.delete(User).where(User.id.in_([values["user"], values["user_2"]])))
        await session.execute(sa.delete(Tenant).where(Tenant.id.in_([values["tenant"], values["tenant_2"]])))


async def test_concurrent_open_id_first_resolution_has_one_database_winner(
    bootstrapped_engine: sa.Engine,
    bootstrapped_async_engine: AsyncEngine,
) -> None:
    values = _ids()
    with bootstrapped_engine.begin() as connection:
        _seed_scope(connection, values)

    factory = async_sessionmaker(
        bootstrapped_async_engine,
        expire_on_commit=False,
    )
    try:
        results = await asyncio.gather(
            _insert_duplicate_alias(factory, values=values),
            _insert_duplicate_alias(factory, values=values),
        )

        assert sorted(results) == [False, True]
        async with factory() as session:
            count = await session.scalar(
                sa.select(sa.func.count())
                .select_from(ExternalIdentityAlias)
                .where(
                    ExternalIdentityAlias.tenant_id == values["tenant"],
                    ExternalIdentityAlias.provider == "feishu",
                    ExternalIdentityAlias.provider_tenant_key == values["provider_tenant_key"],
                    ExternalIdentityAlias.provider_account_key == values["provider_account_key"],
                    ExternalIdentityAlias.alias_type == "open_id",
                    ExternalIdentityAlias.alias_value == values["alias_value"],
                )
            )
        assert count == 1
    finally:
        await _cleanup_async_scope(factory, values)
