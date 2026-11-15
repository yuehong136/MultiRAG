"""Add model gateway tables.

Revision ID: d1f3a5c7e9b2
Revises: c9e1f3a5b7d9
"""

from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "d1f3a5c7e9b2"
down_revision: str | None = "c9e1f3a5b7d9"
branch_labels: str | None = None
depends_on: str | None = None
SCHEMA = "usr_ai"


def _timestamps(*, index: bool = True) -> list[sa.Column[Any]]:
    return [
        sa.Column("create_date", sa.DateTime(), nullable=True, index=index),
        sa.Column("update_date", sa.DateTime(), nullable=True, index=index),
        sa.Column("create_time", sa.BigInteger(), nullable=True, index=index),
        sa.Column("update_time", sa.BigInteger(), nullable=True, index=index),
    ]


def _tables() -> tuple[sa.Table, sa.Table, sa.Table]:
    metadata = sa.MetaData(schema=SCHEMA)
    models = sa.Table(
        "t_ai_gateway_models",
        metadata,
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("tenant_id", sa.String(32), nullable=False, index=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("model_id", sa.BigInteger(), nullable=False, index=True),
        sa.Column("model_type", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(32), nullable=False),
        sa.Column("updated_by", sa.String(32), nullable=True),
        *_timestamps(),
        sa.UniqueConstraint("tenant_id", "name", name="uq_gateway_models_tenant_name"),
        sa.CheckConstraint("status IN ('active', 'inactive')", name="ck_gateway_models_status"),
        sa.CheckConstraint("model_type IN ('chat', 'embedding', 'rerank')", name="ck_gateway_models_type"),
    )
    keys = sa.Table(
        "t_ai_gateway_keys",
        metadata,
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("tenant_id", sa.String(32), nullable=False, index=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("tags", JSONB(), nullable=True),
        sa.Column("secret_hash", sa.CHAR(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("allow_all_models", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("allowed_model_ids", JSONB(), nullable=False, server_default="[]"),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(32), nullable=False),
        sa.Column("updated_by", sa.String(32), nullable=True),
        *_timestamps(),
        sa.CheckConstraint("status IN ('active', 'disabled', 'revoked')", name="ck_gateway_keys_status"),
        sa.Index("uq_gateway_keys_tenant_name_live", "tenant_id", "name", unique=True, postgresql_where=sa.text("status <> 'revoked'")),
    )
    logs = sa.Table(
        "t_ai_gateway_request_logs",
        metadata,
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("tenant_id", sa.String(32), nullable=False),
        sa.Column("key_id", sa.String(32), nullable=False),
        sa.Column("key_name", sa.String(128), nullable=False),
        sa.Column("model", sa.String(128), nullable=True),
        sa.Column("gateway_model_id", sa.String(32), nullable=True),
        sa.Column("upstream_model_id", sa.BigInteger(), nullable=True),
        sa.Column("upstream_model_name", sa.String(255), nullable=True),
        sa.Column("endpoint", sa.String(32), nullable=False),
        sa.Column("stream", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("status_code", sa.Integer(), nullable=False),
        sa.Column("error_type", sa.String(64), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("prompt_tokens", sa.BigInteger(), nullable=True),
        sa.Column("completion_tokens", sa.BigInteger(), nullable=True),
        sa.Column("total_tokens", sa.BigInteger(), nullable=True),
        sa.Column("usage_details", JSONB(), nullable=True),
        sa.Column("usage_source", sa.String(16), nullable=False, server_default="unavailable"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("first_token_ms", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("end_user", sa.String(255), nullable=True),
        sa.Column("client_ip", sa.String(64), nullable=True),
        sa.Column("tags", JSONB(), nullable=True),
        *_timestamps(index=False),
        sa.CheckConstraint("usage_source IN ('reported', 'estimated', 'unavailable')", name="ck_gateway_request_logs_usage_source"),
        sa.Index("ix_gateway_request_logs_tenant_started", "tenant_id", "started_at"),
        sa.Index("ix_gateway_request_logs_key_started", "key_id", "started_at"),
        sa.Index("ix_gateway_request_logs_model_started", "gateway_model_id", "started_at"),
    )
    return models, keys, logs


def _validate(bind: sa.Connection, table: sa.Table) -> None:
    """Accept a table created model-first only when it matches this revision."""
    inspector = sa.inspect(bind)
    columns = {column["name"]: column for column in inspector.get_columns(table.name, schema=SCHEMA)}
    if set(columns) != set(table.columns.keys()):
        raise RuntimeError(f"Incompatible model gateway columns: {table.name}")
    for column in table.columns:
        actual = columns[column.name]
        if actual["type"].compile(dialect=bind.dialect) != column.type.compile(dialect=bind.dialect) or actual["nullable"] != column.nullable:
            raise RuntimeError(f"Incompatible model gateway column: {table.name}.{column.name}")
    if inspector.get_pk_constraint(table.name, schema=SCHEMA)["constrained_columns"] != ["id"]:
        raise RuntimeError(f"Incompatible model gateway primary key: {table.name}")
    actual_unique = {tuple(constraint["column_names"]) for constraint in inspector.get_unique_constraints(table.name, schema=SCHEMA)}
    actual_checks = {item["name"] for item in inspector.get_check_constraints(table.name, schema=SCHEMA)}
    for constraint in table.constraints:
        if isinstance(constraint, sa.UniqueConstraint) and tuple(column.name for column in constraint.columns) not in actual_unique:
            raise RuntimeError(f"Missing model gateway unique constraint: {table.name}")
        if isinstance(constraint, sa.CheckConstraint) and constraint.name not in actual_checks:
            raise RuntimeError(f"Missing model gateway check constraint: {table.name}.{constraint.name}")
    actual_indexes = {(tuple(index["column_names"]), bool(index["unique"])) for index in inspector.get_indexes(table.name, schema=SCHEMA)}
    if any((tuple(column.name for column in index.columns), bool(index.unique)) not in actual_indexes for index in table.indexes):
        raise RuntimeError(f"Missing model gateway indexes: {table.name}")


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
            raise RuntimeError("Cannot discard persisted model gateway data")
    for table in reversed(tables):
        table.drop(bind)
