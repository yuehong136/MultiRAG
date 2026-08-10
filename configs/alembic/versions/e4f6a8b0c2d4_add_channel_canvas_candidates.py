"""add MultiRAG channel Canvas candidate metadata

Revision ID: e4f6a8b0c2d4
Revises: c1dbaa153d0a
Create Date: 2026-08-10 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine.reflection import Inspector

revision: str = "e4f6a8b0c2d4"
down_revision: str | None = "c1dbaa153d0a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "usr_ai"
TABLE = "t_ai_channel_canvas_candidates"

_NULLABLE_COLUMNS = {
    "create_date",
    "update_date",
    "create_time",
    "update_time",
    "public_session_id",
    "source_fingerprint",
    "publish_user_id",
    "publish_exp_user_id",
    "publish_name",
}
_STRING_LENGTHS = {
    "id": 32,
    "candidate_session_id": 32,
    "owner_token": 32,
    "target_id": 32,
    "public_session_id": 32,
    "source_fingerprint": 64,
    "state": 16,
    "publish_user_id": 255,
    "publish_exp_user_id": 255,
    "publish_name": 255,
}
_CHECK_NAMES = {
    "ck_channel_canvas_candidates_distinct_sessions",
    "ck_channel_canvas_candidates_identity",
    "ck_channel_canvas_candidates_state",
}
_UNIQUE_NAMES = {
    "uq_channel_canvas_candidates_owner",
    "uq_channel_canvas_candidates_session",
}
_INDEX_COLUMNS = {
    "ix_channel_canvas_candidates_state_expiry": (
        "state",
        "expires_at",
        "candidate_session_id",
    ),
    "ix_usr_ai_t_ai_channel_canvas_candidates_create_date": ("create_date",),
    "ix_usr_ai_t_ai_channel_canvas_candidates_create_time": ("create_time",),
    "ix_usr_ai_t_ai_channel_canvas_candidates_update_date": ("update_date",),
    "ix_usr_ai_t_ai_channel_canvas_candidates_update_time": ("update_time",),
}


def _base_columns() -> list[sa.Column]:
    return [
        sa.Column("create_date", sa.DateTime(), nullable=True),
        sa.Column("update_date", sa.DateTime(), nullable=True),
        sa.Column("create_time", sa.BigInteger(), nullable=True),
        sa.Column("update_time", sa.BigInteger(), nullable=True),
    ]


def _incompatible(label: str, actual: object, expected: object) -> RuntimeError:
    return RuntimeError(f"Existing {SCHEMA}.{TABLE} has incompatible {label}: expected {expected!r}, got {actual!r}")


def _assert_existing_table_compatible(inspector: Inspector) -> None:
    """Accept model-first startup only when the sidecar schema is exact."""

    columns = {str(column["name"]): column for column in inspector.get_columns(TABLE, schema=SCHEMA)}
    expected_names = {
        "create_date",
        "update_date",
        "create_time",
        "update_time",
        "id",
        "candidate_session_id",
        "owner_token",
        "target_id",
        "public_session_id",
        "source_fingerprint",
        "state",
        "expires_at",
        "publish_user_id",
        "publish_exp_user_id",
        "publish_name",
    }
    if set(columns) != expected_names:
        raise _incompatible("columns", sorted(columns), sorted(expected_names))

    for name, column in columns.items():
        expected_nullable = name in _NULLABLE_COLUMNS
        if bool(column["nullable"]) is not expected_nullable:
            raise _incompatible(
                f"column {name} nullability",
                bool(column["nullable"]),
                expected_nullable,
            )
        column_type = column["type"]
        if name in _STRING_LENGTHS:
            expected_length = _STRING_LENGTHS[name]
            if not isinstance(column_type, sa.String) or column_type.length != expected_length:
                raise _incompatible(
                    f"column {name} type",
                    column_type,
                    f"VARCHAR({expected_length})",
                )
        elif name in {"create_time", "update_time"}:
            if not isinstance(column_type, sa.BigInteger):
                raise _incompatible(f"column {name} type", column_type, "BIGINT")
        elif name in {"create_date", "update_date", "expires_at"}:
            expected_timezone = name == "expires_at"
            if not isinstance(column_type, sa.DateTime) or bool(column_type.timezone) is not expected_timezone:
                raise _incompatible(
                    f"column {name} type",
                    column_type,
                    f"TIMESTAMP(timezone={expected_timezone})",
                )

    primary_key = inspector.get_pk_constraint(TABLE, schema=SCHEMA)
    if tuple(primary_key.get("constrained_columns") or ()) != ("id",):
        raise _incompatible(
            "primary key",
            primary_key.get("constrained_columns"),
            ["id"],
        )

    unique_names = {str(constraint["name"]) for constraint in inspector.get_unique_constraints(TABLE, schema=SCHEMA)}
    if unique_names != _UNIQUE_NAMES:
        raise _incompatible("unique constraints", sorted(unique_names), sorted(_UNIQUE_NAMES))

    check_names = {str(constraint["name"]) for constraint in inspector.get_check_constraints(TABLE, schema=SCHEMA)}
    if check_names != _CHECK_NAMES:
        raise _incompatible("check constraints", sorted(check_names), sorted(_CHECK_NAMES))

    index_columns = {str(index["name"]): tuple(index["column_names"]) for index in inspector.get_indexes(TABLE, schema=SCHEMA) if not index.get("duplicates_constraint")}
    if index_columns != _INDEX_COLUMNS:
        raise _incompatible("indexes", index_columns, _INDEX_COLUMNS)

    foreign_keys = inspector.get_foreign_keys(TABLE, schema=SCHEMA)
    compatible_foreign_key = (
        len(foreign_keys) == 1
        and foreign_keys[0].get("constrained_columns") == ["candidate_session_id"]
        and foreign_keys[0].get("referred_schema") == SCHEMA
        and foreign_keys[0].get("referred_table") == "t_ai_api4conversations"
        and foreign_keys[0].get("referred_columns") == ["id"]
        and str((foreign_keys[0].get("options") or {}).get("ondelete", "")).upper() == "CASCADE"
    )
    if not compatible_foreign_key:
        raise _incompatible(
            "foreign key",
            foreign_keys,
            "candidate_session_id -> usr_ai.t_ai_api4conversations.id ON DELETE CASCADE",
        )


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table(TABLE, schema=SCHEMA):
        _assert_existing_table_compatible(inspector)
        return

    op.create_table(
        TABLE,
        *_base_columns(),
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("candidate_session_id", sa.String(length=32), nullable=False),
        sa.Column("owner_token", sa.String(length=32), nullable=False),
        sa.Column("target_id", sa.String(length=32), nullable=False),
        sa.Column("public_session_id", sa.String(length=32), nullable=True),
        sa.Column("source_fingerprint", sa.String(length=64), nullable=True),
        sa.Column(
            "state",
            sa.String(length=16),
            server_default=sa.text("'active'"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("publish_user_id", sa.String(length=255), nullable=True),
        sa.Column("publish_exp_user_id", sa.String(length=255), nullable=True),
        sa.Column("publish_name", sa.String(length=255), nullable=True),
        sa.CheckConstraint(
            "state IN ('active', 'finalizing')",
            name="ck_channel_canvas_candidates_state",
        ),
        sa.CheckConstraint(
            "public_session_id IS NULL OR public_session_id <> candidate_session_id",
            name="ck_channel_canvas_candidates_distinct_sessions",
        ),
        sa.CheckConstraint(
            "(public_session_id IS NULL AND source_fingerprint IS NULL AND publish_user_id IS NOT NULL) "
            "OR (public_session_id IS NOT NULL AND source_fingerprint IS NOT NULL "
            "AND publish_user_id IS NULL AND publish_exp_user_id IS NULL AND publish_name IS NULL)",
            name="ck_channel_canvas_candidates_identity",
        ),
        sa.ForeignKeyConstraint(
            ["candidate_session_id"],
            [f"{SCHEMA}.t_ai_api4conversations.id"],
            name="fk_channel_canvas_candidates_session",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_t_ai_channel_canvas_candidates"),
        sa.UniqueConstraint(
            "candidate_session_id",
            name="uq_channel_canvas_candidates_session",
        ),
        sa.UniqueConstraint(
            "owner_token",
            name="uq_channel_canvas_candidates_owner",
        ),
        schema=SCHEMA,
    )
    for column_name in ("create_date", "update_date", "create_time", "update_time"):
        op.create_index(
            f"ix_{SCHEMA}_{TABLE}_{column_name}",
            TABLE,
            [column_name],
            schema=SCHEMA,
        )
    op.create_index(
        "ix_channel_canvas_candidates_state_expiry",
        TABLE,
        ["state", "expires_at", "candidate_session_id"],
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_table(TABLE, schema=SCHEMA)
