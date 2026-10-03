"""PostgreSQL contracts for EIM-I6 provisioning persistence."""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import IntegrityError, OperationalError

from api.db import UserAccountKind, UserTenantRole
from api.db.db_models import (
    ExternalIdentity,
    IdentityBindingEvent,
    IdentityLinkCode,
    IdentityProviderAccount,
    IdentityProviderTenant,
    IdentityTenantPolicy,
    Tenant,
    User,
    UserTenant,
)

_I6_REVISION = "b4c6d8e0f2a4"
_I21_REVISION = "9a3b5c7d8e0f"
_SCHEMA = "usr_ai"
_ACTIVE_MEMBERSHIP_UNIQUE = "uq_user_tenants_active_tenant_user"
_EXTERNAL_IDENTITY_REVERSE_UNIQUE = "uq_external_identities_tenant_user_provider_subject_type"
_PROVISIONING_TABLES = (
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
    IdentityTenantPolicy.__tablename__: {
        "id",
        "tenant_id",
        "mode",
        "revision",
        "link_code_ttl_seconds",
        "changed_at",
    },
    IdentityLinkCode.__tablename__: {
        "id",
        "tenant_id",
        "provider",
        "provider_tenant_key",
        "provider_account_key",
        "target_user_id",
        "digest_key_id",
        "code_digest",
        "policy_revision",
        "provider_account_revision",
        "provider_account_last_scope_change_at",
        "state",
        "issued_at",
        "expires_at",
        "consumed_at",
        "revoked_at",
        "consumed_external_identity_id",
    },
    IdentityBindingEvent.__tablename__: {
        "id",
        "tenant_id",
        "provider",
        "provider_tenant_key",
        "provider_account_key",
        "external_identity_id",
        "target_user_id",
        "actor_user_id",
        "link_code_id",
        "binding_method",
        "previous_account_kind",
        "result_account_kind",
        "policy_revision",
        "provider_verified_at",
        "occurred_at",
        "request_digest_key_id",
        "request_digest",
    },
}


def _ids() -> dict[str, str]:
    suffix = uuid.uuid4().hex[:12]
    return {
        "tenant": f"t-{suffix}",
        "user": f"u-{suffix}",
        "user_2": f"v-{suffix}",
        "provider_tenant": f"p-{suffix}",
        "provider_account": f"a-{suffix}",
        "provider_tenant_key": f"tenant-key-{suffix}",
        "provider_account_key": f"app-{suffix}",
        "identity": f"i-{suffix}",
        "identity_2": f"j-{suffix}",
        "membership": f"m-{suffix}",
        "link_code": f"c-{suffix}",
        "binding_event": f"e-{suffix}",
    }


def _tenant_values(tenant_id: str) -> dict[str, Any]:
    return {
        "id": tenant_id,
        "name": "I6 test tenant",
        "llm_id": "test-llm",
        "embd_id": "test-embedding",
        "asr_id": "test-asr",
        "img2txt_id": "test-image",
        "parser_ids": "naive",
    }


def _user_values(user_id: str) -> dict[str, Any]:
    return {
        "id": user_id,
        "nickname": "I6 test user",
        "email": None,
        "password": None,
        "account_kind": UserAccountKind.EXTERNAL.value,
        "is_authenticated": True,
        "is_active": True,
        "is_anonymous": False,
    }


def _migration_config(alembic_cfg: Config, connection: sa.Connection) -> Config:
    cfg = Config(alembic_cfg.config_file_name)
    cfg.set_main_option(
        "script_location",
        alembic_cfg.get_main_option("script_location"),
    )
    cfg.attributes["connection"] = connection
    return cfg


def _assert_integrity_error(
    connection: sa.Connection,
    statement: sa.Executable,
) -> None:
    with pytest.raises(IntegrityError), connection.begin_nested():
        connection.execute(statement)


