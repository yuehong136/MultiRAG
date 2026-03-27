"""add user_canvas_version release column

Revision ID: a7b8c9d0e1f2
Revises: f4a1b2c3d5e6
Create Date: 2026-03-27 10:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.engine.reflection import Inspector


revision: str = "a7b8c9d0e1f2"
down_revision: str | None = "f4a1b2c3d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "usr_ai"


def _has_column(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = Inspector.from_engine(bind)
    columns = {column["name"] for column in inspector.get_columns(table_name, schema=SCHEMA)}
    return column_name in columns


def _add_column_if_missing(table_name: str, column: sa.Column) -> None:
    if not _has_column(table_name, column.name):
        op.add_column(table_name, column, schema=SCHEMA)


def _drop_column_if_exists(table_name: str, column_name: str) -> None:
    if _has_column(table_name, column_name):
        op.drop_column(table_name, column_name, schema=SCHEMA)


def upgrade() -> None:
    _add_column_if_missing(
        "t_ai_user_canvas_version",
        sa.Column("release", sa.Boolean(), nullable=False, server_default=sa.text("false"), index=True, comment="is released"),
    )


def downgrade() -> None:
    _drop_column_if_exists("t_ai_user_canvas_version", "release")
