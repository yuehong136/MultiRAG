"""update columns

Revision ID: aae5b5f0d916
Revises: d3c94b4c95c9
Create Date: 2026-03-12 17:15:09.825835

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import Inspector

# revision identifiers, used by Alembic.
revision: str = "aae5b5f0d916"
down_revision: str | None = "d3c94b4c95c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    insp = Inspector.from_engine(bind)

    tables = insp.get_table_names(schema="usr_ai")
    if "t_ai_system_settings" not in tables:
        return

    cols = {col["name"]: col for col in insp.get_columns("t_ai_system_settings", schema="usr_ai")}
    if "value" in cols:
        col_type = cols["value"]["type"]
        # Only alter if still VARCHAR (i.e. not yet TEXT)
        if isinstance(col_type, sa.String) and not isinstance(col_type, sa.Text):
            with op.batch_alter_table("t_ai_system_settings", schema="usr_ai") as batch_op:
                batch_op.alter_column(
                    "value",
                    existing_type=sa.String(length=1024),
                    type_=sa.Text,
                    existing_nullable=False,
                )


def downgrade() -> None:
    """Downgrade schema."""
    bind = op.get_bind()
    insp = Inspector.from_engine(bind)

    tables = insp.get_table_names(schema="usr_ai")
    if "t_ai_system_settings" not in tables:
        return

    cols = {col["name"]: col for col in insp.get_columns("t_ai_system_settings", schema="usr_ai")}
    if "value" in cols:
        col_type = cols["value"]["type"]
        if isinstance(col_type, sa.Text):
            with op.batch_alter_table("t_ai_system_settings", schema="usr_ai") as batch_op:
                batch_op.alter_column(
                    "value",
                    existing_type=sa.Text,
                    type_=sa.String(length=1024),
                    existing_nullable=False,
                )
