"""allow external-only platform users

Revision ID: 7c8d9e0f1a2b
Revises: e4f6a8b0c2d4
Create Date: 2026-08-12 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine.reflection import Inspector

revision: str = "7c8d9e0f1a2b"
down_revision: str | None = "e4f6a8b0c2d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "usr_ai"
TABLE = "t_ai_users"
ACCOUNT_KIND_COLUMN = "account_kind"
ACCOUNT_KIND_INDEX = "ix_usr_ai_t_ai_users_account_kind"
ACCOUNT_KIND_CHECK = "ck_users_account_kind"
EXTERNAL_PASSWORD_CHECK = "ck_users_external_password_null"
PASSWORD_ACCOUNT_EMAIL_CHECK = "ck_users_password_account_email"

_CHECK_SQL = {
    ACCOUNT_KIND_CHECK: "account_kind IN ('local', 'external', 'hybrid')",
    EXTERNAL_PASSWORD_CHECK: "account_kind <> 'external' OR password IS NULL",
    PASSWORD_ACCOUNT_EMAIL_CHECK: "account_kind = 'external' OR email IS NOT NULL",
}

_LEGACY_ACCOUNT_KIND_SQL = """
CASE
    WHEN login_channel IS NOT NULL AND login_channel <> 'password'
        THEN CASE WHEN password IS NULL THEN 'external' ELSE 'hybrid' END
    ELSE 'local'
END
"""


def _incompatible(label: str, actual: object, expected: object) -> RuntimeError:
    return RuntimeError(f"Existing {SCHEMA}.{TABLE} has incompatible {label}: expected {expected!r}, got {actual!r}")


def _assert_model_first_table_compatible(inspector: Inspector) -> None:
    """Accept a fresh model-first database that was stamped before upgrade."""

    columns = {str(column["name"]): column for column in inspector.get_columns(TABLE, schema=SCHEMA)}
    email = columns.get("email")
    if email is None or not bool(email["nullable"]):
        raise _incompatible("email nullability", None if email is None else bool(email["nullable"]), True)

    account_kind = columns.get(ACCOUNT_KIND_COLUMN)
    if account_kind is None:
        raise _incompatible("account_kind column", None, "VARCHAR(16) NOT NULL")
    account_kind_type = account_kind["type"]
    if not isinstance(account_kind_type, sa.String) or account_kind_type.length != 16 or bool(account_kind["nullable"]):
        raise _incompatible(
            "account_kind column",
            (account_kind_type, bool(account_kind["nullable"])),
            "VARCHAR(16) NOT NULL",
        )

    check_names = {str(constraint["name"]) for constraint in inspector.get_check_constraints(TABLE, schema=SCHEMA)}
    missing_checks = set(_CHECK_SQL) - check_names
    if missing_checks:
        raise _incompatible("account checks", sorted(check_names), sorted(_CHECK_SQL))

    indexes = {str(index["name"]): tuple(index["column_names"]) for index in inspector.get_indexes(TABLE, schema=SCHEMA)}
    if indexes.get(ACCOUNT_KIND_INDEX) != (ACCOUNT_KIND_COLUMN,):
        raise _incompatible(
            "account_kind index",
            indexes.get(ACCOUNT_KIND_INDEX),
            (ACCOUNT_KIND_COLUMN,),
        )

    unique_email_shapes = {tuple(constraint["column_names"]) for constraint in inspector.get_unique_constraints(TABLE, schema=SCHEMA)}
    unique_email_shapes.update(tuple(index["column_names"]) for index in inspector.get_indexes(TABLE, schema=SCHEMA) if bool(index.get("unique")))
    if ("email",) not in unique_email_shapes:
        raise _incompatible("email uniqueness", sorted(unique_email_shapes), [("email",)])


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(TABLE, schema=SCHEMA):
        raise RuntimeError(f"Required table {SCHEMA}.{TABLE} does not exist")

    columns = {str(column["name"]): column for column in inspector.get_columns(TABLE, schema=SCHEMA)}
    if ACCOUNT_KIND_COLUMN in columns:
        _assert_model_first_table_compatible(inspector)
        return

    email = columns.get("email")
    if email is None:
        raise RuntimeError(f"Required column {SCHEMA}.{TABLE}.email does not exist")

    op.alter_column(
        TABLE,
        "email",
        schema=SCHEMA,
        existing_type=sa.String(length=255),
        nullable=True,
    )
    op.add_column(
        TABLE,
        sa.Column(
            ACCOUNT_KIND_COLUMN,
            sa.String(length=16),
            nullable=True,
        ),
        schema=SCHEMA,
    )
    op.execute(
        sa.text(
            f"""
            UPDATE {SCHEMA}.{TABLE}
            SET {ACCOUNT_KIND_COLUMN} = {_LEGACY_ACCOUNT_KIND_SQL}
            WHERE {ACCOUNT_KIND_COLUMN} IS NULL
            """
        )
    )
    op.alter_column(
        TABLE,
        ACCOUNT_KIND_COLUMN,
        schema=SCHEMA,
        existing_type=sa.String(length=16),
        nullable=False,
        server_default=sa.text("'local'"),
    )
    for name, condition in _CHECK_SQL.items():
        op.create_check_constraint(name, TABLE, condition, schema=SCHEMA)
    op.create_index(
        ACCOUNT_KIND_INDEX,
        TABLE,
        [ACCOUNT_KIND_COLUMN],
        schema=SCHEMA,
    )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {str(column["name"]): column for column in inspector.get_columns(TABLE, schema=SCHEMA)}
    if ACCOUNT_KIND_COLUMN not in columns:
        return

    unreconstructable_count = (
        op.get_bind()
        .execute(
            sa.text(
                f"""
                SELECT count(*)
                FROM {SCHEMA}.{TABLE}
                WHERE account_kind <> {_LEGACY_ACCOUNT_KIND_SQL}
                """
            )
        )
        .scalar_one()
    )
    if unreconstructable_count:
        raise RuntimeError(f"Cannot downgrade while {SCHEMA}.{TABLE} contains {unreconstructable_count} account kinds that legacy columns cannot reconstruct")

    null_email_count = op.get_bind().execute(sa.text(f"SELECT count(*) FROM {SCHEMA}.{TABLE} WHERE email IS NULL")).scalar_one()
    if null_email_count:
        raise RuntimeError(f"Cannot downgrade while {SCHEMA}.{TABLE} contains {null_email_count} users without email")

    op.drop_index(ACCOUNT_KIND_INDEX, table_name=TABLE, schema=SCHEMA)
    for name in reversed(tuple(_CHECK_SQL)):
        op.drop_constraint(name, TABLE, schema=SCHEMA, type_="check")
    op.drop_column(TABLE, ACCOUNT_KIND_COLUMN, schema=SCHEMA)
    op.alter_column(
        TABLE,
        "email",
        schema=SCHEMA,
        existing_type=sa.String(length=255),
        nullable=False,
    )
