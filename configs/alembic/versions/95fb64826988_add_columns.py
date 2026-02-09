"""add columns

Revision ID: 95fb64826988
Revises: 4140f9801e0d
Create Date: 2026-02-09 21:19:53.456100

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import Inspector


# revision identifiers, used by Alembic.
revision: str = '95fb64826988'
down_revision: Union[str, None] = '4140f9801e0d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    insp = Inspector.from_engine(bind)

    cols = [col["name"] for col in insp.get_columns("t_ai_api4conversations", schema="usr_ai")]

    if "name" not in cols:
        with op.batch_alter_table("t_ai_api4conversations", schema="usr_ai") as batch_op:
            batch_op.add_column(
                sa.Column(
                    "name",
                    sa.String(255),
                    nullable=True,
                    doc="conversation name",
                )
            )

    if "exp_user_id" not in cols:
        with op.batch_alter_table("t_ai_api4conversations", schema="usr_ai") as batch_op:
            batch_op.add_column(
                sa.Column(
                    "exp_user_id",
                    sa.String(255),
                    nullable=True,
                    index=True,
                    doc="exp_user_id",
                )
            )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("t_ai_api4conversations", schema="usr_ai") as batch_op:
        batch_op.drop_column("exp_user_id")
        batch_op.drop_column("name")
