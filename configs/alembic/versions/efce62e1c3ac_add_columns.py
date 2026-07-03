"""add_permission_column_to_user_canvases

Revision ID: efce62e1c3ac
Revises: 1484d1511aa0
Create Date: 2025-10-29 17:14:10.791655

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine.reflection import Inspector

# revision identifiers, used by Alembic.
revision: str = "efce62e1c3ac"
down_revision: str | None = "1484d1511aa0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    insp = Inspector.from_engine(bind)
    # ===== 1. Add columns to t_ai_user_canvases =====
    kb_cols = [col["name"] for col in insp.get_columns("t_ai_user_canvases", schema="usr_ai")]

    with op.batch_alter_table("t_ai_user_canvases", schema="usr_ai") as batch_op:
        if "permission" not in kb_cols:
            batch_op.add_column(sa.Column("permission", sa.String(length=32), nullable=True, server_default="me", index=True, doc="me|team"))


def downgrade() -> None:
    """Downgrade schema."""
    # Remove columns from t_ai_user_canvases
    with op.batch_alter_table("t_ai_user_canvases", schema="usr_ai") as batch_op:
        batch_op.drop_column("permission")
