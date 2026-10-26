"""Add model providers/instances while preserving numeric model identities.

Revision ID: b8d0f2a4c6e8
Revises: a7c9e1f3b5d7
"""

from typing import Any

import sqlalchemy as sa
from alembic import op

from api.db.model_backfill import backfill

revision: str = "b8d0f2a4c6e8"
down_revision: str | None = "a7c9e1f3b5d7"
branch_labels: str | None = None
depends_on: str | None = None
SCHEMA = "usr_ai"


def _timestamps() -> list[sa.Column[Any]]:
    return [
        sa.Column("create_date", sa.DateTime(), nullable=True),
        sa.Column("update_date", sa.DateTime(), nullable=True),
        sa.Column("create_time", sa.BigInteger(), nullable=True),
        sa.Column("update_time", sa.BigInteger(), nullable=True),
    ]


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("t_ai_tenant_model_providers", schema=SCHEMA):
        op.create_table(
            "t_ai_tenant_model_providers",
            *_timestamps(),
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("tenant_id", sa.String(32), nullable=False),
            sa.Column("provider_name", sa.String(128), nullable=False),
            sa.Column("status", sa.String(8), nullable=False, server_default="active"),
            sa.UniqueConstraint("tenant_id", "provider_name", name="uq_model_provider_owner_name"),
            sa.CheckConstraint("status IN ('active', 'inactive')", name="ck_model_provider_status"),
            schema=SCHEMA,
        )
        op.create_index("ix_usr_ai_t_ai_tenant_model_providers_tenant_id", "t_ai_tenant_model_providers", ["tenant_id"], schema=SCHEMA)
    if not inspector.has_table("t_ai_tenant_model_instances", schema=SCHEMA):
        op.create_table(
            "t_ai_tenant_model_instances",
            *_timestamps(),
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("provider_id", sa.String(32), sa.ForeignKey(f"{SCHEMA}.t_ai_tenant_model_providers.id", ondelete="RESTRICT"), nullable=False),
            sa.Column("instance_name", sa.String(128), nullable=False),
            sa.Column("api_key", sa.Text(), nullable=False),
            sa.Column("base_url", sa.String(2048), nullable=False, server_default=""),
            sa.Column("region", sa.String(128), nullable=False, server_default=""),
            sa.Column("status", sa.String(8), nullable=False, server_default="active"),
            sa.UniqueConstraint("provider_id", "instance_name", name="uq_model_instance_provider_name"),
            sa.CheckConstraint("status IN ('active', 'inactive')", name="ck_model_instance_status"),
            schema=SCHEMA,
        )
        op.create_index("ix_usr_ai_t_ai_tenant_model_instances_provider_id", "t_ai_tenant_model_instances", ["provider_id"], schema=SCHEMA)
    if not inspector.has_table("t_ai_tenant_models", schema=SCHEMA):
        op.create_table(
            "t_ai_tenant_models",
            *_timestamps(),
            sa.Column("id", sa.BigInteger(), sa.ForeignKey(f"{SCHEMA}.t_ai_tenant_llms.id", ondelete="RESTRICT"), primary_key=True, autoincrement=False),
            sa.Column("instance_id", sa.String(32), sa.ForeignKey(f"{SCHEMA}.t_ai_tenant_model_instances.id", ondelete="RESTRICT"), nullable=False),
            sa.Column("model_name", sa.String(128), nullable=False),
            sa.Column("model_types", sa.JSON(), nullable=False),
            sa.Column("is_tools", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.UniqueConstraint("instance_id", "model_name", name="uq_instance_model_name"),
            schema=SCHEMA,
        )
        op.create_index("ix_usr_ai_t_ai_tenant_models_instance_id", "t_ai_tenant_models", ["instance_id"], schema=SCHEMA)
    columns = {column["name"] for column in sa.inspect(bind).get_columns("t_ai_tenants", schema=SCHEMA)}
    if "ocr_id" not in columns:
        op.add_column("t_ai_tenants", sa.Column("ocr_id", sa.String(128), nullable=True), schema=SCHEMA)
    if "tenant_ocr_id" not in columns:
        op.add_column("t_ai_tenants", sa.Column("tenant_ocr_id", sa.BigInteger(), nullable=True), schema=SCHEMA)
    legacy_columns = {column["name"]: column for column in sa.inspect(bind).get_columns("t_ai_tenant_llms", schema=SCHEMA)}
    if legacy_columns["api_base"]["type"].length < 2048:
        op.alter_column("t_ai_tenant_llms", "api_base", type_=sa.String(2048), existing_type=legacy_columns["api_base"]["type"], schema=SCHEMA)
    backfill(bind)


def downgrade() -> None:
    raise RuntimeError("Provider-instance rollback requires reviewed data export; automatic destructive downgrade is disabled")
