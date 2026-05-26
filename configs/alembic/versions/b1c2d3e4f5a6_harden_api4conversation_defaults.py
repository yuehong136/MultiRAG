"""harden api4conversation numeric defaults

Revision ID: b1c2d3e4f5a6
Revises: a7b8c9d0e1f2
Create Date: 2026-05-26 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.engine.reflection import Inspector


revision: str = "b1c2d3e4f5a6"
down_revision: str | None = "a7b8c9d0e1f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "usr_ai"
TABLE = "t_ai_api4conversations"
NUMERIC_DEFAULT_COLUMNS = {
    "tokens": sa.Integer(),
    "duration": sa.Float(),
    "round": sa.Integer(),
    "thumb_up": sa.Integer(),
}


def _existing_columns() -> set[str]:
    bind = op.get_bind()
    inspector = Inspector.from_engine(bind)
    return {column["name"] for column in inspector.get_columns(TABLE, schema=SCHEMA)}


def upgrade() -> None:
    columns = _existing_columns()
    for column_name, column_type in NUMERIC_DEFAULT_COLUMNS.items():
        if column_name not in columns:
            continue

        op.execute(
            sa.text(
                f'UPDATE "{SCHEMA}"."{TABLE}" '
                f'SET "{column_name}" = 0 '
                f'WHERE "{column_name}" IS NULL'
            )
        )
        with op.batch_alter_table(TABLE, schema=SCHEMA) as batch_op:
            batch_op.alter_column(
                column_name,
                existing_type=column_type,
                nullable=False,
                server_default=sa.text("0"),
            )


def downgrade() -> None:
    columns = _existing_columns()
    for column_name, column_type in NUMERIC_DEFAULT_COLUMNS.items():
        if column_name not in columns:
            continue

        with op.batch_alter_table(TABLE, schema=SCHEMA) as batch_op:
            batch_op.alter_column(
                column_name,
                existing_type=column_type,
                nullable=True,
                server_default=None,
            )
