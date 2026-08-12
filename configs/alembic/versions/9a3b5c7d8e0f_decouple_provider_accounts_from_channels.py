"""decouple provider accounts from chat channels

Revision ID: 9a3b5c7d8e0f
Revises: 8f2c4d6e7a9b
Create Date: 2026-08-12 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine.reflection import Inspector

revision: str = "9a3b5c7d8e0f"
down_revision: str | None = "8f2c4d6e7a9b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "usr_ai"
PROVIDER_ACCOUNTS = "t_ai_identity_provider_accounts"
PROVIDER_CHANNEL_LINKS = "t_ai_identity_provider_channel_links"
PROVIDER_TENANTS = "t_ai_identity_provider_tenants"
CHAT_CHANNELS = "t_ai_chat_channels"
TENANTS = "t_ai_tenants"

OLD_ACCOUNT_CHANNEL_UNIQUE = "uq_identity_provider_accounts_channel"
OLD_ACCOUNT_CHANNEL_FK = "fk_identity_provider_accounts_channel_tenant"
ACCOUNT_LINK_SCOPE_UNIQUE = "uq_identity_provider_accounts_link_scope"
CHAT_TENANT_SCOPE_UNIQUE = "uq_chat_channels_tenant_scope"
CHAT_IDENTITY_SCOPE_UNIQUE = "uq_chat_channels_identity_scope"

LINK_ACCOUNT_UNIQUE = "uq_identity_provider_channel_links_account"
LINK_CHANNEL_UNIQUE = "uq_identity_provider_channel_links_channel"
LINK_PROVIDER_CHECK = "ck_identity_provider_channel_links_provider_nonempty"
LINK_TENANT_FK = "fk_identity_provider_channel_links_tenant_id"
LINK_ACCOUNT_SCOPE_FK = "fk_identity_provider_channel_links_account_scope"
LINK_CHANNEL_SCOPE_FK = "fk_identity_provider_channel_links_channel_scope"

_BASE_COLUMN_SHAPES: dict[str, tuple[str, int | None, bool]] = {
    "create_date": ("datetime", None, True),
    "update_date": ("datetime", None, True),
    "create_time": ("bigint", None, True),
    "update_time": ("bigint", None, True),
}

_ACCOUNT_SERVER_DEFAULTS = {
    "identity_revision": "1",
    "identity_health_state": "'pending'::character varying",
}

_ACCOUNT_CHECK_SQL = {
    "ck_identity_provider_accounts_health_error": (
        "identity_health_state::text = 'error'::text AND identity_health_error_code IS NOT NULL OR identity_health_state::text <> 'error'::text AND identity_health_error_code IS NULL"
    ),
    "ck_identity_provider_accounts_health_state": (
        "identity_health_state::text = ANY (ARRAY['pending'::character varying, "
        "'healthy'::character varying, 'degraded'::character varying, "
        "'error'::character varying, 'disabled'::character varying]::text[])"
    ),
    "ck_identity_provider_accounts_nonempty": ("btrim(provider::text) <> ''::text AND btrim(provider_tenant_key::text) <> ''::text AND btrim(provider_account_key::text) <> ''::text"),
    "ck_identity_provider_accounts_revision": "identity_revision >= 1",
}

_ACCOUNT_INDEXES = {
    "ix_identity_provider_accounts_tenant_health": (
        "tenant_id",
        "identity_health_state",
    ),
    **{f"ix_usr_ai_{PROVIDER_ACCOUNTS}_{column}": (column,) for column in _BASE_COLUMN_SHAPES},
}

_ACCOUNT_FOREIGN_KEYS = {
    "fk_identity_provider_accounts_provider_tenant": (
        ("tenant_id", "provider", "provider_tenant_key"),
        PROVIDER_TENANTS,
        ("tenant_id", "provider", "provider_tenant_key"),
        "RESTRICT",
    ),
    "fk_identity_provider_accounts_tenant_id": (
        ("tenant_id",),
        TENANTS,
        ("id",),
        "RESTRICT",
    ),
}

_LINK_CHECK_SQL = {
    LINK_PROVIDER_CHECK: "btrim(provider::text) <> ''::text",
}

_LINK_INDEXES = {f"ix_usr_ai_{PROVIDER_CHANNEL_LINKS}_{column}": (column,) for column in _BASE_COLUMN_SHAPES}

_LINK_FOREIGN_KEYS = {
    LINK_TENANT_FK: (
        ("tenant_id",),
        TENANTS,
        ("id",),
        "RESTRICT",
    ),
    LINK_ACCOUNT_SCOPE_FK: (
        ("provider_account_id", "tenant_id", "provider"),
        PROVIDER_ACCOUNTS,
        ("id", "tenant_id", "provider"),
        "RESTRICT",
    ),
    LINK_CHANNEL_SCOPE_FK: (
        ("channel_id", "tenant_id", "provider"),
        CHAT_CHANNELS,
        ("id", "tenant_id", "channel"),
        "RESTRICT",
    ),
}


def _incompatible(
    table_name: str,
    label: str,
    actual: object,
    expected: object,
) -> RuntimeError:
    return RuntimeError(f"Existing {SCHEMA}.{table_name} has incompatible {label}: expected {expected!r}, got {actual!r}")


def _column_shape(column: dict[str, object]) -> tuple[str, int | None, bool]:
    column_type = column["type"]
    nullable = bool(column["nullable"])
    if isinstance(column_type, sa.String):
        return ("string", column_type.length, nullable)
    if isinstance(column_type, sa.BigInteger):
        return ("bigint", None, nullable)
    if isinstance(column_type, sa.DateTime):
        return (
            "timestamptz" if column_type.timezone else "datetime",
            None,
            nullable,
        )
    if isinstance(column_type, postgresql.JSONB):
        return ("jsonb", None, nullable)
    return (type(column_type).__name__, None, nullable)


def _normalize_sql(value: object) -> str:
    return " ".join(str(value).split())


def _unique_constraints(
    inspector: Inspector,
    table_name: str,
) -> dict[str, tuple[str, ...]]:
    return {
        str(constraint["name"]): tuple(constraint["column_names"])
        for constraint in inspector.get_unique_constraints(
            table_name,
            schema=SCHEMA,
        )
    }


def _foreign_keys(
    inspector: Inspector,
    table_name: str,
) -> dict[str, tuple[tuple[str, ...], str, tuple[str, ...], str]]:
    actual: dict[str, tuple[tuple[str, ...], str, tuple[str, ...], str]] = {}
    for foreign_key in inspector.get_foreign_keys(table_name, schema=SCHEMA):
        name = str(foreign_key["name"])
        referred_schema = foreign_key.get("referred_schema")
        if referred_schema != SCHEMA:
            raise _incompatible(
                table_name,
                f"foreign key {name} schema",
                referred_schema,
                SCHEMA,
            )
        actual[name] = (
            tuple(foreign_key["constrained_columns"]),
            str(foreign_key["referred_table"]),
            tuple(foreign_key["referred_columns"]),
            str((foreign_key.get("options") or {}).get("ondelete", "")).upper(),
        )
    return actual


def _assert_constraints_enforced(
    table_name: str,
    constraint_names: set[str],
) -> None:
    """Reject model-first constraints that only look structurally correct.

    PostgreSQL reflection exposes the columns and expressions but not whether
    an FK/CHECK was installed ``NOT VALID``.  It may also omit deferrability
    details depending on the SQLAlchemy dialect version.  Existing rows behind
    such a constraint are not an acceptable ownership boundary, and deferred
    uniqueness/FKs do not match the immediate ORM contract.
    """

    rows = op.get_bind().execute(
        sa.text(
            """
            SELECT
                constraint_record.conname,
                constraint_record.convalidated,
                constraint_record.condeferrable,
                constraint_record.condeferred
            FROM pg_catalog.pg_constraint AS constraint_record
            JOIN pg_catalog.pg_class AS table_record
              ON table_record.oid = constraint_record.conrelid
            JOIN pg_catalog.pg_namespace AS namespace_record
              ON namespace_record.oid = table_record.relnamespace
            WHERE namespace_record.nspname = :schema_name
              AND table_record.relname = :table_name
            """
        ),
        {
            "schema_name": SCHEMA,
            "table_name": table_name,
        },
    )
    actual = {
        str(row.conname): (
            bool(row.convalidated),
            bool(row.condeferrable),
            bool(row.condeferred),
        )
        for row in rows
        if str(row.conname) in constraint_names
    }
    missing = constraint_names - set(actual)
    unsafe = {name: properties for name, properties in actual.items() if properties != (True, False, False)}
    if missing or unsafe:
        raise _incompatible(
            table_name,
            "constraint enforcement",
            {"missing": sorted(missing), "unsafe": unsafe},
            "all declared constraints validated, immediate, and non-deferrable",
        )


def _assert_table_compatible(
    inspector: Inspector,
    table_name: str,
    *,
    columns: dict[str, tuple[str, int | None, bool]],
    server_defaults: dict[str, str],
    unique_constraints: dict[str, tuple[str, ...]],
    check_constraints: dict[str, str],
    indexes: dict[str, tuple[str, ...]],
    foreign_keys: dict[
        str,
        tuple[tuple[str, ...], str, tuple[str, ...], str],
    ],
) -> None:
    if not inspector.has_table(table_name, schema=SCHEMA):
        raise RuntimeError(f"Required table {SCHEMA}.{table_name} does not exist")

    actual_columns = {str(column["name"]): _column_shape(column) for column in inspector.get_columns(table_name, schema=SCHEMA)}
    if actual_columns != columns:
        raise _incompatible(table_name, "columns", actual_columns, columns)

    actual_defaults = {str(column["name"]): _normalize_sql(column["default"]) for column in inspector.get_columns(table_name, schema=SCHEMA) if column.get("default") is not None}
    if actual_defaults != server_defaults:
        raise _incompatible(
            table_name,
            "server defaults",
            actual_defaults,
            server_defaults,
        )

    primary_key = inspector.get_pk_constraint(table_name, schema=SCHEMA)
    if tuple(primary_key.get("constrained_columns") or ()) != ("id",):
        raise _incompatible(
            table_name,
            "primary key",
            primary_key.get("constrained_columns"),
            ["id"],
        )

    actual_unique = _unique_constraints(inspector, table_name)
    if actual_unique != unique_constraints:
        raise _incompatible(
            table_name,
            "unique constraints",
            actual_unique,
            unique_constraints,
        )

    actual_checks = {
        str(constraint["name"]): _normalize_sql(constraint.get("sqltext", ""))
        for constraint in inspector.get_check_constraints(
            table_name,
            schema=SCHEMA,
        )
    }
    if actual_checks != check_constraints:
        raise _incompatible(
            table_name,
            "check constraints",
            actual_checks,
            check_constraints,
        )

    actual_indexes = {str(index["name"]): tuple(index["column_names"]) for index in inspector.get_indexes(table_name, schema=SCHEMA) if not index.get("duplicates_constraint")}
    if actual_indexes != indexes:
        raise _incompatible(
            table_name,
            "indexes",
            actual_indexes,
            indexes,
        )

    actual_foreign_keys = _foreign_keys(inspector, table_name)
    if actual_foreign_keys != foreign_keys:
        raise _incompatible(
            table_name,
            "foreign keys",
            actual_foreign_keys,
            foreign_keys,
        )

    _assert_constraints_enforced(
        table_name,
        set(unique_constraints) | set(check_constraints) | set(foreign_keys),
    )


def _account_columns(*, final: bool) -> dict[str, tuple[str, int | None, bool]]:
    columns = {
        **_BASE_COLUMN_SHAPES,
        "id": ("string", 32, False),
        "tenant_id": ("string", 32, False),
        "provider": ("string", 64, False),
        "provider_tenant_key": ("string", 255, False),
        "provider_account_key": ("string", 255, False),
        "identity_revision": ("bigint", None, False),
        "last_scope_change_at": ("timestamptz", None, True),
        "last_directory_event_at": ("timestamptz", None, True),
        "identity_health_state": ("string", 16, False),
        "identity_health_error_code": ("string", 64, True),
    }
    if not final:
        columns["channel_id"] = ("string", 32, False)
    return columns


def _account_unique_constraints(*, final: bool) -> dict[str, tuple[str, ...]]:
    constraints = {
        "uq_identity_provider_accounts_provider_account": (
            "provider",
            "provider_tenant_key",
            "provider_account_key",
        ),
        "uq_identity_provider_accounts_scope": (
            "tenant_id",
            "provider",
            "provider_tenant_key",
            "provider_account_key",
        ),
    }
    if final:
        constraints[ACCOUNT_LINK_SCOPE_UNIQUE] = (
            "id",
            "tenant_id",
            "provider",
        )
    else:
        constraints[OLD_ACCOUNT_CHANNEL_UNIQUE] = ("channel_id",)
    return constraints


def _account_foreign_keys(
    *,
    final: bool,
) -> dict[str, tuple[tuple[str, ...], str, tuple[str, ...], str]]:
    constraints = dict(_ACCOUNT_FOREIGN_KEYS)
    if not final:
        constraints[OLD_ACCOUNT_CHANNEL_FK] = (
            ("channel_id", "tenant_id"),
            CHAT_CHANNELS,
            ("id", "tenant_id"),
            "RESTRICT",
        )
    return constraints


def _assert_provider_account_shape(
    inspector: Inspector,
    *,
    final: bool,
) -> None:
    _assert_table_compatible(
        inspector,
        PROVIDER_ACCOUNTS,
        columns=_account_columns(final=final),
        server_defaults=_ACCOUNT_SERVER_DEFAULTS,
        unique_constraints=_account_unique_constraints(final=final),
        check_constraints=_ACCOUNT_CHECK_SQL,
        indexes=_ACCOUNT_INDEXES,
        foreign_keys=_account_foreign_keys(final=final),
    )


def _assert_chat_channel_identity_scope(
    inspector: Inspector,
    *,
    final: bool,
) -> None:
    if not inspector.has_table(CHAT_CHANNELS, schema=SCHEMA):
        raise RuntimeError(f"Required table {SCHEMA}.{CHAT_CHANNELS} does not exist")

    columns = {str(column["name"]): _column_shape(column) for column in inspector.get_columns(CHAT_CHANNELS, schema=SCHEMA)}
    expected_columns = {
        "id": ("string", 32, False),
        "tenant_id": ("string", 32, False),
        "channel": ("string", 128, False),
        "config": ("jsonb", None, False),
    }
    actual_columns = {name: columns.get(name) for name in expected_columns}
    if actual_columns != expected_columns:
        raise _incompatible(
            CHAT_CHANNELS,
            "identity scope columns",
            actual_columns,
            expected_columns,
        )

    unique_constraints = _unique_constraints(inspector, CHAT_CHANNELS)
    tenant_scope = unique_constraints.get(CHAT_TENANT_SCOPE_UNIQUE)
    if tenant_scope != ("id", "tenant_id"):
        raise _incompatible(
            CHAT_CHANNELS,
            f"unique constraint {CHAT_TENANT_SCOPE_UNIQUE}",
            tenant_scope,
            ("id", "tenant_id"),
        )

    identity_scope = unique_constraints.get(CHAT_IDENTITY_SCOPE_UNIQUE)
    expected_identity_scope = ("id", "tenant_id", "channel") if final else None
    if identity_scope != expected_identity_scope:
        raise _incompatible(
            CHAT_CHANNELS,
            f"unique constraint {CHAT_IDENTITY_SCOPE_UNIQUE}",
            identity_scope,
            expected_identity_scope,
        )


def _assert_provider_channel_link_shape(inspector: Inspector) -> None:
    _assert_table_compatible(
        inspector,
        PROVIDER_CHANNEL_LINKS,
        columns={
            **_BASE_COLUMN_SHAPES,
            "id": ("string", 32, False),
            "tenant_id": ("string", 32, False),
            "provider": ("string", 64, False),
            "provider_account_id": ("string", 32, False),
            "channel_id": ("string", 32, False),
            "linked_at": ("timestamptz", None, False),
        },
        server_defaults={},
        unique_constraints={
            LINK_ACCOUNT_UNIQUE: ("provider_account_id",),
            LINK_CHANNEL_UNIQUE: ("channel_id",),
        },
        check_constraints=_LINK_CHECK_SQL,
        indexes=_LINK_INDEXES,
        foreign_keys=_LINK_FOREIGN_KEYS,
    )


def _schema_state(inspector: Inspector) -> str:
    """Recognize only the complete I2 or I2.1 transition markers."""

    if not inspector.has_table(PROVIDER_ACCOUNTS, schema=SCHEMA):
        raise RuntimeError(f"Required table {SCHEMA}.{PROVIDER_ACCOUNTS} does not exist")
    if not inspector.has_table(CHAT_CHANNELS, schema=SCHEMA):
        raise RuntimeError(f"Required table {SCHEMA}.{CHAT_CHANNELS} does not exist")

    account_columns = {str(column["name"]) for column in inspector.get_columns(PROVIDER_ACCOUNTS, schema=SCHEMA)}
    account_unique = _unique_constraints(inspector, PROVIDER_ACCOUNTS)
    account_foreign_keys = _foreign_keys(inspector, PROVIDER_ACCOUNTS)
    chat_unique = _unique_constraints(inspector, CHAT_CHANNELS)
    markers = {
        "link_table": inspector.has_table(PROVIDER_CHANNEL_LINKS, schema=SCHEMA),
        "account_channel_column": "channel_id" in account_columns,
        "old_account_channel_unique": OLD_ACCOUNT_CHANNEL_UNIQUE in account_unique,
        "old_account_channel_fk": OLD_ACCOUNT_CHANNEL_FK in account_foreign_keys,
        "account_link_scope_unique": ACCOUNT_LINK_SCOPE_UNIQUE in account_unique,
        "chat_identity_scope_unique": CHAT_IDENTITY_SCOPE_UNIQUE in chat_unique,
    }
    old_markers = {
        "link_table": False,
        "account_channel_column": True,
        "old_account_channel_unique": True,
        "old_account_channel_fk": True,
        "account_link_scope_unique": False,
        "chat_identity_scope_unique": False,
    }
    final_markers = {
        "link_table": True,
        "account_channel_column": False,
        "old_account_channel_unique": False,
        "old_account_channel_fk": False,
        "account_link_scope_unique": True,
        "chat_identity_scope_unique": True,
    }
    if markers == old_markers:
        return "old"
    if markers == final_markers:
        return "final"
    raise RuntimeError(f"Existing enterprise identity schema is incomplete: transition markers={markers!r}")


def _assert_old_shape(inspector: Inspector) -> None:
    if _schema_state(inspector) != "old":
        raise AssertionError("unreachable enterprise identity schema state")
    _assert_provider_account_shape(inspector, final=False)
    _assert_chat_channel_identity_scope(inspector, final=False)


def _assert_final_shape(inspector: Inspector) -> None:
    if _schema_state(inspector) != "final":
        raise AssertionError("unreachable enterprise identity schema state")
    _assert_provider_account_shape(inspector, final=True)
    _assert_chat_channel_identity_scope(inspector, final=True)
    _assert_provider_channel_link_shape(inspector)


def _lock_transition_tables(*, include_link: bool) -> None:
    # Match the control-plane lock order (Channel -> Link -> Account).  LOCK
    # TABLE acquires the listed relations in order, so reversing this during a
    # live migration would create an avoidable deadlock window with Channel
    # updates even though PostgreSQL would eventually choose a victim.
    table_names = [CHAT_CHANNELS]
    if include_link:
        table_names.append(PROVIDER_CHANNEL_LINKS)
    table_names.append(PROVIDER_ACCOUNTS)
    qualified_tables = ", ".join(f'{SCHEMA}."{table_name}"' for table_name in table_names)
    op.get_bind().execute(sa.text(f"LOCK TABLE {qualified_tables} IN ACCESS EXCLUSIVE MODE"))


def _identity_validation_failures() -> dict[str, int]:
    rows = op.get_bind().execute(
        sa.text(
            f"""
            WITH classified AS (
                SELECT CASE
                    WHEN channel.id IS NULL
                        THEN 'missing_channel'
                    WHEN channel.tenant_id <> account.tenant_id
                        THEN 'tenant_mismatch'
                    WHEN account.provider NOT IN ('feishu', 'dingtalk')
                        THEN 'unsupported_provider'
                    WHEN channel.channel <> account.provider
                        THEN 'provider_mismatch'
                    WHEN CASE account.provider
                        WHEN 'feishu'
                            THEN channel.config #> '{{credential,app_id}}'
                        WHEN 'dingtalk'
                            THEN channel.config #> '{{credential,client_id}}'
                    END IS NULL
                        OR CASE account.provider
                            WHEN 'feishu'
                                THEN channel.config #> '{{credential,app_id}}'
                            WHEN 'dingtalk'
                                THEN channel.config #> '{{credential,client_id}}'
                        END = 'null'::jsonb
                        THEN 'missing_account_identity'
                    WHEN jsonb_typeof(
                        CASE account.provider
                            WHEN 'feishu'
                                THEN channel.config #> '{{credential,app_id}}'
                            WHEN 'dingtalk'
                                THEN channel.config #> '{{credential,client_id}}'
                        END
                    ) <> 'string'
                        THEN 'invalid_account_identity_type'
                    WHEN btrim(CASE account.provider
                        WHEN 'feishu'
                            THEN channel.config #>> '{{credential,app_id}}'
                        WHEN 'dingtalk'
                            THEN channel.config #>> '{{credential,client_id}}'
                    END) = ''
                        THEN 'missing_account_identity'
                    WHEN account.provider_account_key <> CASE account.provider
                        WHEN 'feishu'
                            THEN channel.config #>> '{{credential,app_id}}'
                        WHEN 'dingtalk'
                            THEN channel.config #>> '{{credential,client_id}}'
                    END
                        THEN 'account_identity_mismatch'
                END AS category
                FROM {SCHEMA}."{PROVIDER_ACCOUNTS}" AS account
                LEFT JOIN {SCHEMA}."{CHAT_CHANNELS}" AS channel
                  ON channel.id = account.channel_id
            )
            SELECT category, count(*) AS failure_count
            FROM classified
            WHERE category IS NOT NULL
            GROUP BY category
            ORDER BY category
            """
        )
    )
    return {str(row.category): int(row.failure_count) for row in rows}


def _assert_existing_bindings_are_compatible() -> None:
    failures = _identity_validation_failures()
    if not failures:
        return
    summary = ", ".join(f"{category}={failures[category]}" for category in sorted(failures))
    raise RuntimeError(f"Cannot decouple provider accounts from channels: identity binding validation failed ({summary})")


def _create_audit_indexes(table_name: str) -> None:
    for column_name in _BASE_COLUMN_SHAPES:
        op.create_index(
            f"ix_usr_ai_{table_name}_{column_name}",
            table_name,
            [column_name],
            schema=SCHEMA,
        )


def _create_provider_channel_links() -> None:
    op.create_table(
        PROVIDER_CHANNEL_LINKS,
        sa.Column("create_date", sa.DateTime(), nullable=True),
        sa.Column("update_date", sa.DateTime(), nullable=True),
        sa.Column("create_time", sa.BigInteger(), nullable=True),
        sa.Column("update_time", sa.BigInteger(), nullable=True),
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("tenant_id", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("provider_account_id", sa.String(length=32), nullable=False),
        sa.Column("channel_id", sa.String(length=32), nullable=False),
        sa.Column("linked_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "btrim(provider) <> ''",
            name=LINK_PROVIDER_CHECK,
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            [f"{SCHEMA}.{TENANTS}.id"],
            name=LINK_TENANT_FK,
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["provider_account_id", "tenant_id", "provider"],
            [
                f"{SCHEMA}.{PROVIDER_ACCOUNTS}.id",
                f"{SCHEMA}.{PROVIDER_ACCOUNTS}.tenant_id",
                f"{SCHEMA}.{PROVIDER_ACCOUNTS}.provider",
            ],
            name=LINK_ACCOUNT_SCOPE_FK,
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["channel_id", "tenant_id", "provider"],
            [
                f"{SCHEMA}.{CHAT_CHANNELS}.id",
                f"{SCHEMA}.{CHAT_CHANNELS}.tenant_id",
                f"{SCHEMA}.{CHAT_CHANNELS}.channel",
            ],
            name=LINK_CHANNEL_SCOPE_FK,
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "id",
            name=f"pk_{PROVIDER_CHANNEL_LINKS}",
        ),
        sa.UniqueConstraint(
            "provider_account_id",
            name=LINK_ACCOUNT_UNIQUE,
        ),
        sa.UniqueConstraint(
            "channel_id",
            name=LINK_CHANNEL_UNIQUE,
        ),
        schema=SCHEMA,
    )
    _create_audit_indexes(PROVIDER_CHANNEL_LINKS)


def _upgrade_old_shape() -> None:
    _lock_transition_tables(include_link=False)
    inspector = sa.inspect(op.get_bind())
    _assert_old_shape(inspector)
    _assert_existing_bindings_are_compatible()

    op.create_unique_constraint(
        ACCOUNT_LINK_SCOPE_UNIQUE,
        PROVIDER_ACCOUNTS,
        ["id", "tenant_id", "provider"],
        schema=SCHEMA,
    )
    op.create_unique_constraint(
        CHAT_IDENTITY_SCOPE_UNIQUE,
        CHAT_CHANNELS,
        ["id", "tenant_id", "channel"],
        schema=SCHEMA,
    )
    _create_provider_channel_links()

    op.get_bind().execute(
        sa.text(
            f"""
            INSERT INTO {SCHEMA}."{PROVIDER_CHANNEL_LINKS}" (
                id,
                tenant_id,
                provider,
                provider_account_id,
                channel_id,
                linked_at
            )
            SELECT
                replace(gen_random_uuid()::text, '-', ''),
                tenant_id,
                provider,
                id,
                channel_id,
                transaction_timestamp()
            FROM {SCHEMA}."{PROVIDER_ACCOUNTS}"
            """
        )
    )

    op.drop_constraint(
        OLD_ACCOUNT_CHANNEL_FK,
        PROVIDER_ACCOUNTS,
        schema=SCHEMA,
        type_="foreignkey",
    )
    op.drop_constraint(
        OLD_ACCOUNT_CHANNEL_UNIQUE,
        PROVIDER_ACCOUNTS,
        schema=SCHEMA,
        type_="unique",
    )
    op.drop_column(PROVIDER_ACCOUNTS, "channel_id", schema=SCHEMA)

    _assert_final_shape(sa.inspect(op.get_bind()))


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    state = _schema_state(inspector)
    if state == "final":
        _assert_final_shape(inspector)
        return

    _assert_old_shape(inspector)
    _upgrade_old_shape()


def _populated_identity_tables() -> dict[str, int]:
    populated: dict[str, int] = {}
    for table_name in (PROVIDER_ACCOUNTS, PROVIDER_CHANNEL_LINKS):
        count = op.get_bind().execute(sa.text(f'SELECT count(*) FROM {SCHEMA}."{table_name}"')).scalar_one()
        if count:
            populated[table_name] = int(count)
    return populated


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    _assert_final_shape(inspector)

    _lock_transition_tables(include_link=True)
    _assert_final_shape(sa.inspect(op.get_bind()))

    populated = _populated_identity_tables()
    if populated:
        summary = ", ".join(f"{table_name}={populated[table_name]}" for table_name in sorted(populated))
        raise RuntimeError(f"Cannot downgrade provider account/channel decoupling while identity ownership exists ({summary})")

    op.add_column(
        PROVIDER_ACCOUNTS,
        sa.Column("channel_id", sa.String(length=32), nullable=False),
        schema=SCHEMA,
    )
    op.create_unique_constraint(
        OLD_ACCOUNT_CHANNEL_UNIQUE,
        PROVIDER_ACCOUNTS,
        ["channel_id"],
        schema=SCHEMA,
    )
    op.create_foreign_key(
        OLD_ACCOUNT_CHANNEL_FK,
        PROVIDER_ACCOUNTS,
        CHAT_CHANNELS,
        ["channel_id", "tenant_id"],
        ["id", "tenant_id"],
        source_schema=SCHEMA,
        referent_schema=SCHEMA,
        ondelete="RESTRICT",
    )

    op.drop_table(PROVIDER_CHANNEL_LINKS, schema=SCHEMA)
    op.drop_constraint(
        ACCOUNT_LINK_SCOPE_UNIQUE,
        PROVIDER_ACCOUNTS,
        schema=SCHEMA,
        type_="unique",
    )
    op.drop_constraint(
        CHAT_IDENTITY_SCOPE_UNIQUE,
        CHAT_CHANNELS,
        schema=SCHEMA,
        type_="unique",
    )

    inspector = sa.inspect(op.get_bind())
    _assert_old_shape(inspector)
