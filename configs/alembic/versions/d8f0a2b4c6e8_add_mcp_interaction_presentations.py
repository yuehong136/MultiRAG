"""add durable Channel presentation and callback inbox

Revision ID: d8f0a2b4c6e8
Revises: c6d8e0f2a4b6
Create Date: 2026-08-23 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d8f0a2b4c6e8"
down_revision: str | None = "c6d8e0f2a4b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "usr_ai"
PRESENTATIONS = "t_ai_mcp_interaction_presentations"
RECEIPTS = "t_ai_mcp_interaction_callback_receipts"


def _base_columns() -> list[sa.Column]:
    return [
        sa.Column("create_date", sa.DateTime(), nullable=True),
        sa.Column("update_date", sa.DateTime(), nullable=True),
        sa.Column("create_time", sa.BigInteger(), nullable=True),
        sa.Column("update_time", sa.BigInteger(), nullable=True),
    ]


def _create_base_indexes(table: str) -> None:
    for column in ("create_date", "update_date", "create_time", "update_time"):
        op.create_index(f"ix_{SCHEMA}_{table}_{column}", table, [column], schema=SCHEMA)


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(PRESENTATIONS, schema=SCHEMA):
        op.create_table(
            PRESENTATIONS,
            *_base_columns(),
            sa.Column("id", sa.String(length=32), nullable=False),
            sa.Column("interaction_id", sa.String(length=32), nullable=False),
            sa.Column("tenant_id", sa.String(length=32), nullable=False),
            sa.Column("revision", sa.Integer(), nullable=False),
            sa.Column("binding_id", sa.String(length=32), nullable=False),
            sa.Column("binding_generation", sa.Integer(), nullable=False),
            sa.Column("provider", sa.String(length=64), nullable=False),
            sa.Column("provider_account_id", sa.String(length=32), nullable=False),
            sa.Column("source_event_digest", sa.String(length=64), nullable=False),
            sa.Column("conversation_ref", sa.String(length=255), nullable=False),
            sa.Column("presentation_ref", sa.String(length=255), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("response_state", sa.String(length=16), nullable=False),
            sa.Column("delivery_kind", sa.String(length=16), nullable=False),
            sa.Column("delivery_state", sa.String(length=16), nullable=False),
            sa.Column("delivery_projection", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
            sa.Column("form_mapping_ciphertext", sa.Text(), nullable=True),
            sa.Column("form_mapping_key_id", sa.String(length=16), nullable=True),
            sa.Column("nonce_digest", sa.String(length=64), nullable=True),
            sa.Column("delivery_token_digest", sa.String(length=64), nullable=True),
            sa.Column("delivery_lease_owner", sa.String(length=64), nullable=True),
            sa.Column("delivery_lease_until", sa.DateTime(timezone=True), nullable=True),
            sa.Column("delivery_attempt", sa.Integer(), nullable=False),
            sa.Column("delivery_next_attempt_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("safe_error_code", sa.String(length=64), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint("response_state IN ('open', 'received', 'claimed', 'terminal')", name="ck_mcp_interaction_presentations_response_state"),
            sa.CheckConstraint("delivery_kind IN ('form', 'terminal')", name="ck_mcp_interaction_presentations_delivery_kind"),
            sa.CheckConstraint("delivery_state IN ('pending', 'leased', 'delivered', 'failed')", name="ck_mcp_interaction_presentations_delivery_state"),
            sa.CheckConstraint("revision > 0 AND binding_generation > 0 AND delivery_attempt >= 0", name="ck_mcp_interaction_presentations_counters"),
            sa.CheckConstraint(
                "btrim(provider) <> '' AND btrim(conversation_ref) <> '' AND btrim(presentation_ref) <> ''",
                name="ck_mcp_interaction_presentations_nonempty",
            ),
            sa.CheckConstraint("source_event_digest ~ '^[0-9a-f]{64}$'", name="ck_mcp_interaction_presentations_source_digest"),
            sa.CheckConstraint("nonce_digest IS NULL OR nonce_digest ~ '^[0-9a-f]{64}$'", name="ck_mcp_interaction_presentations_nonce_digest"),
            sa.CheckConstraint("delivery_token_digest IS NULL OR delivery_token_digest ~ '^[0-9a-f]{64}$'", name="ck_mcp_interaction_presentations_delivery_token_digest"),
            sa.CheckConstraint(
                "(form_mapping_ciphertext IS NULL AND form_mapping_key_id IS NULL) OR (form_mapping_ciphertext IS NOT NULL AND form_mapping_key_id IS NOT NULL)",
                name="ck_mcp_interaction_presentations_form_mapping",
            ),
            sa.CheckConstraint(
                "(delivery_state = 'leased' AND delivery_lease_owner IS NOT NULL AND delivery_lease_until IS NOT NULL AND delivery_token_digest IS NOT NULL) OR "
                "(delivery_state <> 'leased' AND delivery_lease_owner IS NULL AND delivery_lease_until IS NULL AND delivery_token_digest IS NULL)",
                name="ck_mcp_interaction_presentations_delivery_lease",
            ),
            sa.ForeignKeyConstraint(
                ["interaction_id", "tenant_id"],
                [f"{SCHEMA}.t_ai_mcp_interactions.id", f"{SCHEMA}.t_ai_mcp_interactions.tenant_id"],
                name="fk_mcp_interaction_presentations_interaction_scope",
                ondelete="RESTRICT",
            ),
            sa.ForeignKeyConstraint(["binding_id"], [f"{SCHEMA}.t_ai_channel_bindings.id"], name="fk_mcp_interaction_presentations_binding_id", ondelete="CASCADE"),
            sa.ForeignKeyConstraint(
                ["provider_account_id", "tenant_id", "provider"],
                [
                    f"{SCHEMA}.t_ai_identity_provider_accounts.id",
                    f"{SCHEMA}.t_ai_identity_provider_accounts.tenant_id",
                    f"{SCHEMA}.t_ai_identity_provider_accounts.provider",
                ],
                name="fk_mcp_interaction_presentations_provider_account_scope",
                ondelete="RESTRICT",
            ),
            sa.PrimaryKeyConstraint("id", name="pk_t_ai_mcp_interaction_presentations"),
            sa.UniqueConstraint("interaction_id", "revision", name="uq_mcp_interaction_presentations_round"),
            sa.UniqueConstraint("binding_id", "source_event_digest", name="uq_mcp_interaction_presentations_source_event"),
            sa.UniqueConstraint("id", "binding_id", name="uq_mcp_interaction_presentations_id_binding"),
            schema=SCHEMA,
        )
        _create_base_indexes(PRESENTATIONS)
        op.create_index(
            "ix_mcp_interaction_presentations_delivery_ready",
            PRESENTATIONS,
            [
                "binding_id",
                "binding_generation",
                "delivery_state",
                "delivery_next_attempt_at",
                "delivery_lease_until",
                "created_at",
            ],
            schema=SCHEMA,
        )
        op.create_index(
            "ix_mcp_interaction_presentations_reconcile",
            PRESENTATIONS,
            ["response_state", "updated_at"],
            schema=SCHEMA,
        )

    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(RECEIPTS, schema=SCHEMA):
        op.create_table(
            RECEIPTS,
            *_base_columns(),
            sa.Column("id", sa.String(length=32), nullable=False),
            sa.Column("presentation_id", sa.String(length=32), nullable=False),
            sa.Column("binding_id", sa.String(length=32), nullable=False),
            sa.Column("event_digest", sa.String(length=64), nullable=False),
            sa.Column("payload_digest", sa.String(length=64), nullable=False),
            sa.Column("payload_ciphertext", sa.Text(), nullable=False),
            sa.Column("payload_key_id", sa.String(length=16), nullable=False),
            sa.Column("state", sa.String(length=16), nullable=False),
            sa.Column("lease_owner", sa.String(length=64), nullable=True),
            sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
            sa.Column("attempt", sa.Integer(), nullable=False),
            sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("safe_error_code", sa.String(length=64), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint("state IN ('received', 'leased', 'claimed', 'rejected')", name="ck_mcp_interaction_callback_receipts_state"),
            sa.CheckConstraint("attempt >= 0 AND event_digest ~ '^[0-9a-f]{64}$' AND payload_digest ~ '^[0-9a-f]{64}$'", name="ck_mcp_interaction_callback_receipts_attempt_digest"),
            sa.CheckConstraint(
                "(state = 'leased' AND lease_owner IS NOT NULL AND lease_until IS NOT NULL) OR (state <> 'leased' AND lease_owner IS NULL AND lease_until IS NULL)",
                name="ck_mcp_interaction_callback_receipts_lease",
            ),
            sa.ForeignKeyConstraint(
                ["presentation_id", "binding_id"],
                [
                    f"{SCHEMA}.{PRESENTATIONS}.id",
                    f"{SCHEMA}.{PRESENTATIONS}.binding_id",
                ],
                name="fk_mcp_interaction_callback_receipts_presentation_scope",
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(["binding_id"], [f"{SCHEMA}.t_ai_channel_bindings.id"], name="fk_mcp_interaction_callback_receipts_binding_id", ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id", name="pk_t_ai_mcp_interaction_callback_receipts"),
            sa.UniqueConstraint("binding_id", "event_digest", name="uq_mcp_interaction_callback_receipts_event"),
            schema=SCHEMA,
        )
        _create_base_indexes(RECEIPTS)
        op.create_index(
            "ix_mcp_interaction_callback_receipts_ready",
            RECEIPTS,
            ["state", "next_attempt_at", "lease_until", "created_at"],
            schema=SCHEMA,
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    for table in (RECEIPTS, PRESENTATIONS):
        if inspector.has_table(table, schema=SCHEMA):
            op.drop_table(table, schema=SCHEMA)
            inspector = sa.inspect(op.get_bind())
