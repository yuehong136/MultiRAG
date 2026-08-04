"""store synchronization cursors as native timezone-aware datetimes

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-08-02 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f6a7b8c9d0e1"
down_revision: str | None = "e5f6a7b8c9d0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "usr_ai"
TABLE = "t_ai_sync_logs"


def upgrade() -> None:
    bind = op.get_bind()
    is_postgresql = bind.dialect.name == "postgresql"

    op.alter_column(
        TABLE,
        "time_started",
        schema=SCHEMA,
        existing_type=sa.DateTime(timezone=False),
        type_=sa.DateTime(timezone=True),
        postgresql_using="\"time_started\" AT TIME ZONE 'UTC'" if is_postgresql else None,
    )
    for column_name in ("poll_range_start", "poll_range_end"):
        op.alter_column(
            TABLE,
            column_name,
            schema=SCHEMA,
            existing_type=sa.String(length=255),
            type_=sa.DateTime(timezone=True),
            postgresql_using=f'"{column_name}"::timestamptz' if is_postgresql else None,
        )


def downgrade() -> None:
    bind = op.get_bind()
    is_postgresql = bind.dialect.name == "postgresql"

    for column_name in ("poll_range_start", "poll_range_end"):
        op.alter_column(
            TABLE,
            column_name,
            schema=SCHEMA,
            existing_type=sa.DateTime(timezone=True),
            type_=sa.String(length=255),
            postgresql_using=f'"{column_name}"::text' if is_postgresql else None,
        )
    op.alter_column(
        TABLE,
        "time_started",
        schema=SCHEMA,
        existing_type=sa.DateTime(timezone=True),
        type_=sa.DateTime(timezone=False),
        postgresql_using="\"time_started\" AT TIME ZONE 'UTC'" if is_postgresql else None,
    )
