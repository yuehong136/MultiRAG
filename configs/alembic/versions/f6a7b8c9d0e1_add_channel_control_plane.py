"""add MultiRAG channel control plane tables

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-07-31 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f6a7b8c9d0e1"
down_revision: str | None = "e5f6a7b8c9d0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "usr_ai"
CHAT_CHANNELS = "t_ai_chat_channels"
CHANNEL_SECRETS = "t_ai_channel_secrets"
CHANNEL_BINDINGS = "t_ai_channel_bindings"
CHANNEL_RUNTIME_STATUS = "t_ai_channel_runtime_status"


def _json_type() -> sa.types.TypeEngine:
    return postgresql.JSONB() if op.get_bind().dialect.name == "postgresql" else sa.JSON()


def _base_columns() -> list[sa.Column]:
    return [
        sa.Column("create_date", sa.DateTime(), nullable=True),
        sa.Column("update_date", sa.DateTime(), nullable=True),
        sa.Column("create_time", sa.BigInteger(), nullable=True),
        sa.Column("update_time", sa.BigInteger(), nullable=True),
    ]


def _create_timestamp_indexes(table_name: str) -> None:
    for column_name in ("create_date", "update_date", "create_time", "update_time"):
        op.create_index(
            f"ix_{table_name}_{column_name}",
            table_name,
            [column_name],
            schema=SCHEMA,
        )


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(CHAT_CHANNELS, schema=SCHEMA):
        op.create_table(
            CHAT_CHANNELS,
            *_base_columns(),
            sa.Column("id", sa.String(length=32), nullable=False),
            sa.Column("tenant_id", sa.String(length=32), nullable=False),
            sa.Column("name", sa.String(length=128), nullable=False),
            sa.Column("channel", sa.String(length=128), nullable=False),
            sa.Column("config", _json_type(), server_default=sa.text("'{}'"), nullable=False),
            sa.Column("chat_id", sa.String(length=32), nullable=True),
            sa.Column("status", sa.Integer(), server_default=sa.text("0"), nullable=False),
            sa.Column("generation", sa.Integer(), server_default=sa.text("1"), nullable=False),
            sa.CheckConstraint("status IN (0, 1)", name="ck_chat_channels_status"),
            sa.CheckConstraint("generation >= 1", name="ck_chat_channels_generation"),
            sa.PrimaryKeyConstraint("id", name="pk_t_ai_chat_channels"),
            schema=SCHEMA,
        )
        _create_timestamp_indexes(CHAT_CHANNELS)
        op.create_index("ix_chat_channels_tenant_channel", CHAT_CHANNELS, ["tenant_id", "channel"], schema=SCHEMA)
        op.create_index("ix_chat_channels_tenant_status", CHAT_CHANNELS, ["tenant_id", "status"], schema=SCHEMA)

    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(CHANNEL_SECRETS, schema=SCHEMA):
        op.create_table(
            CHANNEL_SECRETS,
            *_base_columns(),
            sa.Column("id", sa.String(length=32), nullable=False),
            sa.Column("channel_id", sa.String(length=32), nullable=False),
            sa.Column("ciphertext", sa.Text(), nullable=False),
            sa.Column("key_id", sa.String(length=128), nullable=False),
            sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
            sa.CheckConstraint("version >= 1", name="ck_channel_secrets_version"),
            sa.ForeignKeyConstraint(
                ["channel_id"],
                [f"{SCHEMA}.{CHAT_CHANNELS}.id"],
                name="fk_channel_secrets_channel_id",
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("id", name="pk_t_ai_channel_secrets"),
            sa.UniqueConstraint("channel_id", name="uq_channel_secrets_channel_id"),
            schema=SCHEMA,
        )
        _create_timestamp_indexes(CHANNEL_SECRETS)

    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(CHANNEL_BINDINGS, schema=SCHEMA):
        op.create_table(
            CHANNEL_BINDINGS,
            *_base_columns(),
            sa.Column("id", sa.String(length=32), nullable=False),
            sa.Column("channel_id", sa.String(length=32), nullable=False),
            sa.Column("target_type", sa.String(length=64), nullable=False),
            sa.Column("target_id", sa.String(length=32), nullable=False),
            sa.Column("target_revision_id", sa.String(length=32), nullable=True),
            sa.Column("policy", _json_type(), server_default=sa.text("'{}'"), nullable=False),
            sa.Column("enabled", sa.Boolean(), server_default=sa.false(), nullable=False),
            sa.Column("generation", sa.Integer(), server_default=sa.text("1"), nullable=False),
            sa.CheckConstraint(
                "target_type IN ('multirag.canvas_agent', 'multirag.dialog')",
                name="ck_channel_bindings_target_type",
            ),
            sa.CheckConstraint(
                "(target_type = 'multirag.canvas_agent' AND target_revision_id IS NOT NULL) OR (target_type = 'multirag.dialog' AND target_revision_id IS NULL)",
                name="ck_channel_bindings_revision",
            ),
            sa.CheckConstraint("generation >= 1", name="ck_channel_bindings_generation"),
            sa.ForeignKeyConstraint(
                ["channel_id"],
                [f"{SCHEMA}.{CHAT_CHANNELS}.id"],
                name="fk_channel_bindings_channel_id",
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("id", name="pk_t_ai_channel_bindings"),
            sa.UniqueConstraint("channel_id", name="uq_channel_bindings_channel_id"),
            schema=SCHEMA,
        )
        _create_timestamp_indexes(CHANNEL_BINDINGS)
        op.create_index("ix_channel_bindings_enabled", CHANNEL_BINDINGS, ["enabled"], schema=SCHEMA)
        op.create_index("ix_channel_bindings_target", CHANNEL_BINDINGS, ["target_type", "target_id"], schema=SCHEMA)

    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(CHANNEL_RUNTIME_STATUS, schema=SCHEMA):
        op.create_table(
            CHANNEL_RUNTIME_STATUS,
            *_base_columns(),
            sa.Column("id", sa.String(length=32), nullable=False),
            sa.Column("binding_id", sa.String(length=32), nullable=False),
            sa.Column("observed_generation", sa.Integer(), server_default=sa.text("0"), nullable=False),
            sa.Column("state", sa.String(length=32), server_default=sa.text("'waiting'"), nullable=False),
            sa.Column("runner_id", sa.String(length=128), nullable=True),
            sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("connected_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_error_code", sa.String(length=64), nullable=True),
            sa.CheckConstraint("observed_generation >= 0", name="ck_channel_runtime_observed_generation"),
            sa.CheckConstraint(
                "state IN ('waiting', 'starting', 'connected', 'stopping', 'stopped', 'error')",
                name="ck_channel_runtime_state",
            ),
            sa.ForeignKeyConstraint(
                ["binding_id"],
                [f"{SCHEMA}.{CHANNEL_BINDINGS}.id"],
                name="fk_channel_runtime_binding_id",
                ondelete="CASCADE",
            ),
            sa.PrimaryKeyConstraint("id", name="pk_t_ai_channel_runtime_status"),
            sa.UniqueConstraint("binding_id", name="uq_channel_runtime_status_binding_id"),
            schema=SCHEMA,
        )
        _create_timestamp_indexes(CHANNEL_RUNTIME_STATUS)
        op.create_index(
            "ix_channel_runtime_state_heartbeat",
            CHANNEL_RUNTIME_STATUS,
            ["state", "heartbeat_at"],
            schema=SCHEMA,
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    for table_name in (
        CHANNEL_RUNTIME_STATUS,
        CHANNEL_BINDINGS,
        CHANNEL_SECRETS,
        CHAT_CHANNELS,
    ):
        if inspector.has_table(table_name, schema=SCHEMA):
            op.drop_table(table_name, schema=SCHEMA)
            inspector = sa.inspect(op.get_bind())
