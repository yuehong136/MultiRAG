"""add durable MCP interaction sessions

Revision ID: c6d8e0f2a4b6
Revises: b4c6d8e0f2a4
Create Date: 2026-08-22 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c6d8e0f2a4b6"
down_revision: str | None = "b4c6d8e0f2a4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "usr_ai"
INTERACTIONS = "t_ai_mcp_interactions"
RESUME_JOBS = "t_ai_mcp_interaction_resume_jobs"


def _base_columns() -> list[sa.Column]:
    return [
        sa.Column("create_date", sa.DateTime(), nullable=True),
        sa.Column("update_date", sa.DateTime(), nullable=True),
        sa.Column("create_time", sa.BigInteger(), nullable=True),
        sa.Column("update_time", sa.BigInteger(), nullable=True),
    ]


def _create_base_indexes(table_name: str) -> None:
    for column_name in ("create_date", "update_date", "create_time", "update_time"):
        op.create_index(
            f"ix_usr_ai_{table_name}_{column_name}",
            table_name,
            [column_name],
            schema=SCHEMA,
        )


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(INTERACTIONS, schema=SCHEMA):
        op.create_table(
            INTERACTIONS,
            *_base_columns(),
            sa.Column("id", sa.String(length=32), nullable=False),
            sa.Column("tenant_id", sa.String(length=32), nullable=False),
            sa.Column("platform_user_id", sa.String(length=32), nullable=False),
            sa.Column("external_identity_id", sa.String(length=32), nullable=True),
            sa.Column("identity_revision", sa.BigInteger(), nullable=True),
            sa.Column("agent_id", sa.String(length=255), nullable=False),
            sa.Column("agent_revision_id", sa.String(length=255), nullable=False),
            sa.Column("mcp_server_id", sa.String(length=32), nullable=False),
            sa.Column("resource_name", sa.String(length=128), nullable=False),
            sa.Column("resource_uri", sa.Text(), nullable=False),
            sa.Column("tool_name", sa.String(length=255), nullable=False),
            sa.Column("call_digest", sa.String(length=64), nullable=False),
            sa.Column("schema_digest", sa.String(length=64), nullable=False),
            sa.Column("output_schema_digest", sa.String(length=64), nullable=True),
            sa.Column("output_schema_ciphertext", sa.Text(), nullable=True),
            sa.Column("output_schema_key_id", sa.String(length=16), nullable=True),
            sa.Column("original_arguments_ciphertext", sa.Text(), nullable=False),
            sa.Column("original_arguments_key_id", sa.String(length=16), nullable=False),
            sa.Column("input_requests_ciphertext", sa.Text(), nullable=False),
            sa.Column("input_requests_key_id", sa.String(length=16), nullable=False),
            sa.Column("request_state_ciphertext", sa.Text(), nullable=False),
            sa.Column("request_state_key_id", sa.String(length=16), nullable=False),
            sa.Column("policy_revision", sa.String(length=128), nullable=False),
            sa.Column("credential_generation", sa.BigInteger(), nullable=False),
            sa.Column("effect", sa.String(length=16), nullable=False),
            sa.Column("replay_mode", sa.String(length=16), nullable=False),
            sa.Column("state", sa.String(length=32), nullable=False),
            sa.Column("revision", sa.Integer(), nullable=False),
            sa.Column("round_count", sa.Integer(), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("result_ciphertext", sa.Text(), nullable=True),
            sa.Column("result_key_id", sa.String(length=16), nullable=True),
            sa.Column("provider", sa.String(length=64), nullable=True),
            sa.Column("presentation_ref", sa.String(length=255), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint(
                "state IN ('awaiting_input', 'response_ready', 'resuming', 'completed', 'declined', 'cancelled', 'expired', 'failed')",
                name="ck_mcp_interactions_state",
            ),
            sa.CheckConstraint(
                "effect IN ('read', 'prepare', 'side_effect')",
                name="ck_mcp_interactions_effect",
            ),
            sa.CheckConstraint(
                "replay_mode IN ('reusable', 'single_use')",
                name="ck_mcp_interactions_replay_mode",
            ),
            sa.CheckConstraint(
                "revision > 0 AND round_count > 0 AND credential_generation >= 0",
                name="ck_mcp_interactions_revision_round",
            ),
            sa.CheckConstraint(
                "call_digest ~ '^[0-9a-f]{64}$' AND schema_digest ~ '^[0-9a-f]{64}$' AND (output_schema_digest IS NULL OR output_schema_digest ~ '^[0-9a-f]{64}$')",
                name="ck_mcp_interactions_digest",
            ),
            sa.CheckConstraint(
                "(output_schema_digest IS NULL AND output_schema_ciphertext IS NULL AND output_schema_key_id IS NULL) OR "
                "(output_schema_digest IS NOT NULL AND output_schema_ciphertext IS NOT NULL AND output_schema_key_id IS NOT NULL)",
                name="ck_mcp_interactions_output_schema_payload",
            ),
            sa.CheckConstraint(
                "btrim(agent_id) <> '' AND btrim(agent_revision_id) <> '' AND btrim(mcp_server_id) <> '' AND btrim(resource_name) <> '' AND btrim(resource_uri) <> '' AND btrim(tool_name) <> ''",
                name="ck_mcp_interactions_binding_nonempty",
            ),
            sa.ForeignKeyConstraint(
                ["tenant_id"],
                [f"{SCHEMA}.t_ai_tenants.id"],
                name="fk_mcp_interactions_tenant_id",
                ondelete="RESTRICT",
            ),
            sa.ForeignKeyConstraint(
                ["platform_user_id"],
                [f"{SCHEMA}.t_ai_users.id"],
                name="fk_mcp_interactions_platform_user_id",
                ondelete="RESTRICT",
            ),
            sa.PrimaryKeyConstraint("id", name="pk_t_ai_mcp_interactions"),
            sa.UniqueConstraint(
                "id",
                "tenant_id",
                name="uq_mcp_interactions_id_tenant",
            ),
            schema=SCHEMA,
        )
        _create_base_indexes(INTERACTIONS)
        op.create_index(
            "ix_mcp_interactions_tenant_user_state",
            INTERACTIONS,
            ["tenant_id", "platform_user_id", "state"],
            schema=SCHEMA,
        )
        op.create_index(
            "ix_mcp_interactions_state_expiry",
            INTERACTIONS,
            ["state", "expires_at"],
            schema=SCHEMA,
        )

    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(RESUME_JOBS, schema=SCHEMA):
        op.create_table(
            RESUME_JOBS,
            *_base_columns(),
            sa.Column("id", sa.String(length=32), nullable=False),
            sa.Column("interaction_id", sa.String(length=32), nullable=False),
            sa.Column("tenant_id", sa.String(length=32), nullable=False),
            sa.Column("revision", sa.Integer(), nullable=False),
            sa.Column("response_idempotency_key", sa.String(length=64), nullable=False),
            sa.Column("input_response_ciphertext", sa.Text(), nullable=False),
            sa.Column("input_response_key_id", sa.String(length=16), nullable=False),
            sa.Column("state", sa.String(length=32), nullable=False),
            sa.Column("lease_owner", sa.String(length=64), nullable=True),
            sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
            sa.Column("attempt", sa.Integer(), nullable=False),
            sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("safe_error_code", sa.String(length=64), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint(
                "state IN ('response_ready', 'leased', 'succeeded', 'terminal_failed')",
                name="ck_mcp_interaction_jobs_state",
            ),
            sa.CheckConstraint(
                "revision > 0 AND attempt >= 0",
                name="ck_mcp_interaction_jobs_revision_attempt",
            ),
            sa.CheckConstraint(
                "(state = 'leased' AND lease_owner IS NOT NULL AND lease_until IS NOT NULL) OR (state <> 'leased' AND lease_owner IS NULL AND lease_until IS NULL)",
                name="ck_mcp_interaction_jobs_lease",
            ),
            sa.CheckConstraint(
                "response_idempotency_key ~ '^[0-9a-f]{64}$'",
                name="ck_mcp_interaction_jobs_idempotency",
            ),
            sa.ForeignKeyConstraint(
                ["interaction_id", "tenant_id"],
                [f"{SCHEMA}.{INTERACTIONS}.id", f"{SCHEMA}.{INTERACTIONS}.tenant_id"],
                name="fk_mcp_interaction_jobs_interaction_scope",
                ondelete="RESTRICT",
            ),
            sa.PrimaryKeyConstraint("id", name="pk_t_ai_mcp_interaction_resume_jobs"),
            sa.UniqueConstraint(
                "interaction_id",
                "revision",
                name="uq_mcp_interaction_jobs_round",
            ),
            sa.UniqueConstraint(
                "tenant_id",
                "response_idempotency_key",
                name="uq_mcp_interaction_jobs_tenant_idempotency",
            ),
            schema=SCHEMA,
        )
        _create_base_indexes(RESUME_JOBS)
        op.create_index(
            "ix_mcp_interaction_jobs_ready",
            RESUME_JOBS,
            ["state", "next_attempt_at", "created_at"],
            schema=SCHEMA,
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    for table_name in (RESUME_JOBS, INTERACTIONS):
        if inspector.has_table(table_name, schema=SCHEMA):
            op.drop_table(table_name, schema=SCHEMA)
            inspector = sa.inspect(op.get_bind())
