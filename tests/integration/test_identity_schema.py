"""PostgreSQL contract tests for canonical enterprise identity persistence."""

from __future__ import annotations

import asyncio
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
    IdentityEventReceipt,
    IdentityProviderAccount,
    IdentityProviderTenant,
    Tenant,
    User,
)

_I2_REVISION = "8f2c4d6e7a9b"
_PRE_I2_REVISION = "7c8d9e0f1a2b"
_SCHEMA = "usr_ai"
_CHAT_TENANT_SCOPE_UNIQUE = "uq_chat_channels_tenant_scope"
_IDENTITY_TABLES = (
    IdentityProviderTenant.__table__,
    IdentityProviderAccount.__table__,
    ExternalIdentity.__table__,
    ExternalIdentityAlias.__table__,
    EnterpriseSubjectLink.__table__,
    IdentityEventReceipt.__table__,
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
        "channel_id",
        "provider",
        "provider_tenant_key",
        "provider_account_key",
        "identity_revision",
        "last_scope_change_at",
        "last_directory_event_at",
        "identity_health_state",
        "identity_health_error_code",
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


def _drop_i2_sidecar(connection: sa.Connection) -> None:
    inspector = sa.inspect(connection)
    for table in reversed(_IDENTITY_TABLES):
        if inspector.has_table(table.name, schema=_SCHEMA):
            connection.execute(sa.text(f'DROP TABLE {_SCHEMA}."{table.name}"'))
            inspector = sa.inspect(connection)
    chat_unique = {
        constraint["name"]
        for constraint in inspector.get_unique_constraints(
            ChatChannel.__tablename__,
            schema=_SCHEMA,
        )
    }
    if _CHAT_TENANT_SCOPE_UNIQUE in chat_unique:
        connection.execute(sa.text(f"ALTER TABLE {_SCHEMA}.{ChatChannel.__tablename__} DROP CONSTRAINT {_CHAT_TENANT_SCOPE_UNIQUE}"))


def _seed_scope(connection: sa.Connection, values: dict[str, str]) -> None:
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
            channel_id=values["channel"],
            provider="feishu",
            provider_tenant_key=values["provider_tenant_key"],
            provider_account_key=values["provider_account_key"],
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


def test_stored_database_upgrade_creates_i2_sidecar(
    bootstrapped_engine: sa.Engine,
    alembic_cfg: Config,
) -> None:
    connection = bootstrapped_engine.connect()
    transaction = connection.begin()
    try:
        _drop_i2_sidecar(connection)
        connection.execute(
            sa.text("UPDATE usr_ai.alembic_version SET version_num = :revision"),
            {"revision": _PRE_I2_REVISION},
        )

        command.upgrade(_migration_config(alembic_cfg, connection), _I2_REVISION)

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
        assert connection.execute(sa.text("SELECT version_num FROM usr_ai.alembic_version")).scalar_one() == _I2_REVISION
    finally:
        transaction.rollback()
        connection.close()


def test_model_first_sidecar_is_validated_instead_of_recreated(
    bootstrapped_engine: sa.Engine,
    alembic_cfg: Config,
) -> None:
    connection = bootstrapped_engine.connect()
    transaction = connection.begin()
    try:
        connection.execute(
            sa.text("UPDATE usr_ai.alembic_version SET version_num = :revision"),
            {"revision": _PRE_I2_REVISION},
        )

        command.upgrade(_migration_config(alembic_cfg, connection), _I2_REVISION)

        assert connection.execute(sa.text("SELECT version_num FROM usr_ai.alembic_version")).scalar_one() == _I2_REVISION
    finally:
        transaction.rollback()
        connection.close()


def test_model_first_rejects_same_name_weakened_check_without_advancing_version(
    bootstrapped_engine: sa.Engine,
    alembic_cfg: Config,
) -> None:
    connection = bootstrapped_engine.connect()
    transaction = connection.begin()
    try:
        connection.execute(sa.text("ALTER TABLE usr_ai.t_ai_external_identities DROP CONSTRAINT ck_external_identities_state"))
        connection.execute(sa.text("ALTER TABLE usr_ai.t_ai_external_identities ADD CONSTRAINT ck_external_identities_state CHECK (true)"))
        connection.execute(
            sa.text("UPDATE usr_ai.alembic_version SET version_num = :revision"),
            {"revision": _PRE_I2_REVISION},
        )

        with pytest.raises(RuntimeError, match="check constraints"):
            command.upgrade(
                _migration_config(alembic_cfg, connection),
                _I2_REVISION,
            )

        assert connection.execute(sa.text("SELECT version_num FROM usr_ai.alembic_version")).scalar_one() == _PRE_I2_REVISION
    finally:
        transaction.rollback()
        connection.close()


def test_model_first_rejects_wrong_server_default_without_advancing_version(
    bootstrapped_engine: sa.Engine,
    alembic_cfg: Config,
) -> None:
    connection = bootstrapped_engine.connect()
    transaction = connection.begin()
    try:
        connection.execute(sa.text("ALTER TABLE usr_ai.t_ai_identity_event_receipts ALTER COLUMN processing_state SET DEFAULT 'succeeded'"))
        connection.execute(
            sa.text("UPDATE usr_ai.alembic_version SET version_num = :revision"),
            {"revision": _PRE_I2_REVISION},
        )

        with pytest.raises(RuntimeError, match="server defaults"):
            command.upgrade(
                _migration_config(alembic_cfg, connection),
                _I2_REVISION,
            )

        assert connection.execute(sa.text("SELECT version_num FROM usr_ai.alembic_version")).scalar_one() == _PRE_I2_REVISION
    finally:
        transaction.rollback()
        connection.close()


def test_upgrade_rejects_partial_identity_schema_without_advancing_version(
    bootstrapped_engine: sa.Engine,
    alembic_cfg: Config,
) -> None:
    connection = bootstrapped_engine.connect()
    transaction = connection.begin()
    try:
        connection.execute(sa.text("DROP TABLE usr_ai.t_ai_identity_event_receipts"))
        connection.execute(
            sa.text("UPDATE usr_ai.alembic_version SET version_num = :revision"),
            {"revision": _PRE_I2_REVISION},
        )

        with pytest.raises(RuntimeError, match="identity schema is incomplete"):
            command.upgrade(
                _migration_config(alembic_cfg, connection),
                _I2_REVISION,
            )

        assert connection.execute(sa.text("SELECT version_num FROM usr_ai.alembic_version")).scalar_one() == _PRE_I2_REVISION
    finally:
        transaction.rollback()
        connection.close()


def test_empty_i2_sidecar_downgrades_and_upgrades_round_trip(
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
        assert connection.execute(sa.text("SELECT version_num FROM usr_ai.alembic_version")).scalar_one() == _PRE_I2_REVISION

        command.upgrade(cfg, _I2_REVISION)

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
        assert connection.execute(sa.text("SELECT version_num FROM usr_ai.alembic_version")).scalar_one() == _I2_REVISION
    finally:
        transaction.rollback()
        connection.close()


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
        assert head == _I2_REVISION
        assert connection.execute(sa.text("SELECT version_num FROM usr_ai.alembic_version")).scalar_one() == head
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

        # One installation and one Channel cannot be rebound to another account.
        _assert_integrity_error(
            connection,
            sa.insert(IdentityProviderAccount.__table__).values(
                id=uuid.uuid4().hex,
                tenant_id=values["tenant"],
                channel_id=values["channel_3"],
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
                channel_id=values["channel_3"],
                provider="feishu",
                provider_tenant_key=values["provider_tenant_key"],
                provider_account_key=f"unhealthy-{values['provider_account_key']}",
                identity_health_state="error",
                identity_health_error_code=None,
            ),
        )
        _assert_integrity_error(
            connection,
            sa.insert(IdentityProviderAccount.__table__).values(
                id=uuid.uuid4().hex,
                tenant_id=values["tenant"],
                channel_id=values["channel"],
                provider="feishu",
                provider_tenant_key=values["provider_tenant_key"],
                provider_account_key=f"other-{values['provider_account_key']}",
            ),
        )

        # The composite FK prevents an installation from pointing at a Channel
        # owned by another tenant, even when both tenants are otherwise valid.
        _assert_integrity_error(
            connection,
            sa.insert(IdentityProviderAccount.__table__).values(
                id=uuid.uuid4().hex,
                tenant_id=values["tenant_2"],
                channel_id=values["channel"],
                provider="feishu",
                provider_tenant_key=values["provider_tenant_key_2"],
                provider_account_key=f"wrong-tenant-{values['provider_account_key']}",
            ),
        )
        _assert_integrity_error(
            connection,
            sa.insert(IdentityProviderAccount.__table__).values(
                id=uuid.uuid4().hex,
                tenant_id=values["tenant"],
                channel_id=values["channel_3"],
                provider="feishu",
                provider_tenant_key=values["provider_tenant_key_2"],
                provider_account_key=f"wrong-owner-{values['provider_account_key']}",
            ),
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
