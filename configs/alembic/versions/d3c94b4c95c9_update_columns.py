"""update columns

Revision ID: d3c94b4c95c9
Revises: 95fb64826988
Create Date: 2026-02-28 10:48:40.112481

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import Inspector

# revision identifiers, used by Alembic.
revision: str = "d3c94b4c95c9"
down_revision: str | None = "95fb64826988"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    insp = Inspector.from_engine(bind)

    tables = insp.get_table_names(schema="usr_ai")
    if "t_ai_system_settings" not in tables:
        return

    cols = [col["name"] for col in insp.get_columns("t_ai_system_settings", schema="usr_ai")]
    if "setting_type" in cols and "source" not in cols:
        with op.batch_alter_table("t_ai_system_settings", schema="usr_ai") as batch_op:
            batch_op.alter_column(
                "setting_type",
                new_column_name="source",
                existing_type=sa.String(length=32),
                existing_nullable=False,
            )


def downgrade() -> None:
    """Downgrade schema."""
    bind = op.get_bind()
    insp = Inspector.from_engine(bind)

    tables = insp.get_table_names(schema="usr_ai")
    if "t_ai_system_settings" not in tables:
        return

    cols = [col["name"] for col in insp.get_columns("t_ai_system_settings", schema="usr_ai")]
    if "source" in cols and "setting_type" not in cols:
        with op.batch_alter_table("t_ai_system_settings", schema="usr_ai") as batch_op:
            batch_op.alter_column(
                "source",
                new_column_name="setting_type",
                existing_type=sa.String(length=32),
                existing_nullable=False,
            )
