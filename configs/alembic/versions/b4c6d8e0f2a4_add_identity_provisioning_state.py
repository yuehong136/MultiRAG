"""add transaction-safe identity provisioning state

Revision ID: b4c6d8e0f2a4
Revises: 9a3b5c7d8e0f
Create Date: 2026-08-13 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine.reflection import Inspector

revision: str = "b4c6d8e0f2a4"
down_revision: str | None = "9a3b5c7d8e0f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "usr_ai"
TENANTS = "t_ai_tenants"
USERS = "t_ai_users"
USER_TENANTS = "t_ai_user_tenants"
PROVIDER_ACCOUNTS = "t_ai_identity_provider_accounts"
EXTERNAL_IDENTITIES = "t_ai_external_identities"
TENANT_POLICIES = "t_ai_identity_tenant_policies"
LINK_CODES = "t_ai_identity_link_codes"
BINDING_EVENTS = "t_ai_identity_binding_events"

ACTIVE_MEMBERSHIP_UNIQUE = "uq_user_tenants_active_tenant_user"
EXTERNAL_IDENTITY_REVERSE_UNIQUE = "uq_external_identities_tenant_user_provider_subject_type"

_BASE_COLUMNS: dict[str, tuple[str, int | None, bool]] = {
    "create_date": ("datetime", None, True),
    "update_date": ("datetime", None, True),
    "create_time": ("bigint", None, True),
    "update_time": ("bigint", None, True),
}

_POLICY_COLUMNS = {
    **_BASE_COLUMNS,
    "id": ("string", 32, False),
    "tenant_id": ("string", 32, False),
    "mode": ("string", 16, False),
    "revision": ("bigint", None, False),
    "link_code_ttl_seconds": ("integer", None, False),
    "changed_at": ("timestamptz", None, False),
}

_LINK_CODE_COLUMNS = {
    **_BASE_COLUMNS,
    "id": ("string", 32, False),
    "tenant_id": ("string", 32, False),
    "provider": ("string", 64, False),
    "provider_tenant_key": ("string", 255, False),
    "provider_account_key": ("string", 255, False),
    "target_user_id": ("string", 32, False),
    "digest_key_id": ("string", 64, False),
    "code_digest": ("string", 64, False),
    "policy_revision": ("bigint", None, False),
    "provider_account_revision": ("bigint", None, False),
    "provider_account_last_scope_change_at": ("timestamptz", None, True),
    "state": ("string", 16, False),
    "issued_at": ("timestamptz", None, False),
    "expires_at": ("timestamptz", None, False),
    "consumed_at": ("timestamptz", None, True),
    "revoked_at": ("timestamptz", None, True),
    "consumed_external_identity_id": ("string", 32, True),
}

_BINDING_EVENT_COLUMNS = {
    **_BASE_COLUMNS,
    "id": ("string", 32, False),
    "tenant_id": ("string", 32, False),
    "provider": ("string", 64, False),
    "provider_tenant_key": ("string", 255, False),
    "provider_account_key": ("string", 255, False),
    "external_identity_id": ("string", 32, False),
    "target_user_id": ("string", 32, False),
    "actor_user_id": ("string", 32, True),
    "link_code_id": ("string", 32, True),
    "binding_method": ("string", 32, False),
    "previous_account_kind": ("string", 16, True),
    "result_account_kind": ("string", 16, False),
    "policy_revision": ("bigint", None, False),
    "provider_verified_at": ("timestamptz", None, False),
    "occurred_at": ("timestamptz", None, False),
    "request_digest_key_id": ("string", 64, False),
    "request_digest": ("string", 64, False),
}

_POLICY_CHECKS = {
    "ck_identity_tenant_policies_identity",
    "ck_identity_tenant_policies_link_code_ttl",
    "ck_identity_tenant_policies_mode",
    "ck_identity_tenant_policies_revision",
}

_LINK_CODE_CHECKS = {
    "ck_identity_link_codes_account_revision",
    "ck_identity_link_codes_hash",
    "ck_identity_link_codes_lifetime",
    "ck_identity_link_codes_nonempty",
    "ck_identity_link_codes_revision",
    "ck_identity_link_codes_state",
    "ck_identity_link_codes_state_fields",
}

_BINDING_EVENT_CHECKS = {
    "ck_identity_binding_events_account_kind",
    "ck_identity_binding_events_hash",
    "ck_identity_binding_events_method",
    "ck_identity_binding_events_method_shape",
    "ck_identity_binding_events_nonempty",
    "ck_identity_binding_events_revision",
    "ck_identity_binding_events_verified_time",
}

_CHECK_SQL: dict[str, str] = {
    "ck_identity_tenant_policies_identity": ("((id)::text = (tenant_id)::text)"),
    "ck_identity_tenant_policies_link_code_ttl": ("((link_code_ttl_seconds >= 60) AND (link_code_ttl_seconds <= 900))"),
    "ck_identity_tenant_policies_mode": ("((mode)::text = ANY ((ARRAY['preprovisioned'::character varying, 'link_only'::character varying, 'jit'::character varying])::text[]))"),
    "ck_identity_tenant_policies_revision": "(revision >= 1)",
    "ck_identity_link_codes_account_revision": ("(provider_account_revision >= 1)"),
    "ck_identity_link_codes_hash": ("((code_digest)::text ~ '^[0-9a-f]{64}$'::text)"),
    "ck_identity_link_codes_lifetime": ("((expires_at > issued_at) AND (expires_at <= (issued_at + '00:15:00'::interval)))"),
    "ck_identity_link_codes_nonempty": (
        "((btrim((provider)::text) <> ''::text) AND "
        "(btrim((provider_tenant_key)::text) <> ''::text) AND "
        "(btrim((provider_account_key)::text) <> ''::text) AND "
        "(btrim((target_user_id)::text) <> ''::text) AND "
        "(btrim((digest_key_id)::text) <> ''::text))"
    ),
    "ck_identity_link_codes_revision": "(policy_revision >= 1)",
    "ck_identity_link_codes_state": ("((state)::text = ANY ((ARRAY['pending'::character varying, 'consumed'::character varying, 'revoked'::character varying])::text[]))"),
    "ck_identity_link_codes_state_fields": (
        "((((state)::text = 'pending'::text) AND (consumed_at IS NULL) AND "
        "(revoked_at IS NULL) AND (consumed_external_identity_id IS NULL)) OR "
        "(((state)::text = 'consumed'::text) AND (consumed_at IS NOT NULL) AND "
        "(revoked_at IS NULL) AND (consumed_external_identity_id IS NOT NULL) "
        "AND (consumed_at >= issued_at) AND (consumed_at < expires_at)) OR "
        "(((state)::text = 'revoked'::text) AND (consumed_at IS NULL) AND "
        "(revoked_at IS NOT NULL) AND (consumed_external_identity_id IS NULL) "
        "AND (revoked_at >= issued_at)))"
    ),
    "ck_identity_binding_events_account_kind": (
        "(((previous_account_kind IS NULL) OR "
        "((previous_account_kind)::text = ANY ((ARRAY['local'::character varying, "
        "'external'::character varying, 'hybrid'::character varying])::text[]))) "
        "AND ((result_account_kind)::text = ANY ((ARRAY['external'::character varying, "
        "'hybrid'::character varying])::text[])))"
    ),
    "ck_identity_binding_events_hash": ("((request_digest)::text ~ '^[0-9a-f]{64}$'::text)"),
    "ck_identity_binding_events_method": ("((binding_method)::text = ANY ((ARRAY['jit'::character varying, 'preprovisioned'::character varying, 'link_code'::character varying])::text[]))"),
    "ck_identity_binding_events_method_shape": (
        "((((binding_method)::text = 'jit'::text) AND (actor_user_id IS NULL) "
        "AND (link_code_id IS NULL) AND (previous_account_kind IS NULL) AND "
        "((result_account_kind)::text = 'external'::text)) OR "
        "(((binding_method)::text = 'preprovisioned'::text) AND "
        "(actor_user_id IS NULL) AND (link_code_id IS NULL) AND "
        "(previous_account_kind IS NOT NULL) AND "
        "((((previous_account_kind)::text = 'local'::text) AND "
        "((result_account_kind)::text = 'hybrid'::text)) OR "
        "(((previous_account_kind)::text = 'external'::text) AND "
        "((result_account_kind)::text = 'external'::text)) OR "
        "(((previous_account_kind)::text = 'hybrid'::text) AND "
        "((result_account_kind)::text = 'hybrid'::text)))) OR "
        "(((binding_method)::text = 'link_code'::text) AND "
        "((actor_user_id)::text = (target_user_id)::text) AND "
        "(link_code_id IS NOT NULL) AND (previous_account_kind IS NOT NULL) AND "
        "((((previous_account_kind)::text = 'local'::text) AND "
        "((result_account_kind)::text = 'hybrid'::text)) OR "
        "(((previous_account_kind)::text = 'external'::text) AND "
        "((result_account_kind)::text = 'external'::text)) OR "
        "(((previous_account_kind)::text = 'hybrid'::text) AND "
        "((result_account_kind)::text = 'hybrid'::text)))))"
    ),
    "ck_identity_binding_events_nonempty": (
        "((btrim((provider)::text) <> ''::text) AND "
        "(btrim((provider_tenant_key)::text) <> ''::text) AND "
        "(btrim((provider_account_key)::text) <> ''::text) AND "
        "(btrim((external_identity_id)::text) <> ''::text) AND "
        "(btrim((target_user_id)::text) <> ''::text) AND "
        "(btrim((request_digest_key_id)::text) <> ''::text))"
    ),
    "ck_identity_binding_events_revision": "(policy_revision >= 1)",
    "ck_identity_binding_events_verified_time": ("(provider_verified_at <= occurred_at)"),
}


def _audit_columns() -> list[sa.Column]:
    return [
        sa.Column("create_date", sa.DateTime(), nullable=True),
        sa.Column("update_date", sa.DateTime(), nullable=True),
        sa.Column("create_time", sa.BigInteger(), nullable=True),
        sa.Column("update_time", sa.BigInteger(), nullable=True),
    ]


def _create_audit_indexes(table_name: str) -> None:
    for column_name in _BASE_COLUMNS:
        op.create_index(
            f"ix_usr_ai_{table_name}_{column_name}",
            table_name,
            [column_name],
            schema=SCHEMA,
        )


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
    if isinstance(column_type, sa.Integer):
        return ("integer", None, nullable)
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
    actual: dict[
        str,
        tuple[tuple[str, ...], str, tuple[str, ...], str],
    ] = {}
    for foreign_key in inspector.get_foreign_keys(table_name, schema=SCHEMA):
        name = str(foreign_key["name"])
        if foreign_key.get("referred_schema") != SCHEMA:
            raise _incompatible(
                table_name,
                f"foreign key {name} schema",
                foreign_key.get("referred_schema"),
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
        {"schema_name": SCHEMA, "table_name": table_name},
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


def _assert_check_constraints(
    table_name: str,
    expected_names: set[str],
) -> None:
    rows = op.get_bind().execute(
        sa.text(
            """
            SELECT
                constraint_record.conname,
                pg_catalog.pg_get_expr(
                    constraint_record.conbin,
                    constraint_record.conrelid
                ) AS expression
            FROM pg_catalog.pg_constraint AS constraint_record
            JOIN pg_catalog.pg_class AS table_record
              ON table_record.oid = constraint_record.conrelid
            JOIN pg_catalog.pg_namespace AS namespace_record
              ON namespace_record.oid = table_record.relnamespace
            WHERE namespace_record.nspname = :schema_name
              AND table_record.relname = :table_name
              AND constraint_record.contype = 'c'
            """
        ),
        {"schema_name": SCHEMA, "table_name": table_name},
    )
    actual = {str(row.conname): _normalize_sql(row.expression) for row in rows}
    expected = {name: _normalize_sql(_CHECK_SQL[name]) for name in expected_names}
    if actual != expected:
        raise _incompatible(
            table_name,
            "check constraints",
            actual,
            expected,
        )


def _plain_indexes(
    inspector: Inspector,
    table_name: str,
) -> dict[str, tuple[tuple[str, ...], bool]]:
    return {
        str(index["name"]): (
            tuple(index["column_names"]),
            bool(index.get("unique")),
        )
        for index in inspector.get_indexes(table_name, schema=SCHEMA)
        if not index.get("duplicates_constraint") and str(index["name"]) != ACTIVE_MEMBERSHIP_UNIQUE
    }


def _assert_table(
    inspector: Inspector,
    table_name: str,
    *,
    columns: dict[str, tuple[str, int | None, bool]],
    defaults: dict[str, str],
    unique_constraints: dict[str, tuple[str, ...]],
    check_names: set[str],
    indexes: dict[str, tuple[tuple[str, ...], bool]],
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
    if actual_defaults != defaults:
        raise _incompatible(table_name, "server defaults", actual_defaults, defaults)

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

    _assert_check_constraints(table_name, check_names)

    actual_indexes = _plain_indexes(inspector, table_name)
    if actual_indexes != indexes:
        raise _incompatible(table_name, "indexes", actual_indexes, indexes)

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
        set(unique_constraints) | check_names | set(foreign_keys),
    )


def _audit_indexes(table_name: str) -> dict[str, tuple[tuple[str, ...], bool]]:
    return {f"ix_usr_ai_{table_name}_{column}": ((column,), False) for column in _BASE_COLUMNS}


def _assert_policy_table(inspector: Inspector) -> None:
    _assert_table(
        inspector,
        TENANT_POLICIES,
        columns=_POLICY_COLUMNS,
        defaults={"revision": "1"},
        unique_constraints={
            "uq_identity_tenant_policies_tenant": ("tenant_id",),
        },
        check_names=_POLICY_CHECKS,
        indexes=_audit_indexes(TENANT_POLICIES),
        foreign_keys={
            "fk_identity_tenant_policies_tenant_id": (
                ("tenant_id",),
                TENANTS,
                ("id",),
                "RESTRICT",
            ),
        },
    )


def _assert_link_code_table(inspector: Inspector) -> None:
    indexes = {
        **_audit_indexes(LINK_CODES),
        "ix_identity_link_codes_account_state_expiry": (
            (
                "tenant_id",
                "provider",
                "provider_tenant_key",
                "provider_account_key",
                "state",
                "expires_at",
            ),
            False,
        ),
        "ix_identity_link_codes_target_state_expiry": (
            ("target_user_id", "state", "expires_at"),
            False,
        ),
        "uq_identity_link_codes_pending_target_account": (
            (
                "tenant_id",
                "provider",
                "provider_tenant_key",
                "provider_account_key",
                "target_user_id",
            ),
            True,
        ),
    }
    _assert_table(
        inspector,
        LINK_CODES,
        columns=_LINK_CODE_COLUMNS,
        defaults={"state": "'pending'::character varying"},
        unique_constraints={
            "uq_identity_link_codes_digest": (
                "digest_key_id",
                "code_digest",
            ),
            "uq_identity_link_codes_event_scope": (
                "id",
                "tenant_id",
                "provider",
                "provider_tenant_key",
                "provider_account_key",
            ),
        },
        check_names=_LINK_CODE_CHECKS,
        indexes=indexes,
        foreign_keys={
            "fk_identity_link_codes_consumed_identity": (
                (
                    "consumed_external_identity_id",
                    "tenant_id",
                    "provider",
                    "provider_tenant_key",
                ),
                EXTERNAL_IDENTITIES,
                ("id", "tenant_id", "provider", "provider_tenant_key"),
                "RESTRICT",
            ),
            "fk_identity_link_codes_provider_account": (
                (
                    "tenant_id",
                    "provider",
                    "provider_tenant_key",
                    "provider_account_key",
                ),
                PROVIDER_ACCOUNTS,
                (
                    "tenant_id",
                    "provider",
                    "provider_tenant_key",
                    "provider_account_key",
                ),
                "RESTRICT",
            ),
            "fk_identity_link_codes_target_user_id": (
                ("target_user_id",),
                USERS,
                ("id",),
                "RESTRICT",
            ),
            "fk_identity_link_codes_tenant_id": (
                ("tenant_id",),
                TENANTS,
                ("id",),
                "RESTRICT",
            ),
        },
    )
    _assert_partial_index(
        inspector,
        LINK_CODES,
        "uq_identity_link_codes_pending_target_account",
        (
            "tenant_id",
            "provider",
            "provider_tenant_key",
            "provider_account_key",
            "target_user_id",
        ),
        "((state)::text = 'pending'::text)",
    )


def _assert_binding_event_table(inspector: Inspector) -> None:
    _assert_table(
        inspector,
        BINDING_EVENTS,
        columns=_BINDING_EVENT_COLUMNS,
        defaults={},
        unique_constraints={
            "uq_identity_binding_events_identity": ("external_identity_id",),
            "uq_identity_binding_events_link_code": ("link_code_id",),
            "uq_identity_binding_events_request": (
                "request_digest_key_id",
                "request_digest",
            ),
        },
        check_names=_BINDING_EVENT_CHECKS,
        indexes={
            **_audit_indexes(BINDING_EVENTS),
            "ix_identity_binding_events_account_time": (
                (
                    "tenant_id",
                    "provider",
                    "provider_tenant_key",
                    "provider_account_key",
                    "occurred_at",
                ),
                False,
            ),
            "ix_identity_binding_events_tenant_target_time": (
                ("tenant_id", "target_user_id", "occurred_at"),
                False,
            ),
        },
        foreign_keys={
            "fk_identity_binding_events_actor_user_id": (
                ("actor_user_id",),
                USERS,
                ("id",),
                "RESTRICT",
            ),
            "fk_identity_binding_events_identity_scope": (
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
            "fk_identity_binding_events_link_code_scope": (
                (
                    "link_code_id",
                    "tenant_id",
                    "provider",
                    "provider_tenant_key",
                    "provider_account_key",
                ),
                LINK_CODES,
                (
                    "id",
                    "tenant_id",
                    "provider",
                    "provider_tenant_key",
                    "provider_account_key",
                ),
                "RESTRICT",
            ),
            "fk_identity_binding_events_provider_account": (
                (
                    "tenant_id",
                    "provider",
                    "provider_tenant_key",
                    "provider_account_key",
                ),
                PROVIDER_ACCOUNTS,
                (
                    "tenant_id",
                    "provider",
                    "provider_tenant_key",
                    "provider_account_key",
                ),
                "RESTRICT",
            ),
            "fk_identity_binding_events_target_user_id": (
                ("target_user_id",),
                USERS,
                ("id",),
                "RESTRICT",
            ),
            "fk_identity_binding_events_tenant_id": (
                ("tenant_id",),
                TENANTS,
                ("id",),
                "RESTRICT",
            ),
        },
    )


def _index_state(
    table_name: str,
    index_name: str,
) -> tuple[str, bool, bool, bool] | None:
    row = (
        op.get_bind()
        .execute(
            sa.text(
                """
            SELECT
                pg_catalog.pg_get_expr(
                    index_record.indpred,
                    index_record.indrelid
                ) AS predicate,
                index_record.indisvalid,
                index_record.indisready,
                index_record.indisunique
            FROM pg_catalog.pg_index AS index_record
            JOIN pg_catalog.pg_class AS table_record
              ON table_record.oid = index_record.indrelid
            JOIN pg_catalog.pg_class AS index_class
              ON index_class.oid = index_record.indexrelid
            JOIN pg_catalog.pg_namespace AS namespace_record
              ON namespace_record.oid = table_record.relnamespace
            WHERE namespace_record.nspname = :schema_name
              AND table_record.relname = :table_name
              AND index_class.relname = :index_name
            """
            ),
            {
                "schema_name": SCHEMA,
                "table_name": table_name,
                "index_name": index_name,
            },
        )
        .one_or_none()
    )
    if row is None:
        return None
    return (
        _normalize_sql(row.predicate),
        bool(row.indisvalid),
        bool(row.indisready),
        bool(row.indisunique),
    )


def _assert_partial_index(
    inspector: Inspector,
    table_name: str,
    index_name: str,
    columns: tuple[str, ...],
    predicate: str,
) -> None:
    indexes = {str(index["name"]): index for index in inspector.get_indexes(table_name, schema=SCHEMA) if not index.get("duplicates_constraint")}
    index = indexes.get(index_name)
    if index is None:
        raise _incompatible(table_name, f"index {index_name}", None, columns)
    state = _index_state(table_name, index_name)
    expected_state = (_normalize_sql(predicate), True, True, True)
    if tuple(index["column_names"]) != columns or not bool(index.get("unique")) or state != expected_state:
        raise _incompatible(
            table_name,
            f"partial unique index {index_name}",
            {
                "columns": tuple(index["column_names"]),
                "unique": bool(index.get("unique")),
                "state": state,
            },
            {
                "columns": columns,
                "unique": True,
                "state": expected_state,
            },
        )


def _assert_cross_table_invariants(inspector: Inspector) -> None:
    external_unique = _unique_constraints(inspector, EXTERNAL_IDENTITIES)
    if external_unique.get(EXTERNAL_IDENTITY_REVERSE_UNIQUE) != (
        "tenant_id",
        "user_id",
        "provider",
        "provider_tenant_key",
        "subject_type",
    ):
        raise _incompatible(
            EXTERNAL_IDENTITIES,
            EXTERNAL_IDENTITY_REVERSE_UNIQUE,
            external_unique.get(EXTERNAL_IDENTITY_REVERSE_UNIQUE),
            (
                "tenant_id",
                "user_id",
                "provider",
                "provider_tenant_key",
                "subject_type",
            ),
        )
    _assert_constraints_enforced(
        EXTERNAL_IDENTITIES,
        {EXTERNAL_IDENTITY_REVERSE_UNIQUE},
    )
    _assert_partial_index(
        inspector,
        USER_TENANTS,
        ACTIVE_MEMBERSHIP_UNIQUE,
        ("tenant_id", "user_id"),
        "((status)::text = '1'::text)",
    )


def _schema_state(inspector: Inspector) -> str:
    tables = {table_name: inspector.has_table(table_name, schema=SCHEMA) for table_name in (TENANT_POLICIES, LINK_CODES, BINDING_EVENTS)}
    external_unique = _unique_constraints(inspector, EXTERNAL_IDENTITIES)
    membership_indexes = {str(index["name"]) for index in inspector.get_indexes(USER_TENANTS, schema=SCHEMA) if not index.get("duplicates_constraint")}
    markers = {
        **tables,
        EXTERNAL_IDENTITY_REVERSE_UNIQUE: (EXTERNAL_IDENTITY_REVERSE_UNIQUE in external_unique),
        ACTIVE_MEMBERSHIP_UNIQUE: ACTIVE_MEMBERSHIP_UNIQUE in membership_indexes,
    }
    if all(markers.values()):
        return "final"
    if not any(markers.values()):
        return "old"
    raise RuntimeError(f"Existing identity provisioning schema is incomplete: transition markers={markers!r}")


def _assert_final_shape(inspector: Inspector) -> None:
    if _schema_state(inspector) != "final":
        raise AssertionError("unreachable identity provisioning schema state")
    _assert_policy_table(inspector)
    _assert_link_code_table(inspector)
    _assert_binding_event_table(inspector)
    _assert_cross_table_invariants(inspector)


def _present_new_tables(inspector: Inspector) -> set[str]:
    return {table_name for table_name in (TENANT_POLICIES, LINK_CODES, BINDING_EVENTS) if inspector.has_table(table_name, schema=SCHEMA)}


def _lock_transition_tables(present_new_tables: set[str]) -> None:
    # Follow the runtime write order.  The one statement makes PostgreSQL take
    # these locks in the listed order and avoids migration/runtime inversions.
    table_names = []
    if TENANT_POLICIES in present_new_tables:
        table_names.append(TENANT_POLICIES)
    table_names.extend(
        [
            PROVIDER_ACCOUNTS,
            USER_TENANTS,
            EXTERNAL_IDENTITIES,
        ]
    )
    if LINK_CODES in present_new_tables:
        table_names.append(LINK_CODES)
    if BINDING_EVENTS in present_new_tables:
        table_names.append(BINDING_EVENTS)
    qualified = ", ".join(f'{SCHEMA}."{table_name}"' for table_name in table_names)
    op.get_bind().execute(sa.text(f"LOCK TABLE {qualified} IN ACCESS EXCLUSIVE MODE"))


def _duplicate_groups() -> dict[str, int]:
    membership_groups = (
        op.get_bind()
        .execute(
            sa.text(
                f"""
            SELECT count(*)
            FROM (
                SELECT tenant_id, user_id
                FROM {SCHEMA}."{USER_TENANTS}"
                WHERE status = '1'
                GROUP BY tenant_id, user_id
                HAVING count(*) > 1
            ) AS duplicate_groups
            """
            )
        )
        .scalar_one()
    )
    identity_groups = (
        op.get_bind()
        .execute(
            sa.text(
                f"""
            SELECT count(*)
            FROM (
                SELECT tenant_id, user_id, provider, provider_tenant_key, subject_type
                FROM {SCHEMA}."{EXTERNAL_IDENTITIES}"
                GROUP BY tenant_id, user_id, provider, provider_tenant_key, subject_type
                HAVING count(*) > 1
            ) AS duplicate_groups
            """
            )
        )
        .scalar_one()
    )
    failures = {
        "active_membership_duplicate_groups": int(membership_groups),
        "external_identity_reverse_duplicate_groups": int(identity_groups),
    }
    return {name: count for name, count in failures.items() if count}


def _create_policy_table() -> None:
    op.create_table(
        TENANT_POLICIES,
        *_audit_columns(),
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("tenant_id", sa.String(length=32), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column(
            "revision",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column("link_code_ttl_seconds", sa.Integer(), nullable=False),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "id = tenant_id",
            name="ck_identity_tenant_policies_identity",
        ),
        sa.CheckConstraint(
            "mode IN ('preprovisioned', 'link_only', 'jit')",
            name="ck_identity_tenant_policies_mode",
        ),
        sa.CheckConstraint(
            "revision >= 1",
            name="ck_identity_tenant_policies_revision",
        ),
        sa.CheckConstraint(
            "link_code_ttl_seconds BETWEEN 60 AND 900",
            name="ck_identity_tenant_policies_link_code_ttl",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            [f"{SCHEMA}.{TENANTS}.id"],
            name="fk_identity_tenant_policies_tenant_id",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=f"pk_{TENANT_POLICIES}"),
        sa.UniqueConstraint(
            "tenant_id",
            name="uq_identity_tenant_policies_tenant",
        ),
        schema=SCHEMA,
    )
    _create_audit_indexes(TENANT_POLICIES)


def _create_link_code_table() -> None:
    op.create_table(
        LINK_CODES,
        *_audit_columns(),
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("tenant_id", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("provider_tenant_key", sa.String(length=255), nullable=False),
        sa.Column("provider_account_key", sa.String(length=255), nullable=False),
        sa.Column("target_user_id", sa.String(length=32), nullable=False),
        sa.Column("digest_key_id", sa.String(length=64), nullable=False),
        sa.Column("code_digest", sa.String(length=64), nullable=False),
        sa.Column("policy_revision", sa.BigInteger(), nullable=False),
        sa.Column("provider_account_revision", sa.BigInteger(), nullable=False),
        sa.Column(
            "provider_account_last_scope_change_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "state",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "consumed_external_identity_id",
            sa.String(length=32),
            nullable=True,
        ),
        sa.CheckConstraint(
            "btrim(provider) <> '' AND btrim(provider_tenant_key) <> '' AND btrim(provider_account_key) <> '' AND btrim(target_user_id) <> '' AND btrim(digest_key_id) <> ''",
            name="ck_identity_link_codes_nonempty",
        ),
        sa.CheckConstraint(
            "code_digest ~ '^[0-9a-f]{64}$'",
            name="ck_identity_link_codes_hash",
        ),
        sa.CheckConstraint(
            "policy_revision >= 1",
            name="ck_identity_link_codes_revision",
        ),
        sa.CheckConstraint(
            "provider_account_revision >= 1",
            name="ck_identity_link_codes_account_revision",
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'consumed', 'revoked')",
            name="ck_identity_link_codes_state",
        ),
        sa.CheckConstraint(
            "expires_at > issued_at AND expires_at <= issued_at + interval '15 minutes'",
            name="ck_identity_link_codes_lifetime",
        ),
        sa.CheckConstraint(
            "(state = 'pending' AND consumed_at IS NULL AND revoked_at IS NULL "
            "AND consumed_external_identity_id IS NULL) "
            "OR (state = 'consumed' AND consumed_at IS NOT NULL AND revoked_at IS NULL "
            "AND consumed_external_identity_id IS NOT NULL AND consumed_at >= issued_at "
            "AND consumed_at < expires_at) "
            "OR (state = 'revoked' AND consumed_at IS NULL AND revoked_at IS NOT NULL "
            "AND consumed_external_identity_id IS NULL AND revoked_at >= issued_at)",
            name="ck_identity_link_codes_state_fields",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            [f"{SCHEMA}.{TENANTS}.id"],
            name="fk_identity_link_codes_tenant_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["target_user_id"],
            [f"{SCHEMA}.{USERS}.id"],
            name="fk_identity_link_codes_target_user_id",
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
                f"{SCHEMA}.{PROVIDER_ACCOUNTS}.tenant_id",
                f"{SCHEMA}.{PROVIDER_ACCOUNTS}.provider",
                f"{SCHEMA}.{PROVIDER_ACCOUNTS}.provider_tenant_key",
                f"{SCHEMA}.{PROVIDER_ACCOUNTS}.provider_account_key",
            ],
            name="fk_identity_link_codes_provider_account",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            [
                "consumed_external_identity_id",
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
            name="fk_identity_link_codes_consumed_identity",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=f"pk_{LINK_CODES}"),
        sa.UniqueConstraint(
            "digest_key_id",
            "code_digest",
            name="uq_identity_link_codes_digest",
        ),
        sa.UniqueConstraint(
            "id",
            "tenant_id",
            "provider",
            "provider_tenant_key",
            "provider_account_key",
            name="uq_identity_link_codes_event_scope",
        ),
        schema=SCHEMA,
    )
    _create_audit_indexes(LINK_CODES)
    op.create_index(
        "ix_identity_link_codes_target_state_expiry",
        LINK_CODES,
        ["target_user_id", "state", "expires_at"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_identity_link_codes_account_state_expiry",
        LINK_CODES,
        [
            "tenant_id",
            "provider",
            "provider_tenant_key",
            "provider_account_key",
            "state",
            "expires_at",
        ],
        schema=SCHEMA,
    )
    op.create_index(
        "uq_identity_link_codes_pending_target_account",
        LINK_CODES,
        [
            "tenant_id",
            "provider",
            "provider_tenant_key",
            "provider_account_key",
            "target_user_id",
        ],
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text("state = 'pending'"),
    )


def _create_binding_event_table() -> None:
    op.create_table(
        BINDING_EVENTS,
        *_audit_columns(),
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("tenant_id", sa.String(length=32), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("provider_tenant_key", sa.String(length=255), nullable=False),
        sa.Column("provider_account_key", sa.String(length=255), nullable=False),
        sa.Column("external_identity_id", sa.String(length=32), nullable=False),
        sa.Column("target_user_id", sa.String(length=32), nullable=False),
        sa.Column("actor_user_id", sa.String(length=32), nullable=True),
        sa.Column("link_code_id", sa.String(length=32), nullable=True),
        sa.Column("binding_method", sa.String(length=32), nullable=False),
        sa.Column("previous_account_kind", sa.String(length=16), nullable=True),
        sa.Column("result_account_kind", sa.String(length=16), nullable=False),
        sa.Column("policy_revision", sa.BigInteger(), nullable=False),
        sa.Column("provider_verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("request_digest_key_id", sa.String(length=64), nullable=False),
        sa.Column("request_digest", sa.String(length=64), nullable=False),
        sa.CheckConstraint(
            "btrim(provider) <> '' AND btrim(provider_tenant_key) <> '' "
            "AND btrim(provider_account_key) <> '' AND btrim(external_identity_id) <> '' "
            "AND btrim(target_user_id) <> '' AND btrim(request_digest_key_id) <> ''",
            name="ck_identity_binding_events_nonempty",
        ),
        sa.CheckConstraint(
            "request_digest ~ '^[0-9a-f]{64}$'",
            name="ck_identity_binding_events_hash",
        ),
        sa.CheckConstraint(
            "policy_revision >= 1",
            name="ck_identity_binding_events_revision",
        ),
        sa.CheckConstraint(
            "binding_method IN ('jit', 'preprovisioned', 'link_code')",
            name="ck_identity_binding_events_method",
        ),
        sa.CheckConstraint(
            "(previous_account_kind IS NULL OR previous_account_kind IN ('local', 'external', 'hybrid')) AND result_account_kind IN ('external', 'hybrid')",
            name="ck_identity_binding_events_account_kind",
        ),
        sa.CheckConstraint(
            "(binding_method = 'jit' AND actor_user_id IS NULL AND link_code_id IS NULL "
            "AND previous_account_kind IS NULL AND result_account_kind = 'external') "
            "OR (binding_method = 'preprovisioned' AND actor_user_id IS NULL "
            "AND link_code_id IS NULL AND previous_account_kind IS NOT NULL "
            "AND ((previous_account_kind = 'local' AND result_account_kind = 'hybrid') "
            "OR (previous_account_kind = 'external' AND result_account_kind = 'external') "
            "OR (previous_account_kind = 'hybrid' AND result_account_kind = 'hybrid'))) "
            "OR (binding_method = 'link_code' AND actor_user_id = target_user_id "
            "AND link_code_id IS NOT NULL AND previous_account_kind IS NOT NULL "
            "AND ((previous_account_kind = 'local' AND result_account_kind = 'hybrid') "
            "OR (previous_account_kind = 'external' AND result_account_kind = 'external') "
            "OR (previous_account_kind = 'hybrid' AND result_account_kind = 'hybrid')))",
            name="ck_identity_binding_events_method_shape",
        ),
        sa.CheckConstraint(
            "provider_verified_at <= occurred_at",
            name="ck_identity_binding_events_verified_time",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            [f"{SCHEMA}.{TENANTS}.id"],
            name="fk_identity_binding_events_tenant_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["target_user_id"],
            [f"{SCHEMA}.{USERS}.id"],
            name="fk_identity_binding_events_target_user_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            [f"{SCHEMA}.{USERS}.id"],
            name="fk_identity_binding_events_actor_user_id",
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
                f"{SCHEMA}.{PROVIDER_ACCOUNTS}.tenant_id",
                f"{SCHEMA}.{PROVIDER_ACCOUNTS}.provider",
                f"{SCHEMA}.{PROVIDER_ACCOUNTS}.provider_tenant_key",
                f"{SCHEMA}.{PROVIDER_ACCOUNTS}.provider_account_key",
            ],
            name="fk_identity_binding_events_provider_account",
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
            name="fk_identity_binding_events_identity_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            [
                "link_code_id",
                "tenant_id",
                "provider",
                "provider_tenant_key",
                "provider_account_key",
            ],
            [
                f"{SCHEMA}.{LINK_CODES}.id",
                f"{SCHEMA}.{LINK_CODES}.tenant_id",
                f"{SCHEMA}.{LINK_CODES}.provider",
                f"{SCHEMA}.{LINK_CODES}.provider_tenant_key",
                f"{SCHEMA}.{LINK_CODES}.provider_account_key",
            ],
            name="fk_identity_binding_events_link_code_scope",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=f"pk_{BINDING_EVENTS}"),
        sa.UniqueConstraint(
            "external_identity_id",
            name="uq_identity_binding_events_identity",
        ),
        sa.UniqueConstraint(
            "link_code_id",
            name="uq_identity_binding_events_link_code",
        ),
        sa.UniqueConstraint(
            "request_digest_key_id",
            "request_digest",
            name="uq_identity_binding_events_request",
        ),
        schema=SCHEMA,
    )
    _create_audit_indexes(BINDING_EVENTS)
    op.create_index(
        "ix_identity_binding_events_tenant_target_time",
        BINDING_EVENTS,
        ["tenant_id", "target_user_id", "occurred_at"],
        schema=SCHEMA,
    )
    op.create_index(
        "ix_identity_binding_events_account_time",
        BINDING_EVENTS,
        [
            "tenant_id",
            "provider",
            "provider_tenant_key",
            "provider_account_key",
            "occurred_at",
        ],
        schema=SCHEMA,
    )


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    _lock_transition_tables(_present_new_tables(inspector))
    inspector = sa.inspect(op.get_bind())
    state = _schema_state(inspector)
    if state == "final":
        _assert_final_shape(inspector)
        return

    duplicates = _duplicate_groups()
    if duplicates:
        summary = ", ".join(f"{name}={duplicates[name]}" for name in sorted(duplicates))
        raise RuntimeError(f"Cannot enable identity provisioning invariants while duplicate ownership exists ({summary})")

    op.create_unique_constraint(
        EXTERNAL_IDENTITY_REVERSE_UNIQUE,
        EXTERNAL_IDENTITIES,
        [
            "tenant_id",
            "user_id",
            "provider",
            "provider_tenant_key",
            "subject_type",
        ],
        schema=SCHEMA,
    )
    op.create_index(
        ACTIVE_MEMBERSHIP_UNIQUE,
        USER_TENANTS,
        ["tenant_id", "user_id"],
        unique=True,
        schema=SCHEMA,
        postgresql_where=sa.text("status = '1'"),
    )
    _create_policy_table()
    _create_link_code_table()
    _create_binding_event_table()
    _assert_final_shape(sa.inspect(op.get_bind()))


def _populated_provisioning_tables() -> dict[str, int]:
    populated: dict[str, int] = {}
    for table_name in (BINDING_EVENTS, LINK_CODES, TENANT_POLICIES):
        count = op.get_bind().execute(sa.text(f'SELECT count(*) FROM {SCHEMA}."{table_name}"')).scalar_one()
        if count:
            populated[table_name] = int(count)
    return populated


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    present_new_tables = _present_new_tables(inspector)
    if present_new_tables != {TENANT_POLICIES, LINK_CODES, BINDING_EVENTS}:
        raise RuntimeError(f"Cannot downgrade incomplete identity provisioning schema: present tables={sorted(present_new_tables)!r}")
    _lock_transition_tables(present_new_tables)
    _assert_final_shape(sa.inspect(op.get_bind()))

    populated = _populated_provisioning_tables()
    if populated:
        summary = ", ".join(f"{table_name}={populated[table_name]}" for table_name in sorted(populated))
        raise RuntimeError(f"Cannot downgrade identity provisioning while policy, code, or binding history exists ({summary})")

    op.drop_table(BINDING_EVENTS, schema=SCHEMA)
    op.drop_table(LINK_CODES, schema=SCHEMA)
    op.drop_table(TENANT_POLICIES, schema=SCHEMA)
    op.drop_constraint(
        EXTERNAL_IDENTITY_REVERSE_UNIQUE,
        EXTERNAL_IDENTITIES,
        schema=SCHEMA,
        type_="unique",
    )
    op.drop_index(
        ACTIVE_MEMBERSHIP_UNIQUE,
        table_name=USER_TENANTS,
        schema=SCHEMA,
    )
