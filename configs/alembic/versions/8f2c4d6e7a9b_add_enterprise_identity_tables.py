"""add canonical enterprise identity tables

Revision ID: 8f2c4d6e7a9b
Revises: 7c8d9e0f1a2b
Create Date: 2026-08-12 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine.reflection import Inspector

revision: str = "8f2c4d6e7a9b"
down_revision: str | None = "7c8d9e0f1a2b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "usr_ai"
EXTERNAL_IDENTITIES = "t_ai_external_identities"
IDENTITY_PROVIDER_TENANTS = "t_ai_identity_provider_tenants"
IDENTITY_PROVIDER_ACCOUNTS = "t_ai_identity_provider_accounts"
EXTERNAL_IDENTITY_ALIASES = "t_ai_external_identity_aliases"
ENTERPRISE_SUBJECT_LINKS = "t_ai_enterprise_subject_links"
IDENTITY_EVENT_RECEIPTS = "t_ai_identity_event_receipts"
CHAT_CHANNELS = "t_ai_chat_channels"
CHAT_TENANT_SCOPE_UNIQUE = "uq_chat_channels_tenant_scope"

_TABLES = (
    IDENTITY_PROVIDER_TENANTS,
    IDENTITY_PROVIDER_ACCOUNTS,
    EXTERNAL_IDENTITIES,
    EXTERNAL_IDENTITY_ALIASES,
    ENTERPRISE_SUBJECT_LINKS,
    IDENTITY_EVENT_RECEIPTS,
)

_BASE_COLUMN_SHAPES: dict[str, tuple[str, int | None, bool]] = {
    "create_date": ("datetime", None, True),
    "update_date": ("datetime", None, True),
    "create_time": ("bigint", None, True),
    "update_time": ("bigint", None, True),
}

_SERVER_DEFAULTS: dict[str, dict[str, str]] = {
    IDENTITY_PROVIDER_TENANTS: {},
    IDENTITY_PROVIDER_ACCOUNTS: {
        "identity_revision": "1",
        "identity_health_state": "'pending'::character varying",
    },
    EXTERNAL_IDENTITIES: {
        "state": "'pending_link'::character varying",
        "identity_revision": "1",
        "attributes": "'{}'::jsonb",
    },
    EXTERNAL_IDENTITY_ALIASES: {},
    ENTERPRISE_SUBJECT_LINKS: {
        "state": "'inactive'::character varying",
    },
    IDENTITY_EVENT_RECEIPTS: {
        "processing_state": "'processing'::character varying",
    },
}

# PostgreSQL reflects CHECK expressions into a canonical form.  Comparing the
# expression as well as its name prevents a model-first database from replacing
# a security constraint with a same-named ``CHECK (true)`` and then being
# silently stamped as compatible.
_CHECK_SQL: dict[str, dict[str, str]] = {
    IDENTITY_PROVIDER_TENANTS: {
        "ck_identity_provider_tenants_nonempty": ("btrim(provider::text) <> ''::text AND btrim(provider_tenant_key::text) <> ''::text"),
    },
    IDENTITY_PROVIDER_ACCOUNTS: {
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
    },
    EXTERNAL_IDENTITIES: {
        "ck_external_identities_active_verified": ("state::text <> 'active'::text OR verified_at IS NOT NULL"),
        "ck_external_identities_attributes_keys": ("(attributes - ARRAY['display_name'::text, 'provider_status'::text]) = '{}'::jsonb"),
        "ck_external_identities_attributes_object": ("jsonb_typeof(attributes) = 'object'::text"),
        "ck_external_identities_attributes_values": (
            "NOT attributes ? 'display_name'::text OR "
            "(attributes -> 'display_name'::text) = 'null'::jsonb OR "
            "jsonb_typeof(attributes -> 'display_name'::text) = 'string'::text) "
            "AND (NOT attributes ? 'provider_status'::text OR "
            "(attributes -> 'provider_status'::text) = 'null'::jsonb OR "
            "jsonb_typeof(attributes -> 'provider_status'::text) = 'string'::text"
        ),
        "ck_external_identities_nonempty": (
            "btrim(provider::text) <> ''::text AND btrim(provider_tenant_key::text) <> ''::text AND btrim(subject_type::text) <> ''::text AND btrim(subject_value::text) <> ''::text"
        ),
        "ck_external_identities_revision": "identity_revision >= 1",
        "ck_external_identities_state": (
            "state::text = ANY (ARRAY['pending_link'::character varying, "
            "'active'::character varying, 'inactive'::character varying, "
            "'revoked'::character varying, 'conflict'::character varying]::text[])"
        ),
    },
    EXTERNAL_IDENTITY_ALIASES: {
        "ck_external_identity_aliases_nonempty": (
            "btrim(provider::text) <> ''::text AND btrim(provider_tenant_key::text) <> ''::text AND btrim(provider_account_key::text) <> ''::text AND btrim(alias_value::text) <> ''::text"
        ),
        "ck_external_identity_aliases_type": ("alias_type::text = ANY (ARRAY['open_id'::character varying, 'union_id'::character varying]::text[])"),
    },
    ENTERPRISE_SUBJECT_LINKS: {
        "ck_enterprise_subject_links_active_verified": ("state::text <> 'active'::text OR verified_at IS NOT NULL"),
        "ck_enterprise_subject_links_nonempty": ("btrim(subject_value::text) <> ''::text AND btrim(issuer::text) <> ''::text AND btrim(issuer_tenant::text) <> ''::text"),
        "ck_enterprise_subject_links_state": ("state::text = ANY (ARRAY['active'::character varying, 'inactive'::character varying, 'conflict'::character varying]::text[])"),
        "ck_enterprise_subject_links_type": ("subject_type::text = ANY (ARRAY['employee_no'::character varying, 'talent_id'::character varying, 'workcode'::character varying]::text[])"),
    },
    IDENTITY_EVENT_RECEIPTS: {
        "ck_identity_event_receipts_hash": ("event_hash::text ~ '^[0-9a-f]{64}$'::text"),
        "ck_identity_event_receipts_nonempty": (
            "btrim(provider::text) <> ''::text AND "
            "btrim(provider_tenant_key::text) <> ''::text AND "
            "btrim(provider_account_key::text) <> ''::text AND "
            "btrim(event_type::text) <> ''::text AND "
            "btrim(event_id::text) <> ''::text"
        ),
        "ck_identity_event_receipts_processed_at": (
            "processing_state::text = 'processing'::text AND "
            "processed_at IS NULL AND error_code IS NULL OR "
            "processing_state::text = 'succeeded'::text AND "
            "processed_at IS NOT NULL AND error_code IS NULL OR "
            "processing_state::text = 'failed'::text AND "
            "processed_at IS NOT NULL AND error_code IS NOT NULL"
        ),
        "ck_identity_event_receipts_state": ("processing_state::text = ANY (ARRAY['processing'::character varying, 'succeeded'::character varying, 'failed'::character varying]::text[])"),
    },
}


def _base_columns() -> list[sa.Column]:
    return [
        sa.Column("create_date", sa.DateTime(), nullable=True),
        sa.Column("update_date", sa.DateTime(), nullable=True),
        sa.Column("create_time", sa.BigInteger(), nullable=True),
        sa.Column("update_time", sa.BigInteger(), nullable=True),
    ]


def _create_audit_indexes(table_name: str) -> None:
    for column_name in ("create_date", "update_date", "create_time", "update_time"):
        op.create_index(
            f"ix_usr_ai_{table_name}_{column_name}",
            table_name,
            [column_name],
            schema=SCHEMA,
        )


def _incompatible(table_name: str, label: str, actual: object, expected: object) -> RuntimeError:
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


def _assert_existing_table_compatible(
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

    actual_unique = {str(constraint["name"]): tuple(constraint["column_names"]) for constraint in inspector.get_unique_constraints(table_name, schema=SCHEMA)}
    if actual_unique != unique_constraints:
        raise _incompatible(
            table_name,
            "unique constraints",
            actual_unique,
            unique_constraints,
        )

    actual_checks = {str(constraint["name"]): _normalize_sql(constraint.get("sqltext", "")) for constraint in inspector.get_check_constraints(table_name, schema=SCHEMA)}
    if actual_checks != check_constraints:
        raise _incompatible(
            table_name,
            "check constraints",
            actual_checks,
            check_constraints,
        )

    actual_indexes = {str(index["name"]): tuple(index["column_names"]) for index in inspector.get_indexes(table_name, schema=SCHEMA) if not index.get("duplicates_constraint")}
    if actual_indexes != indexes:
        raise _incompatible(table_name, "indexes", actual_indexes, indexes)

    actual_foreign_keys: dict[
        str,
        tuple[tuple[str, ...], str, tuple[str, ...], str],
    ] = {}
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
        actual_foreign_keys[name] = (
            tuple(foreign_key["constrained_columns"]),
            str(foreign_key["referred_table"]),
            tuple(foreign_key["referred_columns"]),
            str((foreign_key.get("options") or {}).get("ondelete", "")).upper(),
        )
    if actual_foreign_keys != foreign_keys:
        raise _incompatible(
            table_name,
            "foreign keys",
            actual_foreign_keys,
            foreign_keys,
        )


def _ensure_chat_channel_tenant_scope() -> None:
    """Add the parent key required to bind an installation to one tenant."""

    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(CHAT_CHANNELS, schema=SCHEMA):
        raise RuntimeError(f"Required table {SCHEMA}.{CHAT_CHANNELS} does not exist")
    unique_constraints = {str(constraint["name"]): tuple(constraint["column_names"]) for constraint in inspector.get_unique_constraints(CHAT_CHANNELS, schema=SCHEMA)}
    actual = unique_constraints.get(CHAT_TENANT_SCOPE_UNIQUE)
    if actual == ("id", "tenant_id"):
        return
    if actual is not None:
        raise _incompatible(
            CHAT_CHANNELS,
            f"unique constraint {CHAT_TENANT_SCOPE_UNIQUE}",
            actual,
            ("id", "tenant_id"),
        )
    op.create_unique_constraint(
        CHAT_TENANT_SCOPE_UNIQUE,
        CHAT_CHANNELS,
        ["id", "tenant_id"],
        schema=SCHEMA,
    )


def _external_identity_shape() -> None:
    _assert_existing_table_compatible(
        sa.inspect(op.get_bind()),
        EXTERNAL_IDENTITIES,
        columns={
            **_BASE_COLUMN_SHAPES,
            "id": ("string", 32, False),
            "tenant_id": ("string", 32, False),
            "user_id": ("string", 32, False),
            "provider": ("string", 64, False),
            "provider_tenant_key": ("string", 255, False),
            "subject_type": ("string", 64, False),
            "subject_value": ("string", 255, False),
            "state": ("string", 16, False),
            "verified_at": ("timestamptz", None, True),
            "last_seen_at": ("timestamptz", None, True),
            "identity_revision": ("bigint", None, False),
            "attributes": ("jsonb", None, False),
        },
        server_defaults=_SERVER_DEFAULTS[EXTERNAL_IDENTITIES],
        unique_constraints={
            "uq_external_identities_alias_parent": (
                "id",
                "tenant_id",
                "provider",
                "provider_tenant_key",
            ),
            "uq_external_identities_tenant_provider_subject": (
                "tenant_id",
                "provider",
                "provider_tenant_key",
                "subject_type",
                "subject_value",
            ),
        },
        check_constraints=_CHECK_SQL[EXTERNAL_IDENTITIES],
        indexes={
            "ix_external_identities_tenant_state_verified": (
                "tenant_id",
                "state",
                "verified_at",
            ),
            "ix_external_identities_tenant_user_state": (
                "tenant_id",
                "user_id",
                "state",
            ),
            **{f"ix_usr_ai_{EXTERNAL_IDENTITIES}_{column}": (column,) for column in _BASE_COLUMN_SHAPES},
        },
        foreign_keys={
            "fk_external_identities_provider_tenant": (
                ("tenant_id", "provider", "provider_tenant_key"),
                IDENTITY_PROVIDER_TENANTS,
                ("tenant_id", "provider", "provider_tenant_key"),
                "RESTRICT",
            ),
            "fk_external_identities_tenant_id": (
                ("tenant_id",),
                "t_ai_tenants",
                ("id",),
                "RESTRICT",
            ),
            "fk_external_identities_user_id": (
                ("user_id",),
                "t_ai_users",
                ("id",),
                "RESTRICT",
            ),
        },
    )


def _provider_tenant_shape() -> None:
    _assert_existing_table_compatible(
        sa.inspect(op.get_bind()),
        IDENTITY_PROVIDER_TENANTS,
        columns={
            **_BASE_COLUMN_SHAPES,
            "id": ("string", 32, False),
            "tenant_id": ("string", 32, False),
            "provider": ("string", 64, False),
            "provider_tenant_key": ("string", 255, False),
            "verified_at": ("timestamptz", None, False),
        },
        server_defaults=_SERVER_DEFAULTS[IDENTITY_PROVIDER_TENANTS],
        unique_constraints={
            "uq_identity_provider_tenants_provider_tenant": (
                "provider",
                "provider_tenant_key",
            ),
            "uq_identity_provider_tenants_scope": (
                "tenant_id",
                "provider",
                "provider_tenant_key",
            ),
        },
        check_constraints=_CHECK_SQL[IDENTITY_PROVIDER_TENANTS],
        indexes={
            "ix_identity_provider_tenants_tenant_provider": (
                "tenant_id",
                "provider",
            ),
            **{f"ix_usr_ai_{IDENTITY_PROVIDER_TENANTS}_{column}": (column,) for column in _BASE_COLUMN_SHAPES},
        },
        foreign_keys={
            "fk_identity_provider_tenants_tenant_id": (
                ("tenant_id",),
                "t_ai_tenants",
                ("id",),
                "RESTRICT",
            ),
        },
    )


def _provider_account_shape() -> None:
    _assert_existing_table_compatible(
        sa.inspect(op.get_bind()),
        IDENTITY_PROVIDER_ACCOUNTS,
        columns={
            **_BASE_COLUMN_SHAPES,
            "id": ("string", 32, False),
            "tenant_id": ("string", 32, False),
            "channel_id": ("string", 32, False),
            "provider": ("string", 64, False),
            "provider_tenant_key": ("string", 255, False),
            "provider_account_key": ("string", 255, False),
            "identity_revision": ("bigint", None, False),
            "last_scope_change_at": ("timestamptz", None, True),
            "last_directory_event_at": ("timestamptz", None, True),
            "identity_health_state": ("string", 16, False),
            "identity_health_error_code": ("string", 64, True),
        },
        server_defaults=_SERVER_DEFAULTS[IDENTITY_PROVIDER_ACCOUNTS],
        unique_constraints={
            "uq_identity_provider_accounts_channel": ("channel_id",),
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
        },
        check_constraints=_CHECK_SQL[IDENTITY_PROVIDER_ACCOUNTS],
        indexes={
            "ix_identity_provider_accounts_tenant_health": (
                "tenant_id",
                "identity_health_state",
            ),
            **{f"ix_usr_ai_{IDENTITY_PROVIDER_ACCOUNTS}_{column}": (column,) for column in _BASE_COLUMN_SHAPES},
        },
        foreign_keys={
            "fk_identity_provider_accounts_provider_tenant": (
                ("tenant_id", "provider", "provider_tenant_key"),
                IDENTITY_PROVIDER_TENANTS,
                ("tenant_id", "provider", "provider_tenant_key"),
                "RESTRICT",
            ),
            "fk_identity_provider_accounts_channel_tenant": (
                ("channel_id", "tenant_id"),
                CHAT_CHANNELS,
                ("id", "tenant_id"),
                "RESTRICT",
            ),
            "fk_identity_provider_accounts_tenant_id": (
                ("tenant_id",),
                "t_ai_tenants",
                ("id",),
                "RESTRICT",
            ),
        },
    )


def _alias_shape() -> None:
    _assert_existing_table_compatible(
        sa.inspect(op.get_bind()),
        EXTERNAL_IDENTITY_ALIASES,
        columns={
            **_BASE_COLUMN_SHAPES,
            "id": ("string", 32, False),
            "tenant_id": ("string", 32, False),
            "external_identity_id": ("string", 32, False),
            "provider": ("string", 64, False),
            "provider_tenant_key": ("string", 255, False),
            "provider_account_key": ("string", 255, False),
            "alias_type": ("string", 32, False),
            "alias_value": ("string", 255, False),
            "verified_at": ("timestamptz", None, False),
        },
        server_defaults=_SERVER_DEFAULTS[EXTERNAL_IDENTITY_ALIASES],
        unique_constraints={
            "uq_external_identity_aliases_tenant_provider_alias": (
                "tenant_id",
                "provider",
                "provider_tenant_key",
                "provider_account_key",
                "alias_type",
                "alias_value",
            ),
        },
        check_constraints=_CHECK_SQL[EXTERNAL_IDENTITY_ALIASES],
        indexes={
            "ix_external_identity_aliases_tenant_identity": (
                "tenant_id",
                "external_identity_id",
            ),
            **{f"ix_usr_ai_{EXTERNAL_IDENTITY_ALIASES}_{column}": (column,) for column in _BASE_COLUMN_SHAPES},
        },
        foreign_keys={
            "fk_external_identity_aliases_parent": (
                (
                    "external_identity_id",
                    "tenant_id",
                    "provider",
                    "provider_tenant_key",
                ),
                EXTERNAL_IDENTITIES,
                ("id", "tenant_id", "provider", "provider_tenant_key"),
                "RESTRICT",
            ),
            "fk_external_identity_aliases_provider_account": (
                (
                    "tenant_id",
                    "provider",
                    "provider_tenant_key",
                    "provider_account_key",
                ),
                IDENTITY_PROVIDER_ACCOUNTS,
                (
                    "tenant_id",
                    "provider",
                    "provider_tenant_key",
                    "provider_account_key",
                ),
                "RESTRICT",
            ),
            "fk_external_identity_aliases_tenant_id": (
                ("tenant_id",),
                "t_ai_tenants",
                ("id",),
                "RESTRICT",
            ),
        },
    )


def _enterprise_subject_shape() -> None:
    _assert_existing_table_compatible(
        sa.inspect(op.get_bind()),
        ENTERPRISE_SUBJECT_LINKS,
        columns={
            **_BASE_COLUMN_SHAPES,
            "id": ("string", 32, False),
            "tenant_id": ("string", 32, False),
            "user_id": ("string", 32, False),
            "subject_type": ("string", 64, False),
            "subject_value": ("string", 255, False),
            "issuer": ("string", 128, False),
            "issuer_tenant": ("string", 255, False),
            "state": ("string", 16, False),
            "verified_at": ("timestamptz", None, True),
            "source_revision": ("string", 255, True),
        },
        server_defaults=_SERVER_DEFAULTS[ENTERPRISE_SUBJECT_LINKS],
        unique_constraints={
            "uq_enterprise_subject_links_resolver_slot": (
                "tenant_id",
                "user_id",
                "subject_type",
                "issuer",
                "issuer_tenant",
            ),
            "uq_enterprise_subject_links_tenant_subject": (
                "tenant_id",
                "subject_type",
                "subject_value",
            ),
        },
        check_constraints=_CHECK_SQL[ENTERPRISE_SUBJECT_LINKS],
        indexes={
            "ix_enterprise_subject_links_tenant_user_type_state": (
                "tenant_id",
                "user_id",
                "subject_type",
                "state",
            ),
            **{f"ix_usr_ai_{ENTERPRISE_SUBJECT_LINKS}_{column}": (column,) for column in _BASE_COLUMN_SHAPES},
        },
        foreign_keys={
            "fk_enterprise_subject_links_tenant_id": (
                ("tenant_id",),
                "t_ai_tenants",
                ("id",),
                "RESTRICT",
            ),
            "fk_enterprise_subject_links_user_id": (
                ("user_id",),
                "t_ai_users",
                ("id",),
                "RESTRICT",
            ),
        },
    )


def _event_receipt_shape() -> None:
    _assert_existing_table_compatible(
        sa.inspect(op.get_bind()),
        IDENTITY_EVENT_RECEIPTS,
        columns={
            **_BASE_COLUMN_SHAPES,
            "id": ("string", 32, False),
            "tenant_id": ("string", 32, False),
            "provider": ("string", 64, False),
            "provider_tenant_key": ("string", 255, False),
            "provider_account_key": ("string", 255, False),
            "event_type": ("string", 128, False),
            "event_id": ("string", 255, False),
            "event_hash": ("string", 64, False),
            "processing_state": ("string", 16, False),
            "event_at": ("timestamptz", None, True),
            "processed_at": ("timestamptz", None, True),
            "error_code": ("string", 64, True),
            "external_identity_id": ("string", 32, True),
        },
        server_defaults=_SERVER_DEFAULTS[IDENTITY_EVENT_RECEIPTS],
        unique_constraints={
            "uq_identity_event_receipts_provider_event": (
                "tenant_id",
                "provider",
                "provider_tenant_key",
                "provider_account_key",
                "event_type",
                "event_id",
            ),
        },
        check_constraints=_CHECK_SQL[IDENTITY_EVENT_RECEIPTS],
        indexes={
            "ix_identity_event_receipts_identity_id": (
                "tenant_id",
                "external_identity_id",
            ),
            "ix_identity_event_receipts_tenant_state_created": (
                "tenant_id",
                "processing_state",
                "create_date",
            ),
            **{f"ix_usr_ai_{IDENTITY_EVENT_RECEIPTS}_{column}": (column,) for column in _BASE_COLUMN_SHAPES},
        },
        foreign_keys={
            "fk_identity_event_receipts_identity_scope": (
                (
                    "external_identity_id",
                    "tenant_id",
                    "provider",
                    "provider_tenant_key",
                ),
                EXTERNAL_IDENTITIES,
                ("id", "tenant_id", "provider", "provider_tenant_key"),
                "RESTRICT",
            ),
            "fk_identity_event_receipts_provider_account": (
                (
                    "tenant_id",
                    "provider",
                    "provider_tenant_key",
                    "provider_account_key",
                ),
                IDENTITY_PROVIDER_ACCOUNTS,
                (
                    "tenant_id",
                    "provider",
                    "provider_tenant_key",
                    "provider_account_key",
                ),
                "RESTRICT",
            ),
            "fk_identity_event_receipts_tenant_id": (
                ("tenant_id",),
                "t_ai_tenants",
                ("id",),
                "RESTRICT",
            ),
        },
    )


def _create_provider_tenants() -> None:
    op.create_table(
        IDENTITY_PROVIDER_TENANTS,
        *_base_columns(),
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("tenant_id", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("provider_tenant_key", sa.String(length=255), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "btrim(provider) <> '' AND btrim(provider_tenant_key) <> ''",
            name="ck_identity_provider_tenants_nonempty",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            [f"{SCHEMA}.t_ai_tenants.id"],
            name="fk_identity_provider_tenants_tenant_id",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_t_ai_identity_provider_tenants"),
        sa.UniqueConstraint(
            "provider",
            "provider_tenant_key",
            name="uq_identity_provider_tenants_provider_tenant",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "provider",
            "provider_tenant_key",
            name="uq_identity_provider_tenants_scope",
        ),
        schema=SCHEMA,
    )
    _create_audit_indexes(IDENTITY_PROVIDER_TENANTS)
    op.create_index(
        "ix_identity_provider_tenants_tenant_provider",
        IDENTITY_PROVIDER_TENANTS,
        ["tenant_id", "provider"],
        schema=SCHEMA,
    )


def _create_provider_accounts() -> None:
    op.create_table(
        IDENTITY_PROVIDER_ACCOUNTS,
        *_base_columns(),
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("tenant_id", sa.String(length=32), nullable=False),
        sa.Column("channel_id", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("provider_tenant_key", sa.String(length=255), nullable=False),
        sa.Column("provider_account_key", sa.String(length=255), nullable=False),
        sa.Column(
            "identity_revision",
            sa.BigInteger(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.Column("last_scope_change_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_directory_event_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "identity_health_state",
            sa.String(length=16),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column("identity_health_error_code", sa.String(length=64), nullable=True),
        sa.CheckConstraint(
            "btrim(provider) <> '' AND btrim(provider_tenant_key) <> '' AND btrim(provider_account_key) <> ''",
            name="ck_identity_provider_accounts_nonempty",
        ),
        sa.CheckConstraint(
            "identity_revision >= 1",
            name="ck_identity_provider_accounts_revision",
        ),
        sa.CheckConstraint(
            "identity_health_state IN ('pending', 'healthy', 'degraded', 'error', 'disabled')",
            name="ck_identity_provider_accounts_health_state",
        ),
        sa.CheckConstraint(
            "(identity_health_state = 'error' AND identity_health_error_code IS NOT NULL) OR (identity_health_state <> 'error' AND identity_health_error_code IS NULL)",
            name="ck_identity_provider_accounts_health_error",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            [f"{SCHEMA}.t_ai_tenants.id"],
            name="fk_identity_provider_accounts_tenant_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "provider", "provider_tenant_key"],
            [
                f"{SCHEMA}.{IDENTITY_PROVIDER_TENANTS}.tenant_id",
                f"{SCHEMA}.{IDENTITY_PROVIDER_TENANTS}.provider",
                f"{SCHEMA}.{IDENTITY_PROVIDER_TENANTS}.provider_tenant_key",
            ],
            name="fk_identity_provider_accounts_provider_tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["channel_id", "tenant_id"],
            [
                f"{SCHEMA}.{CHAT_CHANNELS}.id",
                f"{SCHEMA}.{CHAT_CHANNELS}.tenant_id",
            ],
            name="fk_identity_provider_accounts_channel_tenant",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_t_ai_identity_provider_accounts"),
        sa.UniqueConstraint(
            "provider",
            "provider_tenant_key",
            "provider_account_key",
            name="uq_identity_provider_accounts_provider_account",
        ),
        sa.UniqueConstraint(
            "channel_id",
            name="uq_identity_provider_accounts_channel",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "provider",
            "provider_tenant_key",
            "provider_account_key",
            name="uq_identity_provider_accounts_scope",
        ),
        schema=SCHEMA,
    )
    _create_audit_indexes(IDENTITY_PROVIDER_ACCOUNTS)
    op.create_index(
        "ix_identity_provider_accounts_tenant_health",
        IDENTITY_PROVIDER_ACCOUNTS,
        ["tenant_id", "identity_health_state"],
        schema=SCHEMA,
    )


def _create_external_identities() -> None:
    op.create_table(
        EXTERNAL_IDENTITIES,
        *_base_columns(),
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("tenant_id", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("provider_tenant_key", sa.String(length=255), nullable=False),
        sa.Column("subject_type", sa.String(length=64), nullable=False),
        sa.Column("subject_value", sa.String(length=255), nullable=False),
        sa.Column(
            "state",
            sa.String(length=16),
            server_default=sa.text("'pending_link'"),
            nullable=False,
        ),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "identity_revision",
            sa.BigInteger(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.Column(
            "attributes",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "state IN ('pending_link', 'active', 'inactive', 'revoked', 'conflict')",
            name="ck_external_identities_state",
        ),
        sa.CheckConstraint(
            "identity_revision >= 1",
            name="ck_external_identities_revision",
        ),
        sa.CheckConstraint(
            "btrim(provider) <> '' AND btrim(provider_tenant_key) <> '' AND btrim(subject_type) <> '' AND btrim(subject_value) <> ''",
            name="ck_external_identities_nonempty",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(attributes) = 'object'",
            name="ck_external_identities_attributes_object",
        ),
        sa.CheckConstraint(
            "attributes - ARRAY['display_name', 'provider_status']::text[] = '{}'::jsonb",
            name="ck_external_identities_attributes_keys",
        ),
        sa.CheckConstraint(
            "(NOT attributes ? 'display_name' OR attributes->'display_name' = 'null'::jsonb OR jsonb_typeof(attributes->'display_name') = 'string') "
            "AND (NOT attributes ? 'provider_status' OR attributes->'provider_status' = 'null'::jsonb OR jsonb_typeof(attributes->'provider_status') = 'string')",
            name="ck_external_identities_attributes_values",
        ),
        sa.CheckConstraint(
            "state <> 'active' OR verified_at IS NOT NULL",
            name="ck_external_identities_active_verified",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            [f"{SCHEMA}.t_ai_tenants.id"],
            name="fk_external_identities_tenant_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            [f"{SCHEMA}.t_ai_users.id"],
            name="fk_external_identities_user_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "provider", "provider_tenant_key"],
            [
                f"{SCHEMA}.{IDENTITY_PROVIDER_TENANTS}.tenant_id",
                f"{SCHEMA}.{IDENTITY_PROVIDER_TENANTS}.provider",
                f"{SCHEMA}.{IDENTITY_PROVIDER_TENANTS}.provider_tenant_key",
            ],
            name="fk_external_identities_provider_tenant",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_t_ai_external_identities"),
        sa.UniqueConstraint(
            "id",
            "tenant_id",
            "provider",
            "provider_tenant_key",
            name="uq_external_identities_alias_parent",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "provider",
            "provider_tenant_key",
            "subject_type",
            "subject_value",
            name="uq_external_identities_tenant_provider_subject",
        ),
        schema=SCHEMA,
    )
    _create_audit_indexes(EXTERNAL_IDENTITIES)
    op.create_index(
        "ix_external_identities_tenant_user_state",
        EXTERNAL_IDENTITIES,
        ["tenant_id", "user_id", "state"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_external_identities_tenant_state_verified",
        EXTERNAL_IDENTITIES,
        ["tenant_id", "state", "verified_at"],
        schema=SCHEMA,
    )


def _create_aliases() -> None:
    op.create_table(
        EXTERNAL_IDENTITY_ALIASES,
        *_base_columns(),
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("tenant_id", sa.String(length=32), nullable=False),
        sa.Column("external_identity_id", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("provider_tenant_key", sa.String(length=255), nullable=False),
        sa.Column("provider_account_key", sa.String(length=255), nullable=False),
        sa.Column("alias_type", sa.String(length=32), nullable=False),
        sa.Column("alias_value", sa.String(length=255), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "alias_type IN ('open_id', 'union_id')",
            name="ck_external_identity_aliases_type",
        ),
        sa.CheckConstraint(
            "btrim(provider) <> '' AND btrim(provider_tenant_key) <> '' AND btrim(provider_account_key) <> '' AND btrim(alias_value) <> ''",
            name="ck_external_identity_aliases_nonempty",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            [f"{SCHEMA}.t_ai_tenants.id"],
            name="fk_external_identity_aliases_tenant_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            [
                "external_identity_id",
                "tenant_id",
                "provider",
                "provider_tenant_key",
            ],
            [
                f"{SCHEMA}.{EXTERNAL_IDENTITIES}.id",
                f"{SCHEMA}.{EXTERNAL_IDENTITIES}.tenant_id",
                f"{SCHEMA}.{EXTERNAL_IDENTITIES}.provider",
                f"{SCHEMA}.{EXTERNAL_IDENTITIES}.provider_tenant_key",
            ],
            name="fk_external_identity_aliases_parent",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            [
                "tenant_id",
                "provider",
                "provider_tenant_key",
                "provider_account_key",
            ],
            [
                f"{SCHEMA}.{IDENTITY_PROVIDER_ACCOUNTS}.tenant_id",
                f"{SCHEMA}.{IDENTITY_PROVIDER_ACCOUNTS}.provider",
                f"{SCHEMA}.{IDENTITY_PROVIDER_ACCOUNTS}.provider_tenant_key",
                f"{SCHEMA}.{IDENTITY_PROVIDER_ACCOUNTS}.provider_account_key",
            ],
            name="fk_external_identity_aliases_provider_account",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_t_ai_external_identity_aliases"),
        sa.UniqueConstraint(
            "tenant_id",
            "provider",
            "provider_tenant_key",
            "provider_account_key",
            "alias_type",
            "alias_value",
            name="uq_external_identity_aliases_tenant_provider_alias",
        ),
        schema=SCHEMA,
    )
    _create_audit_indexes(EXTERNAL_IDENTITY_ALIASES)
    op.create_index(
        "ix_external_identity_aliases_tenant_identity",
        EXTERNAL_IDENTITY_ALIASES,
        ["tenant_id", "external_identity_id"],
        schema=SCHEMA,
    )


def _create_enterprise_subject_links() -> None:
    op.create_table(
        ENTERPRISE_SUBJECT_LINKS,
        *_base_columns(),
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("tenant_id", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.String(length=32), nullable=False),
        sa.Column("subject_type", sa.String(length=64), nullable=False),
        sa.Column("subject_value", sa.String(length=255), nullable=False),
        sa.Column("issuer", sa.String(length=128), nullable=False),
        sa.Column("issuer_tenant", sa.String(length=255), nullable=False),
        sa.Column(
            "state",
            sa.String(length=16),
            server_default=sa.text("'inactive'"),
            nullable=False,
        ),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_revision", sa.String(length=255), nullable=True),
        sa.CheckConstraint(
            "subject_type IN ('employee_no', 'talent_id', 'workcode')",
            name="ck_enterprise_subject_links_type",
        ),
        sa.CheckConstraint(
            "state IN ('active', 'inactive', 'conflict')",
            name="ck_enterprise_subject_links_state",
        ),
        sa.CheckConstraint(
            "state <> 'active' OR verified_at IS NOT NULL",
            name="ck_enterprise_subject_links_active_verified",
        ),
        sa.CheckConstraint(
            "btrim(subject_value) <> '' AND btrim(issuer) <> '' AND btrim(issuer_tenant) <> ''",
            name="ck_enterprise_subject_links_nonempty",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            [f"{SCHEMA}.t_ai_tenants.id"],
            name="fk_enterprise_subject_links_tenant_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            [f"{SCHEMA}.t_ai_users.id"],
            name="fk_enterprise_subject_links_user_id",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_t_ai_enterprise_subject_links"),
        sa.UniqueConstraint(
            "tenant_id",
            "subject_type",
            "subject_value",
            name="uq_enterprise_subject_links_tenant_subject",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "user_id",
            "subject_type",
            "issuer",
            "issuer_tenant",
            name="uq_enterprise_subject_links_resolver_slot",
        ),
        schema=SCHEMA,
    )
    _create_audit_indexes(ENTERPRISE_SUBJECT_LINKS)
    op.create_index(
        "ix_enterprise_subject_links_tenant_user_type_state",
        ENTERPRISE_SUBJECT_LINKS,
        ["tenant_id", "user_id", "subject_type", "state"],
        schema=SCHEMA,
    )


def _create_identity_event_receipts() -> None:
    op.create_table(
        IDENTITY_EVENT_RECEIPTS,
        *_base_columns(),
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("tenant_id", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("provider_tenant_key", sa.String(length=255), nullable=False),
        sa.Column("provider_account_key", sa.String(length=255), nullable=False),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("event_id", sa.String(length=255), nullable=False),
        sa.Column("event_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "processing_state",
            sa.String(length=16),
            server_default=sa.text("'processing'"),
            nullable=False,
        ),
        sa.Column("event_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("external_identity_id", sa.String(length=32), nullable=True),
        sa.CheckConstraint(
            "processing_state IN ('processing', 'succeeded', 'failed')",
            name="ck_identity_event_receipts_state",
        ),
        sa.CheckConstraint(
            "event_hash ~ '^[0-9a-f]{64}$'",
            name="ck_identity_event_receipts_hash",
        ),
        sa.CheckConstraint(
            "(processing_state = 'processing' AND processed_at IS NULL AND error_code IS NULL) "
            "OR (processing_state = 'succeeded' AND processed_at IS NOT NULL AND error_code IS NULL) "
            "OR (processing_state = 'failed' AND processed_at IS NOT NULL AND error_code IS NOT NULL)",
            name="ck_identity_event_receipts_processed_at",
        ),
        sa.CheckConstraint(
            "btrim(provider) <> '' AND btrim(provider_tenant_key) <> '' AND btrim(provider_account_key) <> '' AND btrim(event_type) <> '' AND btrim(event_id) <> ''",
            name="ck_identity_event_receipts_nonempty",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            [f"{SCHEMA}.t_ai_tenants.id"],
            name="fk_identity_event_receipts_tenant_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            [
                "external_identity_id",
                "tenant_id",
                "provider",
                "provider_tenant_key",
            ],
            [
                f"{SCHEMA}.{EXTERNAL_IDENTITIES}.id",
                f"{SCHEMA}.{EXTERNAL_IDENTITIES}.tenant_id",
                f"{SCHEMA}.{EXTERNAL_IDENTITIES}.provider",
                f"{SCHEMA}.{EXTERNAL_IDENTITIES}.provider_tenant_key",
            ],
            name="fk_identity_event_receipts_identity_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            [
                "tenant_id",
                "provider",
                "provider_tenant_key",
                "provider_account_key",
            ],
            [
                f"{SCHEMA}.{IDENTITY_PROVIDER_ACCOUNTS}.tenant_id",
                f"{SCHEMA}.{IDENTITY_PROVIDER_ACCOUNTS}.provider",
                f"{SCHEMA}.{IDENTITY_PROVIDER_ACCOUNTS}.provider_tenant_key",
                f"{SCHEMA}.{IDENTITY_PROVIDER_ACCOUNTS}.provider_account_key",
            ],
            name="fk_identity_event_receipts_provider_account",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_t_ai_identity_event_receipts"),
        sa.UniqueConstraint(
            "tenant_id",
            "provider",
            "provider_tenant_key",
            "provider_account_key",
            "event_type",
            "event_id",
            name="uq_identity_event_receipts_provider_event",
        ),
        schema=SCHEMA,
    )
    _create_audit_indexes(IDENTITY_EVENT_RECEIPTS)
    op.create_index(
        "ix_identity_event_receipts_identity_id",
        IDENTITY_EVENT_RECEIPTS,
        ["tenant_id", "external_identity_id"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_identity_event_receipts_tenant_state_created",
        IDENTITY_EVENT_RECEIPTS,
        ["tenant_id", "processing_state", "create_date"],
        schema=SCHEMA,
    )


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    existing_tables = {table_name for table_name in _TABLES if inspector.has_table(table_name, schema=SCHEMA)}
    if existing_tables and existing_tables != set(_TABLES):
        missing_tables = set(_TABLES) - existing_tables
        raise RuntimeError(f"Existing enterprise identity schema is incomplete: present={sorted(existing_tables)!r}, missing={sorted(missing_tables)!r}")

    _ensure_chat_channel_tenant_scope()
    inspector = sa.inspect(op.get_bind())
    creators = (
        (
            IDENTITY_PROVIDER_TENANTS,
            _create_provider_tenants,
            _provider_tenant_shape,
        ),
        (
            IDENTITY_PROVIDER_ACCOUNTS,
            _create_provider_accounts,
            _provider_account_shape,
        ),
        (EXTERNAL_IDENTITIES, _create_external_identities, _external_identity_shape),
        (EXTERNAL_IDENTITY_ALIASES, _create_aliases, _alias_shape),
        (
            ENTERPRISE_SUBJECT_LINKS,
            _create_enterprise_subject_links,
            _enterprise_subject_shape,
        ),
        (
            IDENTITY_EVENT_RECEIPTS,
            _create_identity_event_receipts,
            _event_receipt_shape,
        ),
    )
    for table_name, create_table, assert_shape in creators:
        if inspector.has_table(table_name, schema=SCHEMA):
            assert_shape()
        else:
            create_table()
        inspector = sa.inspect(op.get_bind())


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    existing_tables = [table_name for table_name in _TABLES if inspector.has_table(table_name, schema=SCHEMA)]
    if existing_tables:
        # The empty-history preflight and the destructive drops must be one
        # serializable critical section.  ACCESS EXCLUSIVE blocks a concurrent
        # INSERT from committing after its table was counted but before DROP.
        qualified_tables = ", ".join(f'{SCHEMA}."{table_name}"' for table_name in existing_tables)
        op.get_bind().execute(sa.text(f"LOCK TABLE {qualified_tables} IN ACCESS EXCLUSIVE MODE"))

    populated: dict[str, int] = {}
    for table_name in existing_tables:
        count = op.get_bind().execute(sa.text(f'SELECT count(*) FROM {SCHEMA}."{table_name}"')).scalar_one()
        if count:
            populated[table_name] = count
    if populated:
        raise RuntimeError(f"Cannot downgrade enterprise identity tables while identity history exists: {populated}")

    for table_name in reversed(_TABLES):
        if inspector.has_table(table_name, schema=SCHEMA):
            op.drop_table(table_name, schema=SCHEMA)
            inspector = sa.inspect(op.get_bind())
    chat_unique = {str(constraint["name"]) for constraint in inspector.get_unique_constraints(CHAT_CHANNELS, schema=SCHEMA)}
    if CHAT_TENANT_SCOPE_UNIQUE in chat_unique:
        op.drop_constraint(
            CHAT_TENANT_SCOPE_UNIQUE,
            CHAT_CHANNELS,
            schema=SCHEMA,
            type_="unique",
        )
