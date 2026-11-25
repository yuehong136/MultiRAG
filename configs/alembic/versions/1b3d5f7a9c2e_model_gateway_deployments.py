"""Let several models serve one gateway name.

Model settings gain ``weight`` and ``deployment_order`` (LiteLLM's ``weight``
and ``order``), and the alias index stops being unique: models given the same
alias are the deployments of that name. Request logs gain ``attempts``, the
upstream calls of a request that made more than one.

A model that was marked preferred used to receive every call to its shared
name. It gets order 1, so it still does and the others stand in when it fails.

Each step is skipped when a table created from the current models already
has it, so both bootstrap paths end in the same schema.

Revision ID: 1b3d5f7a9c2e
Revises: 0a2c4e6f8b1d
"""

from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "1b3d5f7a9c2e"
down_revision: str | None = "0a2c4e6f8b1d"
branch_labels: str | None = None
depends_on: str | None = None
SCHEMA = "usr_ai"
_LOGS = "t_ai_gateway_request_logs"
_SETTINGS = "t_ai_gateway_model_settings"
_UNIQUE_ALIAS = "uq_gateway_model_settings_tenant_alias"
_ALIAS = "ix_gateway_model_settings_tenant_alias"


def _added_columns() -> dict[str, list[sa.Column[Any]]]:
    return {
        _LOGS: [sa.Column("attempts", postgresql.JSONB(), nullable=True)],
        _SETTINGS: [sa.Column("weight", sa.Integer(), nullable=True), sa.Column("deployment_order", sa.Integer(), nullable=True)],
    }


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table, schema=SCHEMA)}


def _indexes(table: str) -> set[str]:
    return {str(index["name"]) for index in sa.inspect(op.get_bind()).get_indexes(table, schema=SCHEMA)}


def _alias_index(name: str, *, unique: bool) -> None:
    op.create_index(name, _SETTINGS, ["tenant_id", "alias"], unique=unique, schema=SCHEMA, postgresql_where=sa.text("alias IS NOT NULL"))


def upgrade() -> None:
    created_order = False
    for table, columns in _added_columns().items():
        existing = _columns(table)
        for column in columns:
            if column.name not in existing:
                op.add_column(table, column, schema=SCHEMA)
                created_order = created_order or column.name == "deployment_order"
    if created_order:
        op.execute(sa.text(f"UPDATE {SCHEMA}.{_SETTINGS} SET deployment_order = 1 WHERE preferred"))
    indexes = _indexes(_SETTINGS)
    if _UNIQUE_ALIAS in indexes:
        op.drop_index(_UNIQUE_ALIAS, table_name=_SETTINGS, schema=SCHEMA)
    if _ALIAS not in indexes:
        _alias_index(_ALIAS, unique=False)


def downgrade() -> None:
    # Aliases shared by several models cannot be unique again: all but the oldest lose theirs.
    op.execute(
        sa.text(
            f"UPDATE {SCHEMA}.{_SETTINGS} AS later SET alias = NULL WHERE alias IS NOT NULL AND EXISTS ("
            f"SELECT 1 FROM {SCHEMA}.{_SETTINGS} AS earlier WHERE earlier.tenant_id = later.tenant_id AND earlier.alias = later.alias "
            "AND (COALESCE(earlier.create_time, 0), earlier.id) < (COALESCE(later.create_time, 0), later.id))"
        )
    )
    indexes = _indexes(_SETTINGS)
    if _ALIAS in indexes:
        op.drop_index(_ALIAS, table_name=_SETTINGS, schema=SCHEMA)
    if _UNIQUE_ALIAS not in indexes:
        _alias_index(_UNIQUE_ALIAS, unique=True)
    for table, columns in _added_columns().items():
        existing = _columns(table)
        for column in columns:
            if column.name in existing:
                op.drop_column(table, column.name, schema=SCHEMA)
