"""Keep each model's latest health check and let keys be rotated with a grace period.

``t_ai_gateway_model_health`` holds one row per model, replaced by every
background health check. Keys gain the digest of the secret their last
rotation replaced, until when that secret still works, and when they were
rotated.

Each step is skipped when a table created from the current models already
has it, so both bootstrap paths end in the same schema.

Revision ID: 2c4e6a8b0d1f
Revises: 1b3d5f7a9c2e
"""

from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "2c4e6a8b0d1f"
down_revision: str | None = "1b3d5f7a9c2e"
branch_labels: str | None = None
depends_on: str | None = None
SCHEMA = "usr_ai"
_KEYS = "t_ai_gateway_keys"
_HEALTH = "t_ai_gateway_model_health"


def _key_columns() -> list[sa.Column[Any]]:
    return [
        sa.Column("previous_secret_hash", sa.CHAR(64), nullable=True),
        sa.Column("previous_secret_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rotated_at", sa.DateTime(timezone=True), nullable=True),
    ]


def _health() -> sa.Table:
    return sa.Table(
        _HEALTH,
        sa.MetaData(schema=SCHEMA),
        sa.Column("create_date", sa.DateTime(), nullable=True),
        sa.Column("update_date", sa.DateTime(), nullable=True),
        sa.Column("create_time", sa.BigInteger(), nullable=True),
        sa.Column("update_time", sa.BigInteger(), nullable=True),
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("tenant_id", sa.String(32), nullable=False),
        sa.Column("model_id", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("response_time_ms", sa.Integer(), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "model_id", name="uq_gateway_model_health_tenant_model"),
        sa.CheckConstraint("status IN ('healthy', 'unhealthy')", name="ck_gateway_model_health_status"),
    )


def upgrade() -> None:
    bind = op.get_bind()
    present = {column["name"] for column in sa.inspect(bind).get_columns(_KEYS, schema=SCHEMA)}
    for column in _key_columns():
        if column.name not in present:
            op.add_column(_KEYS, column, schema=SCHEMA)
    if not sa.inspect(bind).has_table(_HEALTH, schema=SCHEMA):
        _health().create(bind)


def downgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table(_HEALTH, schema=SCHEMA):
        op.drop_table(_HEALTH, schema=SCHEMA)
    present = {column["name"] for column in sa.inspect(bind).get_columns(_KEYS, schema=SCHEMA)}
    for column in _key_columns():
        if column.name in present:
            op.drop_column(_KEYS, column.name, schema=SCHEMA)