def _seed_provider_scope(
    connection: sa.Connection,
    values: dict[str, str],
    *,
    with_policy: bool = True,
    with_identity: bool = True,
) -> datetime:
    now = datetime(2026, 8, 13, tzinfo=UTC)
    connection.execute(sa.insert(Tenant.__table__).values(**_tenant_values(values["tenant"])))
    connection.execute(
        sa.insert(User.__table__),
        [_user_values(values["user"]), _user_values(values["user_2"])],
    )
    connection.execute(
        sa.insert(UserTenant.__table__).values(
            id=values["membership"],
            tenant_id=values["tenant"],
            user_id=values["user"],
            role=UserTenantRole.NORMAL.value,
            invited_by=values["user"],
            status="1",
        )
    )
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
        sa.insert(IdentityProviderAccount.__table__).values(
            id=values["provider_account"],
            tenant_id=values["tenant"],
            provider="feishu",
            provider_tenant_key=values["provider_tenant_key"],
            provider_account_key=values["provider_account_key"],
            identity_revision=3,
            last_scope_change_at=now,
            identity_health_state="healthy",
        )
    )
    if with_policy:
        connection.execute(
            sa.insert(IdentityTenantPolicy.__table__).values(
                id=values["tenant"],
                tenant_id=values["tenant"],
                mode="link_only",
                revision=7,
                link_code_ttl_seconds=600,
                changed_at=now,
            )
        )
    if with_identity:
        connection.execute(
            sa.insert(ExternalIdentity.__table__).values(
                id=values["identity"],
                tenant_id=values["tenant"],
                user_id=values["user"],
                provider="feishu",
                provider_tenant_key=values["provider_tenant_key"],
                subject_type="user_id",
                subject_value=f"subject-{values['identity']}",
                state="active",
                verified_at=now,
            )
        )
    return now


def _link_code_values(
    values: dict[str, str],
    now: datetime,
    *,
    link_code_id: str | None = None,
    digest: str = "a" * 64,
    state: str = "pending",
) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": link_code_id or values["link_code"],
        "tenant_id": values["tenant"],
        "provider": "feishu",
        "provider_tenant_key": values["provider_tenant_key"],
        "provider_account_key": values["provider_account_key"],
        "target_user_id": values["user"],
        "digest_key_id": "link-hmac-v1",
        "code_digest": digest,
        "policy_revision": 7,
        "provider_account_revision": 3,
        "provider_account_last_scope_change_at": now,
        "state": state,
        "issued_at": now,
        "expires_at": now + timedelta(minutes=10),
    }
    if state == "revoked":
        payload["revoked_at"] = now + timedelta(seconds=1)
    return payload


def test_fresh_schema_has_exact_i6_tables_constraints_and_no_raw_codes(
    bootstrapped_engine: sa.Engine,
) -> None:
    inspector = sa.inspect(bootstrapped_engine)

    for table_name, domain_columns in _EXPECTED_COLUMNS.items():
        columns = {str(column["name"]): column for column in inspector.get_columns(table_name, schema=_SCHEMA)}
        assert set(columns) == domain_columns | _AUDIT_COLUMNS
        assert columns["id"]["nullable"] is False

    policy_columns = {str(column["name"]): column for column in inspector.get_columns(IdentityTenantPolicy.__tablename__, schema=_SCHEMA)}
    code_columns = {str(column["name"]): column for column in inspector.get_columns(IdentityLinkCode.__tablename__, schema=_SCHEMA)}
    assert policy_columns["mode"]["default"] is None
    assert policy_columns["link_code_ttl_seconds"]["default"] is None
    assert "1" in str(policy_columns["revision"]["default"])
    assert "pending" in str(code_columns["state"]["default"])
    assert not set(code_columns).intersection({"code", "raw_code", "link_code", "token", "secret", "payload"})

    identity_unique = {str(constraint["name"]): tuple(constraint["column_names"]) for constraint in inspector.get_unique_constraints(ExternalIdentity.__tablename__, schema=_SCHEMA)}
    assert identity_unique[_EXTERNAL_IDENTITY_REVERSE_UNIQUE] == (
        "tenant_id",
        "user_id",
        "provider",
        "provider_tenant_key",
        "subject_type",
    )

    membership_indexes = {str(index["name"]): index for index in inspector.get_indexes(UserTenant.__tablename__, schema=_SCHEMA) if not index.get("duplicates_constraint")}
    active_index = membership_indexes[_ACTIVE_MEMBERSHIP_UNIQUE]
    assert tuple(active_index["column_names"]) == ("tenant_id", "user_id")
    assert active_index["unique"] is True
    assert "status" in str((active_index.get("dialect_options") or {}).get("postgresql_where"))

    membership_foreign_keys = inspector.get_foreign_keys(UserTenant.__tablename__, schema=_SCHEMA)
    assert membership_foreign_keys == []


