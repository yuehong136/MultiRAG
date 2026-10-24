"""Separate connector synchronization and pruning logs.

Revision ID: a7c9e1f3b5d7
Revises: f1a3c5e7b9d0
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a7c9e1f3b5d7"
down_revision: str | None = "f1a3c5e7b9d0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("t_ai_sync_logs", sa.Column("task_type", sa.String(32), nullable=False, server_default="sync"), schema="usr_ai")
    op.create_index("ix_usr_ai_t_ai_sync_logs_task_type", "t_ai_sync_logs", ["task_type"], schema="usr_ai")


def downgrade() -> None:
    op.drop_index("ix_usr_ai_t_ai_sync_logs_task_type", table_name="t_ai_sync_logs", schema="usr_ai")
    op.drop_column("t_ai_sync_logs", "task_type", schema="usr_ai")
