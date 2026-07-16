"""add canvas_template canvas_types column

Revision ID: e5f6a7b8c9d0
Revises: c4d5e6f7a8b9
Create Date: 2026-07-16 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine.reflection import Inspector

revision: str = "e5f6a7b8c9d0"
down_revision: str | None = "c4d5e6f7a8b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "usr_ai"
TABLE = "t_ai_canvas_templates"


def _json_type_for(dialect_name: str):
    if dialect_name == "postgresql":
        return sa.dialects.postgresql.JSONB
    elif dialect_name == "mysql":
        return sa.JSON
    elif dialect_name == "sqlite":
        # sqlite 无原生 JSON，通常以 TEXT 存储
        return sa.Text
    return sa.JSON


def _has_column(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = Inspector.from_engine(bind)
    columns = {column["name"] for column in inspector.get_columns(table_name, schema=SCHEMA)}
    return column_name in columns


def upgrade() -> None:
    if not _has_column(TABLE, "canvas_types"):
        bind = op.get_bind()
        op.add_column(
            TABLE,
            sa.Column("canvas_types", _json_type_for(bind.dialect.name), nullable=True, comment="Canvas types"),
            schema=SCHEMA,
        )


def downgrade() -> None:
    if _has_column(TABLE, "canvas_types"):
        op.drop_column(TABLE, "canvas_types", schema=SCHEMA)