def test_i21_upgrade_adds_empty_fail_closed_policy_without_backfill(
    bootstrapped_engine: sa.Engine,
    alembic_cfg: Config,
) -> None:
    connection = bootstrapped_engine.connect()
    transaction = connection.begin()
    try:
        cfg = _migration_config(alembic_cfg, connection)
        command.downgrade(cfg, _I21_REVISION)
        connection.execute(sa.insert(Tenant.__table__).values(**_tenant_values(_ids()["tenant"])))

        command.upgrade(cfg, _I6_REVISION)

        inspector = sa.inspect(connection)
        assert all(inspector.has_table(table.name, schema=_SCHEMA) for table in _PROVISIONING_TABLES)
        assert connection.scalar(sa.select(sa.func.count()).select_from(IdentityTenantPolicy)) == 0
        assert connection.execute(sa.text("SELECT version_num FROM usr_ai.alembic_version")).scalar_one() == _I6_REVISION
    finally:
        transaction.rollback()
        connection.close()


def test_i6_empty_downgrade_upgrade_round_trip(
    bootstrapped_engine: sa.Engine,
    alembic_cfg: Config,
) -> None:
    connection = bootstrapped_engine.connect()
    transaction = connection.begin()
    try:
        cfg = _migration_config(alembic_cfg, connection)
        command.downgrade(cfg, _I21_REVISION)

        inspector = sa.inspect(connection)
        assert all(not inspector.has_table(table.name, schema=_SCHEMA) for table in _PROVISIONING_TABLES)
        assert _EXTERNAL_IDENTITY_REVERSE_UNIQUE not in {constraint["name"] for constraint in inspector.get_unique_constraints(ExternalIdentity.__tablename__, schema=_SCHEMA)}
        assert _ACTIVE_MEMBERSHIP_UNIQUE not in {index["name"] for index in inspector.get_indexes(UserTenant.__tablename__, schema=_SCHEMA)}

        command.upgrade(cfg, _I6_REVISION)

        assert connection.execute(sa.text("SELECT version_num FROM usr_ai.alembic_version")).scalar_one() == _I6_REVISION
    finally:
        transaction.rollback()
        connection.close()


