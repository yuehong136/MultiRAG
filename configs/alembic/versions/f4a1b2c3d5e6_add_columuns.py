"""add document content_hash column

Revision ID: f4a1b2c3d5e6
Revises: c3b7a58f9b2e
Create Date: 2026-03-24 22:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.engine.reflection import Inspector


revision: str = "f4a1b2c3d5e6"
down_revision: Union[str, None] = "c3b7a58f9b2e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

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
        "t_ai_documents",
        sa.Column("content_hash", sa.String(32), nullable=False, server_default="", index=True),
    )


def downgrade() -> None:
    _drop_column_if_exists("t_ai_documents", "content_hash")
