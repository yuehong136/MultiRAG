"""add durable identity reconciliation state

Revision ID: e1f3a5c7b9d0
Revises: d8f0a2b4c6e8
Create Date: 2026-08-24 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e1f3a5c7b9d0"
down_revision: str | None = "d8f0a2b4c6e8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "usr_ai"
CHECKPOINTS = "t_ai_identity_reconciliation_checkpoints"
TARGETS = "t_ai_identity_reconciliation_targets"


def _base_columns() -> list[sa.Column]:
    return [
        sa.Column("create_date", sa.DateTime(), nullable=True),
        sa.Column("update_date", sa.DateTime(), nullable=True),
        sa.Column("create_time", sa.BigInteger(), nullable=True),
        sa.Column("update_time", sa.BigInteger(), nullable=True),
    ]


def _create_base_indexes(table: str) -> None:
    for column in ("create_date", "update_date", "create_time", "update_time"):
        op.create_index(
            f"ix_{SCHEMA}_{table}_{column}",
            table,
            [column],
            schema=SCHEMA,
        )


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(CHECKPOINTS, schema=SCHEMA):
        op.create_table(
            CHECKPOINTS,
            *_base_columns(),
            sa.Column("id", sa.String(length=32), nullable=False),
            sa.Column("provider_account_id", sa.String(length=32), nullable=False),
            sa.Column("tenant_id", sa.String(length=32), nullable=False),
            sa.Column("provider", sa.String(length=64), nullable=False),
            sa.Column("cursor_identity_id", sa.String(length=32), nullable=True),
            sa.Column("cycle_started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("lease_owner", sa.String(length=64), nullable=True),
            sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
            sa.Column("lease_attempt", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
            sa.Column("processed_count", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
            sa.Column("tightened_count", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
            sa.Column("error_count", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
            sa.Column("consecutive_failures", sa.Integer(), server_default=sa.text("0"), nullable=False),
            sa.Column("safe_error_code", sa.String(length=64), nullable=True),
            sa.CheckConstraint(
                "btrim(provider) <> ''",
                name="ck_identity_reconciliation_checkpoints_nonempty",
            ),
            sa.CheckConstraint(
                "lease_attempt >= 0 AND processed_count >= 0 AND tightened_count >= 0 AND error_count >= 0 AND consecutive_failures >= 0",
                name="ck_identity_reconciliation_checkpoints_counters",
            ),
            sa.CheckConstraint(
                "(lease_owner IS NULL AND lease_until IS NULL) OR (lease_owner IS NOT NULL AND btrim(lease_owner) <> '' AND lease_until IS NOT NULL)",
                name="ck_identity_reconciliation_checkpoints_lease",
            ),
            sa.CheckConstraint(
                "cycle_started_at IS NOT NULL OR cursor_identity_id IS NULL",
                name="ck_identity_reconciliation_checkpoints_cycle_cursor",
            ),
            sa.ForeignKeyConstraint(
                ["tenant_id"],
                [f"{SCHEMA}.t_ai_tenants.id"],
                name="fk_identity_reconciliation_checkpoints_tenant_id",
                ondelete="RESTRICT",
            ),
            sa.ForeignKeyConstraint(
                ["provider_account_id", "tenant_id", "provider"],
                [
                    f"{SCHEMA}.t_ai_identity_provider_accounts.id",
                    f"{SCHEMA}.t_ai_identity_provider_accounts.tenant_id",
                    f"{SCHEMA}.t_ai_identity_provider_accounts.provider",
                ],
                name="fk_identity_reconciliation_checkpoints_account_scope",
                ondelete="RESTRICT",
            ),
            sa.PrimaryKeyConstraint("id", name="pk_t_ai_identity_reconciliation_checkpoints"),
            sa.UniqueConstraint(
                "provider_account_id",
                name="uq_identity_reconciliation_checkpoints_account",
            ),
            sa.UniqueConstraint(
                "id",
                "provider_account_id",
                "tenant_id",
                "provider",
                name="uq_identity_reconciliation_checkpoints_target_scope",
            ),
            schema=SCHEMA,
        )
        _create_base_indexes(CHECKPOINTS)
        op.create_index(
            "ix_identity_reconciliation_checkpoints_due",
            CHECKPOINTS,
            ["next_run_at", "lease_until"],
            schema=SCHEMA,
        )
        op.create_index(
            "ix_identity_reconciliation_checkpoints_tenant",
            CHECKPOINTS,
            ["tenant_id", "provider"],
            schema=SCHEMA,
        )

    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(TARGETS, schema=SCHEMA):
        op.create_table(
            TARGETS,
            *_base_columns(),
            sa.Column("id", sa.String(length=32), nullable=False),
            sa.Column("checkpoint_id", sa.String(length=32), nullable=False),
            sa.Column("provider_account_id", sa.String(length=32), nullable=False),
            sa.Column("tenant_id", sa.String(length=32), nullable=False),
            sa.Column("provider", sa.String(length=64), nullable=False),
            sa.Column("provider_tenant_key", sa.String(length=255), nullable=False),
            sa.Column("external_identity_id", sa.String(length=32), nullable=False),
            sa.Column("account_revision", sa.BigInteger(), nullable=False),
            sa.Column("account_scope_change_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("identity_revision", sa.BigInteger(), nullable=False),
            sa.Column("state", sa.String(length=16), server_default=sa.text("'pending'"), nullable=False),
            sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_outcome", sa.String(length=16), nullable=True),
            sa.Column("not_found_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
            sa.Column("first_not_found_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_not_found_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_observed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("proof_account_revision", sa.BigInteger(), nullable=True),
            sa.Column("proof_scope_change_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("proof_identity_revision", sa.BigInteger(), nullable=True),
            sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("safe_error_code", sa.String(length=64), nullable=True),
            sa.CheckConstraint(
                "state IN ('pending', 'completed', 'failed')",
                name="ck_identity_reconciliation_targets_state",
            ),
            sa.CheckConstraint(
                "last_outcome IS NULL OR last_outcome IN ('resolved', 'inactive', 'not_found', 'not_in_scope', 'unavailable', 'conflict', 'invalid')",
                name="ck_identity_reconciliation_targets_outcome",
            ),
            sa.CheckConstraint(
                "account_revision >= 1 AND identity_revision >= 1 AND not_found_count >= 0",
                name="ck_identity_reconciliation_targets_counters",
            ),
            sa.CheckConstraint(
                "(not_found_count = 0 AND first_not_found_at IS NULL AND last_not_found_at IS NULL) OR "
                "(not_found_count > 0 AND first_not_found_at IS NOT NULL AND last_not_found_at IS NOT NULL "
                "AND last_not_found_at >= first_not_found_at)",
                name="ck_identity_reconciliation_targets_not_found",
            ),
            sa.CheckConstraint(
                "(verified_at IS NULL AND proof_account_revision IS NULL AND proof_scope_change_at IS NULL "
                "AND proof_identity_revision IS NULL) OR "
                "(verified_at IS NOT NULL AND proof_account_revision >= 1 AND proof_identity_revision >= 1)",
                name="ck_identity_reconciliation_targets_proof_fence",
            ),
            sa.CheckConstraint(
                "btrim(provider) <> '' AND btrim(provider_tenant_key) <> ''",
                name="ck_identity_reconciliation_targets_nonempty",
            ),
            sa.ForeignKeyConstraint(
                ["checkpoint_id", "provider_account_id", "tenant_id", "provider"],
                [
                    f"{SCHEMA}.{CHECKPOINTS}.id",
                    f"{SCHEMA}.{CHECKPOINTS}.provider_account_id",
                    f"{SCHEMA}.{CHECKPOINTS}.tenant_id",
                    f"{SCHEMA}.{CHECKPOINTS}.provider",
                ],
                name="fk_identity_reconciliation_targets_checkpoint_scope",
                ondelete="RESTRICT",
            ),
            sa.ForeignKeyConstraint(
                ["provider_account_id", "tenant_id", "provider"],
                [
                    f"{SCHEMA}.t_ai_identity_provider_accounts.id",
                    f"{SCHEMA}.t_ai_identity_provider_accounts.tenant_id",
                    f"{SCHEMA}.t_ai_identity_provider_accounts.provider",
                ],
                name="fk_identity_reconciliation_targets_account_scope",
                ondelete="RESTRICT",
            ),
            sa.ForeignKeyConstraint(
                ["external_identity_id", "tenant_id", "provider", "provider_tenant_key"],
                [
                    f"{SCHEMA}.t_ai_external_identities.id",
                    f"{SCHEMA}.t_ai_external_identities.tenant_id",
                    f"{SCHEMA}.t_ai_external_identities.provider",
                    f"{SCHEMA}.t_ai_external_identities.provider_tenant_key",
                ],
                name="fk_identity_reconciliation_targets_identity_scope",
                ondelete="RESTRICT",
            ),
            sa.PrimaryKeyConstraint("id", name="pk_t_ai_identity_reconciliation_targets"),
            sa.UniqueConstraint(
                "provider_account_id",
                "external_identity_id",
                name="uq_identity_reconciliation_targets_account_identity",
            ),
            schema=SCHEMA,
        )
        _create_base_indexes(TARGETS)
        op.create_index(
            "ix_identity_reconciliation_targets_pending",
            TARGETS,
            ["checkpoint_id", "state", "next_attempt_at"],
            schema=SCHEMA,
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    for table in (TARGETS, CHECKPOINTS):
        if inspector.has_table(table, schema=SCHEMA):
            populated = bind.execute(sa.text(f'SELECT EXISTS (SELECT 1 FROM "{SCHEMA}"."{table}" LIMIT 1)')).scalar_one()
            if populated:
                raise RuntimeError(f"refusing to drop non-empty durable reconciliation table {SCHEMA}.{table}")
    for table in (TARGETS, CHECKPOINTS):
        if inspector.has_table(table, schema=SCHEMA):
            op.drop_table(table, schema=SCHEMA)
            inspector = sa.inspect(bind)