@pytest.mark.parametrize(
    ("duplicate_kind", "summary_key"),
    [
        ("membership", "active_membership_duplicate_groups=1"),
        ("identity", "external_identity_reverse_duplicate_groups=1"),
    ],
)
def test_i6_upgrade_rejects_legacy_duplicate_ownership_without_identifiers(
    bootstrapped_engine: sa.Engine,
    alembic_cfg: Config,
    duplicate_kind: str,
    summary_key: str,
) -> None:
    values = _ids()
    connection = bootstrapped_engine.connect()
    transaction = connection.begin()
    try:
        cfg = _migration_config(alembic_cfg, connection)
        command.downgrade(cfg, _I21_REVISION)
        now = _seed_provider_scope(
            connection,
            values,
            with_policy=False,
            with_identity=True,
        )
        if duplicate_kind == "membership":
            connection.execute(
                sa.insert(UserTenant.__table__).values(
                    id=uuid.uuid4().hex,
                    tenant_id=values["tenant"],
                    user_id=values["user"],
                    role=UserTenantRole.ADMIN.value,
                    invited_by=values["user"],
                    status="1",
                )
            )
        else:
            connection.execute(
                sa.insert(ExternalIdentity.__table__).values(
                    id=values["identity_2"],
                    tenant_id=values["tenant"],
                    user_id=values["user"],
                    provider="feishu",
                    provider_tenant_key=values["provider_tenant_key"],
                    subject_type="user_id",
                    subject_value=f"second-subject-{values['identity_2']}",
                    state="active",
                    verified_at=now,
                )
            )

        with pytest.raises(RuntimeError, match="duplicate ownership") as exc_info:
            command.upgrade(cfg, _I6_REVISION)

        message = str(exc_info.value)
        assert summary_key in message
        assert values["tenant"] not in message
        assert values["user"] not in message
        assert values["provider_tenant_key"] not in message
        assert connection.execute(sa.text("SELECT version_num FROM usr_ai.alembic_version")).scalar_one() == _I21_REVISION
    finally:
        transaction.rollback()
        connection.close()


def test_model_first_rejects_weakened_i6_check_without_advancing_version(
    bootstrapped_engine: sa.Engine,
    alembic_cfg: Config,
) -> None:
    connection = bootstrapped_engine.connect()
    transaction = connection.begin()
    try:
        connection.execute(sa.text("ALTER TABLE usr_ai.t_ai_identity_link_codes DROP CONSTRAINT ck_identity_link_codes_lifetime"))
        connection.execute(sa.text("ALTER TABLE usr_ai.t_ai_identity_link_codes ADD CONSTRAINT ck_identity_link_codes_lifetime CHECK (true)"))
        connection.execute(
            sa.text("UPDATE usr_ai.alembic_version SET version_num = :revision"),
            {"revision": _I21_REVISION},
        )

        with pytest.raises(RuntimeError, match="check constraints"):
            command.upgrade(
                _migration_config(alembic_cfg, connection),
                _I6_REVISION,
            )

        assert connection.execute(sa.text("SELECT version_num FROM usr_ai.alembic_version")).scalar_one() == _I21_REVISION
    finally:
        transaction.rollback()
        connection.close()


@pytest.mark.parametrize(
    ("table_name", "index_name", "columns", "permissive_predicate"),
    [
        (
            IdentityLinkCode.__tablename__,
            "uq_identity_link_codes_pending_target_account",
            (
                "tenant_id",
                "provider",
                "provider_tenant_key",
                "provider_account_key",
                "target_user_id",
            ),
            "state = 'pending' OR true",
        ),
        (
            UserTenant.__tablename__,
            _ACTIVE_MEMBERSHIP_UNIQUE,
            ("tenant_id", "user_id"),
            "status = '1' OR true",
        ),
    ],
)
def test_model_first_rejects_permissive_partial_unique_index(
    bootstrapped_engine: sa.Engine,
    alembic_cfg: Config,
    table_name: str,
    index_name: str,
    columns: tuple[str, ...],
    permissive_predicate: str,
) -> None:
    connection = bootstrapped_engine.connect()
    transaction = connection.begin()
    try:
        connection.execute(sa.text(f'DROP INDEX usr_ai."{index_name}"'))
        quoted_columns = ", ".join(f'"{column}"' for column in columns)
        connection.execute(sa.text(f'CREATE UNIQUE INDEX "{index_name}" ON usr_ai."{table_name}" ({quoted_columns}) WHERE {permissive_predicate}'))
        connection.execute(
            sa.text("UPDATE usr_ai.alembic_version SET version_num = :revision"),
            {"revision": _I21_REVISION},
        )

        with pytest.raises(RuntimeError, match="partial unique index"):
            command.upgrade(
                _migration_config(alembic_cfg, connection),
                _I6_REVISION,
            )

        assert connection.execute(sa.text("SELECT version_num FROM usr_ai.alembic_version")).scalar_one() == _I21_REVISION
    finally:
        transaction.rollback()
        connection.close()


