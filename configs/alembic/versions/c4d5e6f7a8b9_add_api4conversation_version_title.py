"""add api4conversation version_title column

Revision ID: c4d5e6f7a8b9
Revises: b1c2d3e4f5a6
Create Date: 2026-06-05 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.engine.reflection import Inspector


revision: str = "c4d5e6f7a8b9"
down_revision: str | None = "b1c2d3e4f5a6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "usr_ai"
TABLE = "t_ai_api4conversations"


def _has_column(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = Inspector.from_engine(bind)
    columns = {column["name"] for column in inspector.get_columns(table_name, schema=SCHEMA)}
    return column_name in columns


def upgrade() -> None:
    if not _has_column(TABLE, "version_title"):
        op.add_column(
            TABLE,
            sa.Column("version_title", sa.String(length=255), nullable=True, comment="canvas version title when session created"),
            schema=SCHEMA,
        )


def downgrade() -> None:
    if _has_column(TABLE, "version_title"):
        op.drop_column(TABLE, "version_title", schema=SCHEMA)
