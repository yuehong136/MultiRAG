"""add release column to user canvases

Revision ID: 19819cd0b63a
Revises: 284487da1d89
Create Date: 2026-03-23 22:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine.reflection import Inspector

# revision identifiers, used by Alembic.
revision: str = '19819cd0b63a'
down_revision: str | None = '284487da1d89'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "usr_ai"
TBL = "t_ai_user_canvases"


def upgrade() -> None:
    """Add release column to user_canvases."""
    bind = op.get_bind()
    insp = Inspector.from_engine(bind)
    cols = {c["name"] for c in insp.get_columns(TBL, schema=SCHEMA)}

    if "release" not in cols:
        with op.batch_alter_table(TBL, schema=SCHEMA) as batch_op:
            batch_op.add_column(
                sa.Column(
                    "release",
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.text("false"),
                    index=True,
                    comment="is released",
                )
            )


def downgrade() -> None:
    """Remove release column from user_canvases."""
    with op.batch_alter_table(TBL, schema=SCHEMA) as batch_op:
        batch_op.drop_column("release")
