"""Add tenant-scoped immutable folder snapshots.

Revision ID: c9e1f3a5b7d9
Revises: b8d0f2a4c6e8
"""

import re
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "c9e1f3a5b7d9"
down_revision: str | None = "b8d0f2a4c6e8"
branch_labels: str | None = None
depends_on: str | None = None
SCHEMA = "usr_ai"


def _timestamps() -> list[sa.Column[Any]]:
    return [
        sa.Column("create_date", sa.DateTime(), nullable=True, index=True),
        sa.Column("update_date", sa.DateTime(), nullable=True, index=True),
        sa.Column("create_time", sa.BigInteger(), nullable=True, index=True),
        sa.Column("update_time", sa.BigInteger(), nullable=True, index=True),
    ]


def _tables() -> tuple[sa.Table, sa.Table]:
    metadata = sa.MetaData(schema=SCHEMA)
    commits = sa.Table(
        "t_ai_file_commits",
        metadata,
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("tenant_id", sa.String(32), nullable=False, index=True),
        sa.Column("folder_id", sa.String(32), nullable=False, index=True),
        sa.Column("parent_id", sa.String(32), nullable=True),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("message", sa.String(512), nullable=False, server_default=""),
        sa.Column("author_id", sa.String(32), nullable=False),
        sa.Column("file_count", sa.Integer(), nullable=False),
        sa.Column("tree_state", JSONB(), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint("tenant_id", "folder_id", "sequence", name="uq_file_commits_scope_sequence"),
        sa.CheckConstraint("sequence >= 1", name="ck_file_commits_sequence"),
        sa.CheckConstraint("file_count >= 0", name="ck_file_commits_file_count"),
    )
    items = sa.Table(
        "t_ai_file_commit_items",
        metadata,
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("commit_id", sa.String(32), sa.ForeignKey(f"{SCHEMA}.t_ai_file_commits.id", ondelete="RESTRICT"), nullable=False, index=True),
        sa.Column("file_id", sa.String(32), nullable=False),
        sa.Column("operation", sa.String(16), nullable=False),
        sa.Column("old_hash", sa.String(64), nullable=True),
        sa.Column("new_hash", sa.String(64), nullable=True),
        sa.Column("old_location", sa.String(255), nullable=True),
        sa.Column("new_location", sa.String(255), nullable=True),
        sa.Column("old_name", sa.String(255), nullable=True),
        sa.Column("new_name", sa.String(255), nullable=True),
        *_timestamps(),
        sa.UniqueConstraint("commit_id", "file_id", name="uq_file_commit_items_commit_file"),
    )
    return commits, items


def _validate(bind: sa.Connection, table: sa.Table) -> None:
    inspector = sa.inspect(bind)
    columns = {column["name"]: column for column in inspector.get_columns(table.name, schema=SCHEMA)}
    if set(columns) != set(table.columns.keys()):
        raise RuntimeError(f"Incompatible file commit columns: {table.name}")
    for column in table.columns:
        actual = columns[column.name]
        if actual["type"].compile(dialect=bind.dialect) != column.type.compile(dialect=bind.dialect) or actual["nullable"] != column.nullable:
            raise RuntimeError(f"Incompatible file commit column: {table.name}.{column.name}")
    if inspector.get_pk_constraint(table.name, schema=SCHEMA)["constrained_columns"] != ["id"]:
        raise RuntimeError(f"Incompatible file commit primary key: {table.name}")
    actual_unique = {tuple(constraint["column_names"]) for constraint in inspector.get_unique_constraints(table.name, schema=SCHEMA)}
    actual_checks = {item["name"]: re.sub(r"[\s()]", "", item["sqltext"]) for item in inspector.get_check_constraints(table.name, schema=SCHEMA)}
    for constraint in table.constraints:
        if isinstance(constraint, sa.UniqueConstraint) and tuple(column.name for column in constraint.columns) not in actual_unique:
            raise RuntimeError(f"Missing file commit unique constraint: {table.name}")
        if isinstance(constraint, sa.CheckConstraint) and actual_checks.get(constraint.name) != re.sub(r"[\s()]", "", str(constraint.sqltext)):
            raise RuntimeError(f"Incompatible file commit check constraint: {table.name}")
    if table.foreign_keys and not any(
        item["constrained_columns"] == ["commit_id"]
        and item["referred_schema"] == SCHEMA
        and item["referred_table"] == "t_ai_file_commits"
        and item["referred_columns"] == ["id"]
        and item["options"].get("ondelete") == "RESTRICT"
        for item in inspector.get_foreign_keys(table.name, schema=SCHEMA)
    ):
        raise RuntimeError("Missing file commit item parent constraint")
    actual_indexes = {tuple(index["column_names"]) for index in inspector.get_indexes(table.name, schema=SCHEMA)}
    if any(tuple(column.name for column in index.columns) not in actual_indexes for index in table.indexes):
        raise RuntimeError(f"Missing file commit indexes: {table.name}")


def upgrade() -> None:
    bind = op.get_bind()
    for table in _tables():
        if sa.inspect(bind).has_table(table.name, schema=SCHEMA):
            _validate(bind, table)
        else:
            table.create(bind)


def downgrade() -> None:
    bind = op.get_bind()
    tables = _tables()
    for table in tables:
        if bind.scalar(sa.select(sa.exists().select_from(table))):
            raise RuntimeError("Cannot discard persisted file commit history")
    for table in reversed(tables):
        table.drop(bind)