def test_i21_upgrade_takes_one_lock_in_runtime_order_before_ddl(
    bootstrapped_engine: sa.Engine,
    alembic_cfg: Config,
) -> None:
    connection = bootstrapped_engine.connect()
    transaction = connection.begin()
    lock_statements: list[str] = []
    listener_installed = False

    def _capture_lock(
        _connection: sa.Connection,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        normalized = " ".join(statement.lower().split())
        if normalized.startswith("lock table"):
            lock_statements.append(normalized)

    try:
        cfg = _migration_config(alembic_cfg, connection)
        command.downgrade(cfg, _I21_REVISION)
        sa.event.listen(connection, "after_cursor_execute", _capture_lock)
        listener_installed = True

        command.upgrade(cfg, _I6_REVISION)

        assert len(lock_statements) == 1
        lock_sql = lock_statements[0]
        expected_order = (
            IdentityProviderAccount.__tablename__,
            UserTenant.__tablename__,
            ExternalIdentity.__tablename__,
        )
        assert [lock_sql.index(table_name) for table_name in expected_order] == sorted(lock_sql.index(table_name) for table_name in expected_order)
        assert IdentityTenantPolicy.__tablename__ not in lock_sql
        assert IdentityLinkCode.__tablename__ not in lock_sql
        assert IdentityBindingEvent.__tablename__ not in lock_sql
    finally:
        if listener_installed:
            sa.event.remove(connection, "after_cursor_execute", _capture_lock)
        transaction.rollback()
        connection.close()


def test_database_enforces_policy_membership_identity_and_code_invariants(
    bootstrapped_engine: sa.Engine,
) -> None:
    values = _ids()
    connection = bootstrapped_engine.connect()
    transaction = connection.begin()
    try:
        now = _seed_provider_scope(connection, values)

        mismatched = _ids()
        connection.execute(sa.insert(Tenant.__table__).values(**_tenant_values(mismatched["tenant"])))
        _assert_integrity_error(
            connection,
            sa.insert(IdentityTenantPolicy.__table__).values(
                id=mismatched["user_2"],
                tenant_id=mismatched["tenant"],
                mode="jit",
                revision=1,
                link_code_ttl_seconds=600,
                changed_at=now,
            ),
        )
        for bad_ttl in (59, 901):
            other = _ids()
            connection.execute(sa.insert(Tenant.__table__).values(**_tenant_values(other["tenant"])))
            _assert_integrity_error(
                connection,
                sa.insert(IdentityTenantPolicy.__table__).values(
                    id=other["tenant"],
                    tenant_id=other["tenant"],
                    mode="link_only",
                    revision=1,
                    link_code_ttl_seconds=bad_ttl,
                    changed_at=now,
                ),
            )

        connection.execute(
            sa.insert(UserTenant.__table__).values(
                id=uuid.uuid4().hex,
                tenant_id=values["tenant"],
                user_id=values["user"],
                role=UserTenantRole.NORMAL.value,
                invited_by=values["user"],
                status="0",
            )
        )
        _assert_integrity_error(
            connection,
            sa.insert(UserTenant.__table__).values(
                id=uuid.uuid4().hex,
                tenant_id=values["tenant"],
                user_id=values["user"],
                role=UserTenantRole.ADMIN.value,
                invited_by=values["user"],
                status="1",
            ),
        )

        _assert_integrity_error(
            connection,
            sa.insert(ExternalIdentity.__table__).values(
                id=values["identity_2"],
                tenant_id=values["tenant"],
                user_id=values["user"],
                provider="feishu",
                provider_tenant_key=values["provider_tenant_key"],
                subject_type="user_id",
                subject_value=f"other-{values['identity_2']}",
                state="revoked",
            ),
        )

        connection.execute(sa.insert(IdentityLinkCode.__table__).values(**_link_code_values(values, now)))
        _assert_integrity_error(
            connection,
            sa.insert(IdentityLinkCode.__table__).values(
                **_link_code_values(
                    values,
                    now,
                    link_code_id=uuid.uuid4().hex,
                    digest="b" * 64,
                )
            ),
        )
        invalid_expiry = _link_code_values(
            values,
            now,
            link_code_id=uuid.uuid4().hex,
            digest="c" * 64,
            state="revoked",
        )
        invalid_expiry["expires_at"] = now + timedelta(minutes=16)
        _assert_integrity_error(
            connection,
            sa.insert(IdentityLinkCode.__table__).values(**invalid_expiry),
        )

        connection.execute(
            sa.insert(IdentityLinkCode.__table__).values(
                **_link_code_values(
                    values,
                    now,
                    link_code_id=uuid.uuid4().hex,
                    digest="d" * 64,
                    state="revoked",
                )
            )
        )
    finally:
        transaction.rollback()
        connection.close()


def test_database_enforces_binding_event_method_and_scope(
    bootstrapped_engine: sa.Engine,
) -> None:
    values = _ids()
    connection = bootstrapped_engine.connect()
    transaction = connection.begin()
    try:
        now = _seed_provider_scope(connection, values)
        base_event = {
            "id": values["binding_event"],
            "tenant_id": values["tenant"],
            "provider": "feishu",
            "provider_tenant_key": values["provider_tenant_key"],
            "provider_account_key": values["provider_account_key"],
            "external_identity_id": values["identity"],
            "target_user_id": values["user"],
            "actor_user_id": None,
            "link_code_id": None,
            "binding_method": "jit",
            "previous_account_kind": None,
            "result_account_kind": "external",
            "policy_revision": 7,
            "provider_verified_at": now,
            "occurred_at": now,
            "request_digest_key_id": "event-hmac-v1",
            "request_digest": "e" * 64,
        }
        explicit_bypass = {
            **base_event,
            "id": uuid.uuid4().hex,
            "binding_method": "explicit_link",
            "request_digest": "f" * 64,
        }
        _assert_integrity_error(
            connection,
            sa.insert(IdentityBindingEvent.__table__).values(**explicit_bypass),
        )

        wrong_scope = {
            **base_event,
            "id": uuid.uuid4().hex,
            "provider_tenant_key": f"wrong-{values['provider_tenant_key']}",
            "request_digest": "0" * 64,
        }
        _assert_integrity_error(
            connection,
            sa.insert(IdentityBindingEvent.__table__).values(**wrong_scope),
        )

        connection.execute(sa.insert(IdentityBindingEvent.__table__).values(**base_event))
    finally:
        transaction.rollback()
        connection.close()


@pytest.mark.parametrize(
    "table_name",
    [
        IdentityTenantPolicy.__tablename__,
        IdentityLinkCode.__tablename__,
        IdentityBindingEvent.__tablename__,
    ],
)
def test_i6_downgrade_refuses_to_destroy_provisioning_state(
    bootstrapped_engine: sa.Engine,
    alembic_cfg: Config,
    table_name: str,
) -> None:
    values = _ids()
    connection = bootstrapped_engine.connect()
    transaction = connection.begin()
    try:
        now = _seed_provider_scope(connection, values)
        if table_name == IdentityLinkCode.__tablename__:
            connection.execute(sa.insert(IdentityLinkCode.__table__).values(**_link_code_values(values, now)))
            connection.execute(sa.delete(IdentityTenantPolicy))
        elif table_name == IdentityBindingEvent.__tablename__:
            connection.execute(
                sa.insert(IdentityBindingEvent.__table__).values(
                    id=values["binding_event"],
                    tenant_id=values["tenant"],
                    provider="feishu",
                    provider_tenant_key=values["provider_tenant_key"],
                    provider_account_key=values["provider_account_key"],
                    external_identity_id=values["identity"],
                    target_user_id=values["user"],
                    binding_method="jit",
                    result_account_kind="external",
                    policy_revision=7,
                    provider_verified_at=now,
                    occurred_at=now,
                    request_digest_key_id="event-hmac-v1",
                    request_digest="1" * 64,
                )
            )
            connection.execute(sa.delete(IdentityTenantPolicy))

        with pytest.raises(RuntimeError, match="policy, code, or binding history") as exc_info:
            command.downgrade(
                _migration_config(alembic_cfg, connection),
                _I21_REVISION,
            )

        message = str(exc_info.value)
        assert f"{table_name}=1" in message
        assert values["tenant"] not in message
        assert values["provider_tenant_key"] not in message
        assert connection.execute(sa.text("SELECT version_num FROM usr_ai.alembic_version")).scalar_one() == _I6_REVISION
    finally:
        transaction.rollback()
        connection.close()


def test_i6_downgrade_lock_blocks_concurrent_policy_insert(
    bootstrapped_engine: sa.Engine,
    alembic_cfg: Config,
) -> None:
    values = _ids()
    with bootstrapped_engine.begin() as connection:
        connection.execute(sa.insert(Tenant.__table__).values(**_tenant_values(values["tenant"])))

    lock_acquired = threading.Event()
    contender_finished = threading.Event()
    lock_statements: list[str] = []
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
        if normalized.startswith("lock table"):
            lock_statements.append(normalized)
        if normalized.startswith("lock table") and IdentityTenantPolicy.__tablename__ in normalized:
            lock_acquired.set()
            if not contender_finished.wait(timeout=5):
                raise AssertionError("concurrent policy insert did not reach lock timeout")

    def _attempt_policy_insert() -> str | None:
        if not lock_acquired.wait(timeout=5):
            raise AssertionError("I6 downgrade did not acquire transition locks")
        with bootstrapped_engine.connect() as contender:
            contender_transaction = contender.begin()
            try:
                contender.execute(sa.text("SET LOCAL lock_timeout = '1s'"))
                contender.execute(
                    sa.insert(IdentityTenantPolicy.__table__).values(
                        id=values["tenant"],
                        tenant_id=values["tenant"],
                        mode="link_only",
                        revision=1,
                        link_code_ttl_seconds=600,
                        changed_at=datetime.now(UTC),
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
            contender = executor.submit(_attempt_policy_insert)
            command.downgrade(
                _migration_config(alembic_cfg, downgrade_connection),
                _I21_REVISION,
            )
            assert contender.result(timeout=5) == "55P03"
            assert len(lock_statements) == 3
            assert lock_statements[0] == "lock table usr_ai.t_ai_agent_execution_origins in access exclusive mode"
            assert lock_statements[1] == "lock table usr_ai.t_document_source_recovery in access exclusive mode"
            lock_sql = lock_statements[2]
            expected_order = (
                IdentityTenantPolicy.__tablename__,
                IdentityProviderAccount.__tablename__,
                UserTenant.__tablename__,
                ExternalIdentity.__tablename__,
                IdentityLinkCode.__tablename__,
                IdentityBindingEvent.__tablename__,
            )
            positions = [lock_sql.index(table_name) for table_name in expected_order]
            assert positions == sorted(positions)
    finally:
        sa.event.remove(
            downgrade_connection,
            "after_cursor_execute",
            _after_cursor_execute,
        )
        downgrade_transaction.rollback()
        downgrade_connection.close()
        with bootstrapped_engine.begin() as cleanup:
            cleanup.execute(sa.delete(Tenant).where(Tenant.id == values["tenant"]))
